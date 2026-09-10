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

    # Scrape an explicit, non-contiguous list of norm IDs (one per line)
    python run_scraper.py --ids-file deployment/decretos_ids.txt --instance-id decretos-1
"""

import asyncio
import argparse
import os
import re
import structlog
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import Config
from core.scraper import BCNPlaywrightScraper
from core.xml_parser import BCNXMLParser
from pipeline.checkpoint import CheckpointManager  # Direct import, bypasses __init__
from pipeline.norm_tracker import NormTracker  # Direct import, bypasses __init__
from pipeline.chunker import ProfessionalChunker
from storage.s3_client import S3Storage  # Direct import, bypasses __init__
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
        skip_ids: set[int] | None = None,
    ):
        self.config = config
        self.instance_id = instance_id
        self.skip_ids = skip_ids or set()

        self.scraper = BCNPlaywrightScraper(config.scraper)
        self.xml_parser = BCNXMLParser()
        self.chunker = ProfessionalChunker(
            target_chunk_size=512,
            article_max_size=7500,  # Safety margin for embeddings + context headers
            overlap_tokens=200
        )
        self.checkpoint_mgr = CheckpointManager(config.aws, instance_id)
        self.norm_tracker = NormTracker(config.aws)
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
            skip_ids_count=len(self.skip_ids),
            chunking="enabled"
        )

    async def scrape_with_storage(
        self,
        norm_ids: list[int],
    ) -> None:
        """
        Scrape a list of norm IDs with S3 storage and checkpointing.

        The list does not need to be contiguous: a range of IDs and an
        explicit, scattered list (e.g. all decretos matched by type) are
        both just a `list[int]` here. The checkpoint stores the *position*
        in the list rather than the norm_id itself, since a non-contiguous
        list has no "next id" to resume from.

        Args:
            norm_ids: Norm IDs to scrape, in the order they should be processed
        """
        idx = -1
        total = len(norm_ids)
        try:
            await self.scraper.start()

            logger.info(
                "scraping_started",
                instance_id=self.instance_id,
                total_ids=total,
            )

            for idx, norm_id in enumerate(norm_ids):
                # Skip if already scraped (DISABLED - starting fresh)
                # if norm_id in self.skip_ids:
                #     logger.debug("skipping_already_scraped", norm_id=norm_id)
                #     continue

                norm = await self.scraper.scrape_one(norm_id)

                if norm:
                    # Fetch XML again for chunking (necessary for subestructura)
                    xml_content = await self.scraper._fetch_xml(norm_id, timeout=30)

                    # Detect if this is a texto refundido and mark originals
                    self._detect_and_mark_refundicion(norm, xml_content)

                    await self._store_norm(norm, xml_content)
                else:
                    error_reason = self.scraper.last_error or "Unknown error"
                    self.norm_tracker.mark_failed(norm_id, error_reason)

                if (idx + 1) % self.config.scraper.checkpoint_every == 0:
                    stats = self.scraper.get_stats()
                    self.checkpoint_mgr.save(idx, stats, metadata={"last_norm_id": norm_id})

                    progress_pct = ((idx + 1) / total) * 100

                    logger.info(
                        "checkpoint_saved",
                        instance_id=self.instance_id,
                        current_id=norm_id,
                        progress=f"{idx + 1}/{total}",
                        progress_pct=f"{progress_pct:.1f}%",
                        success_rate=f"{stats.success_rate:.1f}%",
                    )

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
            if idx >= 0:
                stats = self.scraper.get_stats()
                self.checkpoint_mgr.save(idx, stats, metadata={"last_norm_id": norm_ids[idx]})
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
            await self.scraper.close()

    async def process_norm_data(self, norm: ChileanLegalNorm, xml_content: str = None) -> dict:
        """
        Process norm with chunking and return complete data dict.

        Args:
            norm: Validated legal norm
            xml_content: Raw XML for chunking (optional but recommended)

        Returns:
            Complete data dict with chunks ready for storage
        """
        chunks = []
        total_articles = 0
        vigentes = 0

        if xml_content:
            try:
                # Extract article metadata from XML
                hierarchy = self.xml_parser.extract_article_hierarchy(xml_content, norm.norm_id)
                article_texts = self.xml_parser.extract_article_texts(xml_content)

                total_articles = len(hierarchy)
                vigentes = sum(1 for info in hierarchy.values() if info.get('in_force', True))

                # Extract global in_force status
                norm_in_force = self.xml_parser.extract_norm_vigencia(xml_content)

                # Chunk with XML data
                metadata = {
                    "norm_id": norm.norm_id,
                    "norm_type": norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type,
                    "norm_number": norm.norm_number,
                    "norm_title": norm.title,
                    "official_url": str(norm.official_url),
                    "norm_citation": norm.formal_citation,
                    "in_force": norm_in_force,
                    "common_name": norm.common_name,
                }

                chunks = self.chunker.chunk(
                    norm.full_content,
                    metadata=metadata,
                    xml_content=xml_content,
                    norm_id=norm.norm_id
                )

                logger.info(
                    "chunking_complete",
                    norm_id=norm.norm_id,
                    total_chunks=len(chunks),
                    total_articles=total_articles,
                    vigentes=vigentes
                )

            except Exception as e:
                logger.warning(
                    "chunking_failed",
                    norm_id=norm.norm_id,
                    error=str(e),
                    note="Falling back to raw norm only"
                )

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
            "common_name": norm.common_name,
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
            data["vigentes"] = vigentes
        else:
            data["chunks"] = []
            data["total_chunks"] = 0

        return data

    def _detect_and_mark_refundicion(self, norm: ChileanLegalNorm, xml_content: str):
        """
        Detect if this norm is a texto refundido and mark original as refundida.

        Checks if title contains "TEXTO REFUNDIDO" and extracts which law it refunds
        from <Materia> tags in XML.

        Args:
            norm: The current norm being processed
            xml_content: Raw XML content
        """
        title_upper = norm.title.upper()
        is_refundido = any(phrase in title_upper for phrase in [
            "TEXTO REFUNDIDO",
            "FIJA TEXTO REFUNDIDO",
            "FIJA EL TEXTO REFUNDIDO"
        ])

        if not is_refundido or not xml_content:
            return

        logger.info(
            "texto_refundido_detected",
            norm_id=norm.norm_id,
            title=norm.title[:100]
        )

        # Extract original law number from <Materia>
        # Example: <Materia>Ley no. 18.290</Materia>
        materia_pattern = r'<Materia>Ley\s+no?\.\s*(\d+[\.\d]*)</Materia>'
        matches = re.findall(materia_pattern, xml_content, re.IGNORECASE)

        if not matches:
            logger.warning(
                "refundido_no_materia_found",
                norm_id=norm.norm_id,
                title=norm.title[:100]
            )
            return

        # Get unique law numbers
        law_numbers = list(set(matches))

        logger.info(
            "refundicion_laws_extracted",
            norm_id=norm.norm_id,
            law_numbers=law_numbers
        )

        # Cleanup script will resolve law_number -> norm_id later
        for law_num in law_numbers:
            law_num_clean = law_num.replace(".", "")

            try:
                # Cleanup script will query BCN API to get norm_id
                self.norm_tracker.mark_refundida(
                    refundido_por=norm.norm_id,
                    refundida_law_number=law_num_clean,
                    reason=f"Detectado: TEXTO REFUNDIDO en {norm.title[:80]}"
                )

                logger.info(
                    "refundicion_stored",
                    refundido_por=norm.norm_id,
                    refundida_law_number=law_num_clean
                )

            except Exception as e:
                logger.error(
                    "refundicion_storage_error",
                    refundido_por=norm.norm_id,
                    law_num=law_num,
                    error=str(e)
                )

    async def _store_norm(self, norm: ChileanLegalNorm, xml_content: str = None) -> None:
        """
        Process and upload norm to S3.

        Args:
            norm: Validated legal norm
            xml_content: Raw XML for chunking
        """
        data = await self.process_norm_data(norm, xml_content)

        doc_id = f"bcn-{norm.norm_id}"
        key = self.s3_storage.build_key(
            knowledge_id=self.config.lexintel.knowledge_id,
            doc_id=doc_id,
            prefix="originals",
        )

        metadata = {
            "instance_id": self.instance_id,
            "source": "bcn-scraper-xml",
            "has_chunks": str(data.get("total_chunks", 0) > 0).lower()
        }

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
        Get last checkpoint position to resume from.

        Returns:
            Last processed position (norm_id for range mode, list index for
            --ids-file mode) or None if no checkpoint
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


def load_skip_ids(file_path: str = "deployment/priority_norms.txt") -> set[int]:
    """
    Load IDs to skip from file (already scraped).

    Args:
        file_path: Path to file with one ID per line

    Returns:
        Set of IDs to skip
    """
    skip_ids = set()

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and line.isdigit():
                    skip_ids.add(int(line))

        logger.info(
            "skip_ids_loaded",
            file=file_path,
            count=len(skip_ids),
        )
    except FileNotFoundError:
        logger.warning("skip_file_not_found", file=file_path)
    except Exception as e:
        logger.error("skip_file_load_error", file=file_path, error=str(e))

    return skip_ids


def load_norm_ids(file_path: str) -> list[int]:
    """
    Load an explicit, ordered list of norm IDs to scrape (one per line).

    Used for type-based batches (e.g. all decretos matched ahead of time)
    where the IDs are scattered across the whole BCN ID space, so a
    --start/--end range would waste most of the run on other norm types.

    Args:
        file_path: Path to file with one norm ID per line

    Returns:
        Norm IDs in file order, de-duplicated
    """
    seen = set()
    norm_ids = []

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and line.isdigit():
                norm_id = int(line)
                if norm_id not in seen:
                    seen.add(norm_id)
                    norm_ids.append(norm_id)

    logger.info("norm_ids_loaded", file=file_path, count=len(norm_ids))
    return norm_ids


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
        help="Ending norm ID (inclusive). Required unless --ids-file is given.",
    )
    parser.add_argument(
        "--ids-file",
        type=str,
        help="File with an explicit, one-per-line list of norm IDs to scrape "
             "(e.g. a type-based batch). Mutually exclusive with --start/--end.",
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
        "--skip-file",
        type=str,
        default="deployment/priority_norms.txt",
        help="File with IDs to skip (default: deployment/priority_norms.txt)",
    )

    args = parser.parse_args()

    if args.ids_file and (args.start or args.end):
        logger.error(
            "conflicting_arguments",
            message="--ids-file is mutually exclusive with --start/--end",
        )
        sys.exit(1)
    if not args.ids_file and not args.end:
        logger.error(
            "missing_range_or_ids_file",
            message="Provide --end (range mode) or --ids-file (explicit list mode)",
        )
        sys.exit(1)

    # DISABLED: Starting fresh, no skip list
    # skip_ids = load_skip_ids(args.skip_file)
    skip_ids = set()

    try:
        config = Config.from_env()
        config.validate_required()
    except ValueError as e:
        logger.error("config_validation_failed", error=str(e))
        sys.exit(1)

    s3_bucket = args.s3_bucket or os.getenv("S3_BUCKET_NAME")
    if not s3_bucket:
        logger.error("s3_bucket_required", message="Provide --s3-bucket or set S3_BUCKET_NAME env var")
        sys.exit(1)

    scraper = ProductionScraper(
        config=config,
        instance_id=args.instance_id,
        s3_bucket=s3_bucket,
        skip_ids=skip_ids,
    )

    if args.ids_file:
        norm_ids = load_norm_ids(args.ids_file)
        if args.resume:
            last_idx = scraper.get_resume_point()
            if last_idx is not None:
                norm_ids = norm_ids[last_idx + 1:]
                logger.info(
                    "resuming_scraping",
                    instance_id=args.instance_id,
                    resume_from_idx=last_idx + 1,
                    remaining=len(norm_ids),
                )
            else:
                logger.info("no_checkpoint_starting_fresh", instance_id=args.instance_id)
    else:
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
                start_id = args.start or 1
                logger.info(
                    "no_checkpoint_starting_fresh",
                    instance_id=args.instance_id,
                    start_id=start_id,
                )
        else:
            start_id = args.start or 1
            logger.info(
                "starting_fresh",
                instance_id=args.instance_id,
                start_id=start_id,
            )

        end_id = args.end

        if start_id > end_id:
            logger.error(
                "invalid_range",
                start_id=start_id,
                end_id=end_id,
                message="Start ID must be <= End ID",
            )
            sys.exit(1)

        norm_ids = list(range(start_id, end_id + 1))

    try:
        await scraper.scrape_with_storage(norm_ids)
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
