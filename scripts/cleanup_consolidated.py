#!/usr/bin/env python3
"""
Cleanup script to remove consolidated norms from S3.

Run ONCE after all scraper instances complete.

This script:
1. Queries DynamoDB for all detected consolidations (status='refundida')
2. For each law number, queries BCN API to get norm_id
3. Shows what will be deleted (for review)
4. Asks for confirmation
5. Deletes corresponding documents from S3

Usage:
    python scripts/cleanup_consolidated.py
    python scripts/cleanup_consolidated.py --auto-confirm  # Skip confirmation prompt
"""

import sys
import argparse
import asyncio
import aiohttp
import re
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pipeline.norm_tracker import NormTracker, NormStatus
from storage.s3_storage import S3Storage
from utils.config import Config
import structlog

logger = structlog.get_logger()


async def resolve_law_number_to_norm_id(law_number: str) -> int | None:
    """
    Query BCN API to convert law number to norm_id.

    Uses: obtxml?opt=7&idLey={law_number}
    Extracts normaId attribute from XML response.

    Args:
        law_number: Law number (e.g., "18290", "20000")

    Returns:
        norm_id if found, None if not found or error
    """
    url = f"http://www.leychile.cl/Consulta/obtxml?opt=7&idLey={law_number}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    logger.error(
                        "bcn_api_error",
                        law_number=law_number,
                        status=response.status
                    )
                    return None

                xml_text = await response.text()

                # Extract normaId from XML
                # Example: <Norma ... normaId="29708" ...>
                match = re.search(r'normaId="(\d+)"', xml_text)
                if match:
                    norm_id = int(match.group(1))
                    logger.info(
                        "law_number_resolved",
                        law_number=law_number,
                        norm_id=norm_id
                    )
                    return norm_id
                else:
                    logger.warning(
                        "normaId_not_found_in_xml",
                        law_number=law_number
                    )
                    return None

    except asyncio.TimeoutError:
        logger.error("bcn_api_timeout", law_number=law_number)
        return None
    except Exception as e:
        logger.error(
            "law_number_resolution_failed",
            law_number=law_number,
            error=str(e)
        )
        return None


async def resolve_all_consolidated(consolidations: list) -> list:
    """
    Resolve all law numbers to norm_ids.

    Args:
        consolidations: List of consolidation items from DynamoDB

    Returns:
        List of dicts with resolved norm_ids
    """
    resolved = []

    for item in consolidations:
        law_number = item.get("refundida_law_number")
        refundido_por = item.get("refundido_por")

        if not law_number:
            logger.warning("missing_law_number", item=item)
            continue

        # Resolve law number to norm_id
        norm_id = await resolve_law_number_to_norm_id(law_number)

        if norm_id:
            resolved.append({
                "norm_id": norm_id,
                "law_number": law_number,
                "refundido_por": refundido_por,
                "reason": item.get("reason", "N/A")
            })
        else:
            logger.warning(
                "could_not_resolve_law_number",
                law_number=law_number,
                refundido_por=refundido_por
            )

        # Rate limiting: small delay between API calls
        await asyncio.sleep(0.5)

    return resolved


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup consolidated norms from S3 after scraping"
    )
    parser.add_argument(
        "--auto-confirm",
        action="store_true",
        help="Skip confirmation prompt (use with caution)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting"
    )
    args = parser.parse_args()

    # Load config
    config = Config.from_env()
    tracker = NormTracker(config.aws)
    s3 = S3Storage(
        bucket_name=config.aws.s3_bucket_name,
        config=config.aws
    )

    print("=" * 80)
    print("CLEANUP CONSOLIDATED NORMS FROM S3")
    print("=" * 80)
    print()

    # Query consolidations from DynamoDB
    print("Querying consolidations from DynamoDB...")
    consolidations = tracker.get_all_by_status(NormStatus.REFUNDIDA)

    if not consolidations:
        print("No consolidations found. Nothing to cleanup.")
        return 0

    print(f"Found {len(consolidations)} consolidations detected during scraping")
    print()

    # Resolve law numbers to norm_ids via BCN API
    print("Resolving law numbers to norm_ids via BCN API...")
    print("(This may take a few minutes...)")
    print()

    consolidated_norms = asyncio.run(resolve_all_consolidated(consolidations))

    if not consolidated_norms:
        print("Could not resolve any law numbers to norm_ids.")
        print("Check logs for API errors.")
        return 1

    print(f"Successfully resolved {len(consolidated_norms)} out of {len(consolidations)} consolidations")
    print()

    # Show summary
    print("Norms that will be deleted from S3:")
    print("-" * 90)
    print(f"{'norm_id':<10} {'law_num':<10} {'consolidated_by':<15} {'reason'}")
    print("-" * 90)

    for norm in consolidated_norms:
        norm_id = norm["norm_id"]
        law_number = norm.get("law_number", "N/A")
        refundido_por = norm.get("refundido_por", "N/A")
        reason = norm.get("reason", "No reason")
        # Truncate reason if too long
        reason_display = reason[:40] + "..." if len(reason) > 40 else reason
        print(f"{norm_id:<10} {law_number:<10} {refundido_por:<15} {reason_display}")

    print("-" * 90)
    print()

    # Dry run mode
    if args.dry_run:
        print(f"[DRY RUN] Would delete {len(consolidated_norms)} documents from S3")
        print("Run without --dry-run to actually delete")
        return 0

    # Confirmation
    if not args.auto_confirm:
        confirm = input(f"Proceed with deletion of {len(consolidated_norms)} documents? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return 1

    # Delete from S3
    print()
    print("Deleting from S3...")
    deleted = 0
    failed = 0
    errors = []

    for norm in consolidated_norms:
        norm_id = norm["norm_id"]
        key = s3.build_key(
            knowledge_id=config.lexintel.knowledge_id,
            doc_id=f"bcn-{norm_id}",
            prefix="originals"
        )

        try:
            s3.delete_object(key)
            deleted += 1
            print(f"  Deleted: {norm_id} ({key})")

        except Exception as e:
            failed += 1
            error_msg = f"Failed to delete {norm_id}: {str(e)}"
            errors.append(error_msg)
            print(f"  ERROR: {error_msg}")
            logger.error("s3_delete_failed", norm_id=norm_id, error=str(e))

    # Summary
    print()
    print("=" * 80)
    print("CLEANUP SUMMARY")
    print("=" * 80)
    print(f"Total consolidated norms found: {len(consolidated_norms)}")
    print(f"Successfully deleted: {deleted}")
    print(f"Failed: {failed}")
    print()

    if failed > 0:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")
        print()

    # Calculate final counts
    original_count = 411000  # Approximate total norms
    final_count = original_count - deleted
    print(f"Estimated S3 documents after cleanup: {final_count:,}")
    print()

    logger.info(
        "cleanup_complete",
        total_consolidated=len(consolidated_norms),
        deleted=deleted,
        failed=failed
    )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
