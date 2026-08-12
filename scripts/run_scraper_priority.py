#!/usr/bin/env python3
"""
Production scraper for priority norm IDs.

Reads norm IDs from priority_norms.txt and scrapes them with checkpoint + resume + S3.

Usage:
    # Fresh start
    python run_scraper_priority.py --instance-id priority-1 --s3-bucket jurispeed-bcn-legal-docs

    # Resume from checkpoint
    python run_scraper_priority.py --resume --instance-id priority-1 --s3-bucket jurispeed-bcn-legal-docs
"""

import asyncio
import argparse
import structlog
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import Config
from core.scraper_playwright import BCNPlaywrightScraper
from pipeline.checkpoint import CheckpointManager
from storage.s3_client import S3Storage
from core.models import ChileanLegalNorm

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


def load_priority_ids(file_path: str) -> list[int]:
    """
    Load priority norm IDs from file.

    Args:
        file_path: Path to file with one ID per line

    Returns:
        List of norm IDs (deduplicated and sorted)
    """
    ids = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and line.isdigit():  # Skip empty lines and non-numeric
                ids.append(int(line))

    # Deduplicate and sort
    unique_ids = sorted(set(ids))

    logger.info(
        "priority_ids_loaded",
        file=file_path,
        total_lines=len(ids),
        unique_ids=len(unique_ids),
    )

    return unique_ids


class PriorityScraper:
    """
    Scraper for priority norm IDs with checkpoint + resume.

    Features:
    - Reads from priority_norms.txt
    - Checkpoint after every N docs processed
    - Resume from last checkpoint (index-based)
    - Upload to S3 immediately
    """

    def __init__(
        self,
        config: Config,
        instance_id: str,
        s3_bucket: str,
        priority_ids: list[int],
    ):
        self.config = config
        self.instance_id = instance_id
        self.priority_ids = priority_ids

        # Initialize components
        self.scraper = BCNPlaywrightScraper(config.scraper)
        self.checkpoint_mgr = CheckpointManager(config.aws, instance_id)
        self.s3_storage = S3Storage(
            bucket_name=s3_bucket,
            region=config.aws.region,
            aws_access_key_id=config.aws.access_key_id,
            aws_secret_access_key=config.aws.secret_access_key,
        )

        logger.info(
            "priority_scraper_initialized",
            instance_id=instance_id,
            s3_bucket=s3_bucket,
            total_ids=len(priority_ids),
        )

    async def scrape_priority_list(self, start_index: int = 0) -> None:
        """
        Scrape priority norm IDs starting from index.

        Args:
            start_index: Index in priority_ids to start from (for resume)
        """
        try:
            # Start browser
            await self.scraper.start()

            total_ids = len(self.priority_ids)
            ids_to_process = self.priority_ids[start_index:]

            logger.info(
                "scraping_started",
                instance_id=self.instance_id,
                start_index=start_index,
                total_ids=total_ids,
                remaining_ids=len(ids_to_process),
            )

            # Process each ID
            for idx, norm_id in enumerate(ids_to_process, start=start_index):
                # Scrape one norm
                norm = await self.scraper.scrape_one(norm_id)

                # Upload to S3 if successful
                if norm:
                    await self._store_norm(norm)

                # Checkpoint every N docs
                if (idx + 1) % self.config.scraper.checkpoint_every == 0:
                    stats = self.scraper.get_stats()

                    # Save current index as last_id_processed
                    self.checkpoint_mgr.save(idx, stats)

                    progress_pct = ((idx + 1) / total_ids) * 100

                    logger.info(
                        "checkpoint_saved",
                        instance_id=self.instance_id,
                        current_index=idx,
                        current_norm_id=norm_id,
                        progress=f"{idx + 1}/{total_ids}",
                        progress_pct=f"{progress_pct:.1f}%",
                        success_rate=f"{stats.success_rate:.1f}%",
                    )

            # Mark as completed
            final_stats = self.scraper.get_stats()
            self.checkpoint_mgr.mark_completed(final_stats)

            logger.info(
                "scraping_completed",
                instance_id=self.instance_id,
                final_stats=final_stats.to_dict(),
                s3_stats=self.s3_storage.get_stats(),
            )

        except KeyboardInterrupt:
            logger.warning("scraping_interrupted_by_user", instance_id=self.instance_id)
            # Save checkpoint before exiting
            stats = self.scraper.get_stats()
            self.checkpoint_mgr.save(idx, stats)
            raise

        except Exception as e:
            logger.error(
                "scraping_failed",
                instance_id=self.instance_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            self.checkpoint_mgr.mark_failed(str(e))
            raise

        finally:
            # Cleanup
            await self.scraper.close()

    async def _store_norm(self, norm: ChileanLegalNorm) -> None:
        """
        Store norm to S3.

        Args:
            norm: Validated legal norm
        """
        # Build S3 key
        doc_id = f"bcn-{norm.norm_id}"
        key = self.s3_storage.build_key(
            knowledge_id=self.config.lexintel.knowledge_id,
            doc_id=doc_id,
            prefix="originals",
        )

        # Convert to dict with JSON serialization
        data = norm.model_dump(mode='json')

        # Add metadata
        metadata = {
            "instance_id": self.instance_id,
            "source": "bcn-scraper-priority",
        }

        # Upload
        success = self.s3_storage.store_document(key, data, metadata)

        if success:
            logger.debug(
                "norm_stored_s3",
                norm_id=norm.norm_id,
                key=key,
            )
        else:
            logger.error(
                "norm_storage_failed",
                norm_id=norm.norm_id,
                key=key,
            )

    def get_resume_index(self) -> int:
        """
        Get last checkpoint index to resume from.

        Returns:
            Last processed index (0-based) or 0 if no checkpoint
        """
        checkpoint = self.checkpoint_mgr.load()

        if checkpoint:
            last_index = checkpoint.get("last_id_processed", 0)
            logger.info(
                "resume_from_checkpoint",
                instance_id=self.instance_id,
                last_index=last_index,
                total_processed=checkpoint.get("total_processed"),
            )
            return last_index + 1  # Continue from next index

        logger.info("no_checkpoint_found", instance_id=self.instance_id)
        return 0


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="BCN Priority Norms Scraper with Checkpoint & S3 Storage"
    )

    parser.add_argument(
        "--instance-id",
        type=str,
        required=True,
        help="Instance ID (for checkpoint tracking)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint",
    )
    parser.add_argument(
        "--s3-bucket",
        type=str,
        help="S3 bucket name (default: from env S3_BUCKET_NAME)",
    )
    parser.add_argument(
        "--priority-file",
        type=str,
        default="priority_norms.txt",
        help="File with priority norm IDs (default: priority_norms.txt)",
    )

    args = parser.parse_args()

    # Load config
    try:
        config = Config.from_env()
        config.validate_required()
    except ValueError as e:
        logger.error("config_validation_failed", error=str(e))
        sys.exit(1)

    # Get S3 bucket
    import os
    s3_bucket = args.s3_bucket or os.getenv("S3_BUCKET_NAME")
    if not s3_bucket:
        logger.error("s3_bucket_required", message="Provide --s3-bucket or set S3_BUCKET_NAME env var")
        sys.exit(1)

    # Load priority IDs
    try:
        priority_ids = load_priority_ids(args.priority_file)
    except FileNotFoundError:
        logger.error("priority_file_not_found", file=args.priority_file)
        sys.exit(1)
    except Exception as e:
        logger.error("priority_file_error", file=args.priority_file, error=str(e))
        sys.exit(1)

    if not priority_ids:
        logger.error("no_priority_ids", message="No valid norm IDs found in file")
        sys.exit(1)

    # Initialize scraper
    scraper = PriorityScraper(
        config=config,
        instance_id=args.instance_id,
        s3_bucket=s3_bucket,
        priority_ids=priority_ids,
    )

    # Determine start index
    if args.resume:
        start_index = scraper.get_resume_index()
        logger.info(
            "resuming_scraping",
            instance_id=args.instance_id,
            start_index=start_index,
            remaining=len(priority_ids) - start_index,
        )
    else:
        start_index = 0
        logger.info(
            "starting_fresh",
            instance_id=args.instance_id,
            total_ids=len(priority_ids),
        )

    # Run scraper
    try:
        await scraper.scrape_priority_list(start_index)
        logger.info("scraper_finished_successfully", instance_id=args.instance_id)
        sys.exit(0)

    except KeyboardInterrupt:
        logger.warning("scraper_interrupted", instance_id=args.instance_id)
        sys.exit(130)

    except Exception as e:
        logger.error(
            "scraper_failed",
            instance_id=args.instance_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
