#!/usr/bin/env python3
"""
Production indexer with checkpoint + resume + daily quota management.

Reads pre-chunked documents from S3 and indexes to OpenSearch.

Usage:
    # Fresh start
    python run_indexer.py --s3-bucket jurispeed-bcn-legal-docs --instance-id indexer-1

    # Resume from checkpoint
    python run_indexer.py --resume --instance-id indexer-1

    # Test with limit
    python run_indexer.py --limit 100 --instance-id test-indexer

    # Index specific norms only (e.g. after re-scraping them)
    python run_indexer.py --norm-id 242302 --instance-id reindex-1
"""

import asyncio
import argparse
import structlog
import sys
from pathlib import Path
from typing import List, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import Config
from pipeline.indexer import ProductionIndexer
from pipeline.checkpoint import CheckpointManager

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


class ProductionIndexerRunner:
    """
    Production-ready indexer runner with checkpoint + daily quota management.

    Features:
    - Checkpoint every N documents
    - Resume from last checkpoint
    - Daily quota tracking (16.2M tokens/day)
    - Rate limiting
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
        self.s3_bucket = s3_bucket

        # Initialize indexer
        self.indexer = ProductionIndexer(
            config=config,
            s3_bucket=s3_bucket,
        )

        # Checkpoint manager
        self.checkpoint_mgr = CheckpointManager(config.aws, instance_id)

        # Daily quota tracking (16.2M tokens/day for Bedrock Cohere v4)
        self.daily_quota = 16_200_000  # tokens/day
        self.tokens_used_today = 0

        logger.info(
            "indexer_runner_initialized",
            instance_id=instance_id,
            s3_bucket=s3_bucket,
            daily_quota=self.daily_quota,
        )

    def estimate_tokens(self, num_chunks: int) -> int:
        """
        Estimate tokens for a batch of chunks.

        Args:
            num_chunks: Number of chunks to process

        Returns:
            Estimated tokens (chunks × 512 tokens/chunk)
        """
        return num_chunks * 512

    def can_process(self, num_chunks: int) -> bool:
        """
        Check if we can process more chunks within daily quota.

        Args:
            num_chunks: Number of chunks to process

        Returns:
            True if within quota
        """
        estimated_tokens = self.estimate_tokens(num_chunks)
        return (self.tokens_used_today + estimated_tokens) <= self.daily_quota

    def run_indexing(
        self,
        prefix: str = None,
        limit: int = None,
        checkpoint_every: int = 100,
        s3_keys: Optional[List[str]] = None,
    ) -> None:
        """
        Run indexing with checkpoint and quota management.

        Args:
            prefix: S3 prefix to list documents (default: normativabcn/originals/)
            limit: Max documents to process (optional)
            checkpoint_every: Save checkpoint every N docs
            s3_keys: Explicit keys to index; skips listing the prefix
        """
        try:
            logger.info(
                "indexing_started",
                instance_id=self.instance_id,
                s3_bucket=self.s3_bucket,
                prefix=prefix,
                limit=limit,
                explicit_keys=len(s3_keys) if s3_keys else 0,
            )

            # List documents from S3 unless specific keys were requested
            if not s3_keys:
                s3_keys = self.indexer.list_documents_from_s3(prefix)

            if limit:
                s3_keys = s3_keys[:limit]

            total_docs = len(s3_keys)

            logger.info(
                "documents_to_index",
                total=total_docs,
                quota_available=self.daily_quota - self.tokens_used_today,
            )

            # Process documents
            success_count = 0
            failed_count = 0

            for i, s3_key in enumerate(s3_keys, 1):
                # Check daily quota before processing
                # Estimate: avg 2.49 chunks/doc × 512 tokens = ~1,275 tokens/doc
                if not self.can_process(3):  # conservative estimate
                    logger.warning(
                        "daily_quota_reached",
                        tokens_used=self.tokens_used_today,
                        daily_quota=self.daily_quota,
                        docs_processed=i - 1,
                        docs_remaining=total_docs - i + 1,
                    )
                    break

                # Index document
                result = self.indexer.index_document_from_s3(s3_key)

                if result["success"]:
                    success_count += 1
                    # Update tokens used
                    chunks_processed = result.get("chunks", 0)
                    self.tokens_used_today += self.estimate_tokens(chunks_processed)
                else:
                    failed_count += 1

                # Checkpoint every N docs
                if i % checkpoint_every == 0:
                    self.checkpoint_mgr.save(
                        i,
                        {
                            "success": success_count,
                            "failed": failed_count,
                            "tokens_used_today": self.tokens_used_today,
                        },
                    )

                    progress_pct = (i / total_docs) * 100

                    logger.info(
                        "checkpoint_saved",
                        instance_id=self.instance_id,
                        processed=i,
                        total=total_docs,
                        progress_pct=f"{progress_pct:.1f}%",
                        success_rate=f"{(success_count / i) * 100:.1f}%",
                        tokens_used=self.tokens_used_today,
                    )

            # Final stats
            final_stats = self.indexer.get_stats()
            self.checkpoint_mgr.mark_completed(final_stats)

            logger.info(
                "indexing_completed",
                instance_id=self.instance_id,
                final_stats=final_stats,
                tokens_used=self.tokens_used_today,
            )

        except KeyboardInterrupt:
            logger.warning("indexing_interrupted_by_user", instance_id=self.instance_id)
            # Save checkpoint before exiting
            self.checkpoint_mgr.save(
                i,
                {
                    "success": success_count,
                    "failed": failed_count,
                    "tokens_used_today": self.tokens_used_today,
                },
            )
            raise

        except Exception as e:
            logger.error(
                "indexing_failed",
                instance_id=self.instance_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            self.checkpoint_mgr.mark_failed(str(e))
            raise

        finally:
            # Cleanup
            self.indexer.close()

    def get_resume_point(self) -> int:
        """
        Get last checkpoint to resume from.

        Returns:
            Last processed document index or 0 if no checkpoint
        """
        checkpoint = self.checkpoint_mgr.load()

        if checkpoint:
            last_idx = checkpoint.get("last_id_processed", 0)
            tokens_used = checkpoint.get("tokens_used_today", 0)

            logger.info(
                "resume_from_checkpoint",
                instance_id=self.instance_id,
                last_idx=last_idx,
                tokens_used_today=tokens_used,
                total_processed=checkpoint.get("total_processed"),
            )

            self.tokens_used_today = tokens_used
            return last_idx

        logger.info("no_checkpoint_found", instance_id=self.instance_id)
        return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="BCN Legal Norms Indexer with Checkpoint & Quota Management"
    )

    parser.add_argument(
        "--s3-bucket",
        type=str,
        default="jurispeed-bcn-legal-docs",
        help="S3 bucket with documents (default: jurispeed-bcn-legal-docs)",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        help="S3 prefix to list documents (default: normativabcn/originals/)",
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
        "--limit",
        type=int,
        help="Max number of documents to process (for testing)",
    )
    parser.add_argument(
        "--norm-id",
        type=int,
        nargs="+",
        help="Index only these norm IDs (resolved to S3 keys), instead of the whole prefix",
    )
    parser.add_argument(
        "--s3-key",
        type=str,
        nargs="+",
        help="Index only these exact S3 keys, instead of the whole prefix",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        help="Save checkpoint every N documents (default: 100)",
    )

    args = parser.parse_args()

    # Load config
    try:
        config = Config.from_env()
        config.validate_required()
    except ValueError as e:
        logger.error("config_validation_failed", error=str(e))
        sys.exit(1)

    # Initialize runner
    runner = ProductionIndexerRunner(
        config=config,
        instance_id=args.instance_id,
        s3_bucket=args.s3_bucket,
    )

    # Determine start point
    start_idx = 0
    if args.resume:
        start_idx = runner.get_resume_point()
        logger.info(
            "resuming_indexing",
            instance_id=args.instance_id,
            start_idx=start_idx,
        )

    # Resolve explicit targets, if any. Keys are built the same way the scraper
    # builds them, so --norm-id and --s3-key are interchangeable.
    s3_keys = list(args.s3_key) if args.s3_key else []
    if args.norm_id:
        s3_keys += [
            f"{config.lexintel.knowledge_id}/originals/bcn-{norm_id}.json"
            for norm_id in args.norm_id
        ]

    # Run indexer
    try:
        runner.run_indexing(
            prefix=args.prefix,
            limit=args.limit,
            checkpoint_every=args.checkpoint_every,
            s3_keys=s3_keys or None,
        )
        logger.info("indexer_finished_successfully", instance_id=args.instance_id)
        sys.exit(0)

    except KeyboardInterrupt:
        logger.warning("indexer_interrupted", instance_id=args.instance_id)
        sys.exit(130)

    except Exception as e:
        logger.error(
            "indexer_failed",
            instance_id=args.instance_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
