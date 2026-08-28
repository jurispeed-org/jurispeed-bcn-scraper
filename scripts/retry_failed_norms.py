#!/usr/bin/env python3
"""
Retry failed norms from DynamoDB tracking table.

Queries jurispeed-norm-status for failed norms and retries them.

Usage:
    python scripts/retry_failed_norms.py --instance-id retry-1
    python scripts/retry_failed_norms.py --instance-id retry-1 --max-attempts 5
"""

import asyncio
import argparse
import structlog
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import Config
from core.scraper import BCNPlaywrightScraper
from core.xml_parser import BCNXMLParser
from pipeline.norm_tracker import NormTracker
from pipeline.chunker import ProfessionalChunker
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


async def retry_failed_norms(
    config: Config,
    instance_id: str,
    s3_bucket: str,
    max_attempts: int = 5
):
    """
    Retry all failed norms from DynamoDB.

    Args:
        config: Configuration
        instance_id: Instance identifier for logging
        s3_bucket: S3 bucket name
        max_attempts: Max retry attempts per norm
    """
    # Initialize components
    scraper = BCNPlaywrightScraper(config.scraper)
    xml_parser = BCNXMLParser()
    chunker = ProfessionalChunker(
        target_chunk_size=512,
        article_max_size=8192,
        overlap_tokens=200
    )
    tracker = NormTracker(config.aws)
    s3_storage = S3Storage(
        bucket_name=s3_bucket,
        region=config.aws.region,
        aws_access_key_id=config.aws.access_key_id,
        aws_secret_access_key=config.aws.secret_access_key,
    )

    try:
        # Get failed norms
        logger.info("retrieving_failed_norms", max_attempts=max_attempts)
        failed_norms = tracker.get_failed(max_attempts=max_attempts)

        if not failed_norms:
            logger.info("no_failed_norms_to_retry")
            return

        logger.info(
            "retry_starting",
            total_failed=len(failed_norms),
            instance_id=instance_id
        )

        await scraper.start()

        # Retry each failed norm
        success_count = 0
        still_failed_count = 0

        for i, failed_item in enumerate(failed_norms, 1):
            norm_id = failed_item["norm_id"]
            attempts = failed_item.get("attempts", 0)

            logger.info(
                "retrying_norm",
                progress=f"{i}/{len(failed_norms)}",
                norm_id=norm_id,
                previous_attempts=attempts
            )

            # Scrape
            norm = await scraper.scrape_one(norm_id)

            if norm:
                # Success - fetch XML and do chunking
                xml_content = await scraper._fetch_xml(norm_id, timeout=30)

                chunks = []
                total_articles = 0

                if xml_content:
                    try:
                        hierarchy = xml_parser.extract_article_hierarchy(xml_content, norm_id)
                        total_articles = len(hierarchy)

                        metadata_chunk = {
                            "norm_id": norm.norm_id,
                            "norm_type": norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type,
                            "norm_number": norm.norm_number,
                            "norm_title": norm.title,
                            "official_url": str(norm.official_url)
                        }

                        chunks = chunker.chunk(
                            norm.full_content,
                            metadata=metadata_chunk,
                            xml_content=xml_content,
                            norm_id=norm_id
                        )
                    except Exception as e:
                        logger.warning("chunking_failed_retry", norm_id=norm_id, error=str(e))

                # Prepare data - EXACTLY like production output
                data = {
                    "norm_id": norm.norm_id,
                    "norm_type": norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type,
                    "norm_number": norm.norm_number,
                    "formal_citation": norm.formal_citation,
                    "title": norm.title,
                    "publication_date": norm.publication_date.isoformat() if norm.publication_date else None,
                    "official_url": str(norm.official_url),
                    "summary": norm.summary,
                    "issuing_body": norm.issuing_body,
                    "subject_tags": norm.subject_tags,
                    "source": "xml",
                    "full_content": norm.full_content,
                }

                if chunks:
                    data["chunks"] = [
                        {
                            "chunk_index": c.chunk_index,
                            "token_count": c.token_count,
                            "content": c.text,
                            "metadata": c.metadata  # Contains article_label (string) instead of article_number (int)
                        }
                        for c in chunks
                    ]
                    data["total_chunks"] = len(chunks)
                    data["total_articles"] = total_articles
                else:
                    data["chunks"] = []
                    data["total_chunks"] = 0

                # Upload to S3
                doc_id = f"bcn-{norm.norm_id}"
                key = s3_storage.build_key(
                    knowledge_id=config.lexintel.knowledge_id,
                    doc_id=doc_id,
                    prefix="originals",
                )

                metadata = {
                    "instance_id": instance_id,
                    "source": "bcn-scraper-retry",
                    "has_chunks": str(len(chunks) > 0).lower()
                }

                s3_storage.store_document(key, data, metadata)

                # Update tracker to success (removes from failed list)
                tracker.mark_success(norm_id, "xml", len(chunks), total_articles)
                success_count += 1

                logger.info(
                    "retry_success",
                    norm_id=norm_id,
                    previous_attempts=attempts,
                    chunks=len(chunks)
                )
            else:
                # Still failed - increment attempts
                error_reason = scraper.last_error or "Unknown error"
                tracker.mark_failed(norm_id, error_reason)
                still_failed_count += 1

                logger.warning(
                    "retry_failed",
                    norm_id=norm_id,
                    attempts=attempts + 1,
                    error=error_reason
                )

        # Summary
        logger.info(
            "retry_completed",
            total_retried=len(failed_norms),
            success=success_count,
            still_failed=still_failed_count,
            success_rate=f"{(success_count / len(failed_norms) * 100):.1f}%"
        )

    finally:
        await scraper.close()


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
        help="Max retry attempts per norm (default: 5)"
    )
    parser.add_argument(
        "--s3-bucket",
        default="jurispeed-bcn-legal-docs",
        help="S3 bucket name"
    )

    args = parser.parse_args()

    # Load config
    config = Config.from_env()

    # Run retry
    asyncio.run(retry_failed_norms(
        config=config,
        instance_id=args.instance_id,
        s3_bucket=args.s3_bucket,
        max_attempts=args.max_attempts
    ))


if __name__ == "__main__":
    main()
