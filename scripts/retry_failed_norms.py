#!/usr/bin/env python3
"""
Retry failed norms from DynamoDB tracking table.

Queries jurispeed-norm-status for failed norms and re-runs them through the
EXACT production path (ProductionScraper.scrape_with_storage), so a retried
norm lands in S3 with the same shape as one scraped in the main batch.

After the run, norms now present in S3 are marked as success, which is what
takes them out of the failed set. Norms that fail again are left marked as
failed with attempts incremented by the production path, so they can be
retried later (or given up on once attempts >= max-attempts).

Usage:
    python scripts/retry_failed_norms.py --instance-id retry-1
    python scripts/retry_failed_norms.py --instance-id retry-1 --max-attempts 5
    python scripts/retry_failed_norms.py --instance-id retry-1 --dry-run
"""

import asyncio
import argparse
import json
import structlog
import sys
from pathlib import Path

# Add src and scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from utils.config import Config
from pipeline.norm_tracker import NormTracker
from run_scraper import ProductionScraper

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


def reconcile_with_s3(
    production: ProductionScraper,
    config: Config,
    tracker: NormTracker,
    norm_ids: list[int],
) -> tuple[int, int]:
    """
    Mark as success every retried norm that is now in S3.

    S3 is the source of truth for success (production only writes failures to
    DynamoDB), so presence of the document is what clears the failed status.

    Returns:
        (recovered, still_failed)
    """
    recovered = 0
    still_failed = 0

    for norm_id in norm_ids:
        key = production.s3_storage.build_key(
            knowledge_id=config.lexintel.knowledge_id,
            doc_id=f"bcn-{norm_id}",
            prefix="originals",
        )

        try:
            data = json.loads(production.s3_storage.get_object(key))
        except Exception:
            still_failed += 1
            continue

        tracker.mark_success(
            norm_id,
            data.get("source", "xml"),
            data.get("total_chunks", 0),
            data.get("total_articles", 0),
        )
        recovered += 1
        logger.info(
            "retry_recovered",
            norm_id=norm_id,
            chunks=data.get("total_chunks", 0),
        )

    return recovered, still_failed


async def retry_failed_norms(
    config: Config,
    instance_id: str,
    s3_bucket: str,
    max_attempts: int = 5,
    dry_run: bool = False,
):
    """
    Retry all failed norms from DynamoDB through the production path.

    Args:
        config: Configuration
        instance_id: Instance identifier (checkpoint key + S3 metadata)
        s3_bucket: S3 bucket name
        max_attempts: Only retry norms with fewer than this many attempts
        dry_run: List the norms that would be retried, then stop
    """
    tracker = NormTracker(config.aws)

    logger.info("retrieving_failed_norms", max_attempts=max_attempts)
    failed_norms = tracker.get_failed(max_attempts=max_attempts)

    if not failed_norms:
        logger.info("no_failed_norms_to_retry")
        return

    norm_ids = [int(item["norm_id"]) for item in failed_norms]

    logger.info(
        "retry_starting",
        total_failed=len(norm_ids),
        instance_id=instance_id,
        norm_ids=norm_ids,
    )

    if dry_run:
        logger.info("dry_run_no_changes_made", total_failed=len(norm_ids))
        return

    production = ProductionScraper(
        config=config,
        instance_id=instance_id,
        s3_bucket=s3_bucket,
    )

    await production.scrape_with_storage(norm_ids)

    recovered, still_failed = reconcile_with_s3(production, config, tracker, norm_ids)

    logger.info(
        "retry_completed",
        total_retried=len(norm_ids),
        recovered=recovered,
        still_failed=still_failed,
        recovery_rate=f"{(recovered / len(norm_ids) * 100):.1f}%",
    )


def main():
    parser = argparse.ArgumentParser(description="Retry failed norms")
    parser.add_argument(
        "--instance-id",
        required=True,
        help="Instance identifier"
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="Only retry norms with fewer than this many attempts (default: 5)"
    )
    parser.add_argument(
        "--s3-bucket",
        default="jurispeed-bcn-legal-docs",
        help="S3 bucket name"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the norms that would be retried without scraping them"
    )

    args = parser.parse_args()

    # Load config
    config = Config.from_env()

    # Run retry
    asyncio.run(retry_failed_norms(
        config=config,
        instance_id=args.instance_id,
        s3_bucket=args.s3_bucket,
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
