"""
Command-line interface for BCN scraper.

Professional CLI with click framework.
"""

import asyncio
import sys
import structlog
import click
from typing import Optional
from .utils.config import Config
from .core.scraper import BCNScraper
from .pipeline.indexer import ProfessionalIndexer
from .pipeline.checkpoint import CheckpointManager

# Configure structured logging
def configure_logging(log_level: str = "INFO", log_format: str = "json"):
    """Configure structlog based on format preference."""
    if log_format == "json":
        processors = [
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(structlog.processors, log_level.upper(), structlog.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


@click.command()
@click.option(
    "--instance-id",
    required=True,
    help="Unique ID for this scraper instance (e.g., scraper-1)",
)
@click.option(
    "--range-start",
    type=int,
    required=True,
    help="Starting norm ID (e.g., 1)",
)
@click.option(
    "--range-end",
    type=int,
    required=True,
    help="Ending norm ID (e.g., 60000)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Scrape only, don't upload to Lexintel",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Resume from last checkpoint",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="INFO",
    help="Logging level",
)
@click.option(
    "--log-format",
    type=click.Choice(["json", "console"], case_sensitive=False),
    default="json",
    help="Log output format",
)
def main(
    instance_id: str,
    range_start: int,
    range_end: int,
    dry_run: bool,
    resume: bool,
    log_level: str,
    log_format: str,
):
    """
    BCN Legal Norms Scraper - Professional Edition

    Scrapes Chilean legal norms from BCN and indexes them in Lexintel.

    Examples:

        \b
        # Dry run (scrape without uploading)
        python -m src.cli --instance-id test --range-start 1 --range-end 100 --dry-run

        \b
        # Production run
        python -m src.cli --instance-id scraper-1 --range-start 1 --range-end 60000

        \b
        # Resume from checkpoint
        python -m src.cli --instance-id scraper-1 --range-start 1 --range-end 60000 --resume
    """
    # Configure logging
    configure_logging(log_level, log_format)
    logger = structlog.get_logger()

    # Validate range
    if range_start < 1:
        click.echo("Error: range-start must be >= 1", err=True)
        sys.exit(1)

    if range_end < range_start:
        click.echo("Error: range-end must be >= range-start", err=True)
        sys.exit(1)

    logger.info(
        "scraper_starting",
        instance_id=instance_id,
        range_start=range_start,
        range_end=range_end,
        total_range=range_end - range_start + 1,
        dry_run=dry_run,
        resume=resume,
    )

    try:
        # Run async scraper
        asyncio.run(
            run_scraper(
                instance_id=instance_id,
                range_start=range_start,
                range_end=range_end,
                dry_run=dry_run,
                resume=resume,
            )
        )

        logger.info("scraper_completed_successfully")
        sys.exit(0)

    except KeyboardInterrupt:
        logger.warning("scraper_interrupted_by_user")
        sys.exit(130)

    except Exception as e:
        logger.error(
            "scraper_fatal_error",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        sys.exit(1)


async def run_scraper(
    instance_id: str,
    range_start: int,
    range_end: int,
    dry_run: bool,
    resume: bool,
):
    """Main scraper logic (async)."""
    logger = structlog.get_logger()

    # Load configuration
    try:
        config = Config.from_env()
        config.validate_required()
    except ValueError as e:
        logger.error("config_validation_failed", error=str(e))
        raise

    # Initialize components
    scraper = BCNScraper(config.scraper)
    checkpoint_mgr = CheckpointManager(config.aws, instance_id)
    indexer: Optional[ProfessionalIndexer] = None

    if not dry_run:
        indexer = ProfessionalIndexer(config)
        # Verify OpenSearch connection
        try:
            if not indexer.opensearch.ensure_index_exists():
                logger.warning("opensearch_index_missing")
        except Exception as e:
            logger.error("opensearch_check_failed", error=str(e))
            raise

    # Resume from checkpoint if requested
    actual_start = range_start
    if resume:
        last_id = checkpoint_mgr.get_last_id()
        if last_id and last_id >= range_start:
            actual_start = last_id + 1
            logger.info("resuming_from_checkpoint", last_id=last_id, new_start=actual_start)

    # Check if already complete
    if actual_start > range_end:
        logger.info("range_already_complete", last_checkpoint=actual_start - 1)
        return

    try:
        # Define checkpoint callback
        async def on_checkpoint(norm_id: int, stats):
            """Called at each checkpoint."""
            checkpoint_mgr.save(
                last_id_processed=norm_id,
                stats=stats,
                metadata={
                    "range_start": range_start,
                    "range_end": range_end,
                    "dry_run": dry_run,
                },
            )

            # Indexing happens in real-time, not batching
            # (Batching would require storing norms in memory)
            pass

        # Scrape with checkpoints
        logger.info(
            "scraping_starting",
            actual_start=actual_start,
            range_end=range_end,
            total=range_end - actual_start + 1,
        )

        # Scrape one by one with upload (memory efficient)
        for norm_id in range(actual_start, range_end + 1):
            # Scrape
            norm = await scraper.scrape_one(norm_id)

            # Index if successful and not dry-run
            if norm and indexer:
                try:
                    indexer.index_norm(norm)
                except Exception as e:
                    logger.error(
                        "indexing_failed",
                        norm_id=norm_id,
                        error=str(e),
                    )
                    # Continue scraping even if indexing fails

            # Checkpoint periodically
            if (norm_id - actual_start + 1) % config.scraper.checkpoint_every == 0:
                await on_checkpoint(norm_id, scraper.stats)

        # Final checkpoint
        checkpoint_mgr.mark_completed(scraper.stats)

        # Log final stats
        logger.info(
            "scraping_complete",
            final_stats=scraper.stats.to_dict(),
            indexing_stats=indexer.get_stats() if indexer else {},
        )

    except Exception as e:
        checkpoint_mgr.mark_failed(str(e))
        raise

    finally:
        # Cleanup
        await scraper.close()
        if indexer:
            indexer.close()


if __name__ == "__main__":
    main()
