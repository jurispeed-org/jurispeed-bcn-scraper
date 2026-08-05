#!/usr/bin/env python3
"""
Production scraper with checkpoint + resume + S3 storage.

Usage:
    # Fresh start (scrape from ID 1)
    python run_scraper.py --start 1 --end 60000 --instance-id ec2-instance-1

    # Resume from checkpoint
    python run_scraper.py --resume --instance-id ec2-instance-1

    # Resume and continue to new end
    python run_scraper.py --resume --end 100000 --instance-id ec2-instance-1
"""

import asyncio
import argparse
import structlog
import sys
from pathlib import Path

from src.config import Config
from src.scraper_playwright import BCNPlaywrightScraper
from src.checkpoint import CheckpointManager
from src.s3_client import S3Storage
from src.models import ChileanLegalNorm

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


class ProductionScraper:
    """
    Production-ready scraper with checkpoint + resume + S3.

    Features:
    - Checkpoint every 1,000 docs
    - Resume from last checkpoint
    - Upload to S3 immediately
    - Robust error handling
    - CloudWatch compatible logs
    """

    def __init__(
        self,
        config: Config,
        instance_id: str,
        s3_bucket: str,
    ):
        self.config = config
        self.instance_id = instance_id

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
            "production_scraper_initialized",
            instance_id=instance_id,
            s3_bucket=s3_bucket,
        )

    async def scrape_with_storage(
        self,
        start_id: int,
        end_id: int,
    ) -> None:
        """
        Scrape range with S3 storage and checkpointing.

        Args:
            start_id: Starting norm ID
            end_id: Ending norm ID (inclusive)
        """
        try:
            # Start browser
            await self.scraper.start()

            logger.info(
                "scraping_started",
                instance_id=self.instance_id,
                start_id=start_id,
                end_id=end_id,
                total_range=end_id - start_id + 1,
            )

            # Scrape range
            for norm_id in range(start_id, end_id + 1):
                # Scrape one norm
                norm = await self.scraper.scrape_one(norm_id)

                # Upload to S3 if successful
                if norm:
                    await self._store_norm(norm)

                # Checkpoint every N docs
                if (norm_id - start_id + 1) % self.config.scraper.checkpoint_every == 0:
                    stats = self.scraper.get_stats()
                    self.checkpoint_mgr.save(norm_id, stats)

                    progress_pct = ((norm_id - start_id + 1) / (end_id - start_id + 1)) * 100

                    logger.info(
                        "checkpoint_saved",
                        instance_id=self.instance_id,
                        current_id=norm_id,
                        progress=f"{norm_id - start_id + 1}/{end_id - start_id + 1}",
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
            self.checkpoint_mgr.save(norm_id, stats)
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
            "source": "bcn-scraper",
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

    def get_resume_point(self) -> int | None:
        """
        Get last checkpoint ID to resume from.

        Returns:
            Last processed ID or None if no checkpoint
        """
        checkpoint = self.checkpoint_mgr.load()

        if checkpoint:
            last_id = checkpoint.get("last_id_processed")
            logger.info(
                "resume_from_checkpoint",
                instance_id=self.instance_id,
                last_id=last_id,
                total_processed=checkpoint.get("total_processed"),
            )
            return last_id

        logger.info("no_checkpoint_found", instance_id=self.instance_id)
        return None


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="BCN Legal Norms Scraper with Checkpoint & S3 Storage"
    )

    parser.add_argument(
        "--start",
        type=int,
        help="Starting norm ID (default: 1)",
    )
    parser.add_argument(
        "--end",
        type=int,
        required=True,
        help="Ending norm ID (inclusive)",
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

    # Initialize scraper
    scraper = ProductionScraper(
        config=config,
        instance_id=args.instance_id,
        s3_bucket=s3_bucket,
    )

    # Determine start point
    if args.resume:
        last_id = scraper.get_resume_point()
        if last_id:
            start_id = last_id + 1  # Continue from next ID
            logger.info(
                "resuming_scraping",
                instance_id=args.instance_id,
                resume_from_id=start_id,
            )
        else:
            # No checkpoint, start from beginning or user-provided start
            start_id = args.start or 1
            logger.info(
                "no_checkpoint_starting_fresh",
                instance_id=args.instance_id,
                start_id=start_id,
            )
    else:
        # Fresh start
        start_id = args.start or 1
        logger.info(
            "starting_fresh",
            instance_id=args.instance_id,
            start_id=start_id,
        )

    end_id = args.end

    # Validate range
    if start_id > end_id:
        logger.error(
            "invalid_range",
            start_id=start_id,
            end_id=end_id,
            message="Start ID must be <= End ID",
        )
        sys.exit(1)

    # Run scraper
    try:
        await scraper.scrape_with_storage(start_id, end_id)
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
