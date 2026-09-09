#!/usr/bin/env python3
"""
End-to-end test: scraping → parsing → chunking (33 diverse norms).

Validates complete processing pipeline from BCN XML to chunked JSON output.
Does NOT upload to S3 (local validation only).
"""

import asyncio
import json
import sys
import structlog
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# Add src AND scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from run_scraper import ProductionScraper
from pipeline.norm_tracker import NormTracker
from utils.config import Config

logger = structlog.get_logger()


# Test norm IDs (single-norm validation run)
TEST_NORM_IDS = [242302]


async def test_norm(
    norm_id: int,
    production_scraper: ProductionScraper,
    tracker: NormTracker,
    output_dir: Path
) -> Optional[Dict]:
    """Test one norm using production code path."""
    result = {
        "norm_id": norm_id,
        "status": "pending",
        "error": None,
    }

    try:
        norm = await production_scraper.scraper.scrape_one(norm_id)

        if not norm:
            result["status"] = "failed"
            result["error"] = "XML scraping failed after all retries (no HTML fallback)"
            tracker.mark_failed(norm_id, result["error"])
            return result

        xml_content = await production_scraper.scraper._fetch_xml(norm_id, timeout=30)

        if xml_content:
            xml_dir = output_dir / "xmls"
            xml_dir.mkdir(exist_ok=True)
            xml_path = xml_dir / f"norm_{norm_id}.xml"
            with open(xml_path, "w", encoding="utf-8") as f:
                f.write(xml_content)

        data = await production_scraper.process_norm_data(norm, xml_content)

        # Extract stats for result
        result["status"] = "success"
        result["source"] = data["source"]
        result["norm_type"] = data["norm_type"]
        result["norm_number"] = data["norm_number"]
        result["norm_title"] = norm.title[:100] + "..." if len(norm.title) > 100 else norm.title
        result["content_length"] = len(norm.full_content)
        result["total_chunks"] = data["total_chunks"]
        result["total_articles"] = data.get("total_articles", 0)
        result["vigentes"] = data.get("vigentes", 0)
        result["derogados"] = result["total_articles"] - result["vigentes"]

        if data["chunks"]:
            result["avg_tokens"] = sum(c["token_count"] for c in data["chunks"]) / len(data["chunks"])
            result["nested_chunks"] = sum(1 for c in data["chunks"] if c["metadata"].get("is_nested"))
        else:
            result["avg_tokens"] = 0
            result["nested_chunks"] = 0

        result["chunking"] = "success"
        result["articles_with_text"] = result["total_articles"]

        if xml_content:
            result["xml_file"] = f"xmls/norm_{norm_id}.xml"

        # Save chunks JSON (same structure as production)
        json_dir = output_dir / "chunks"
        json_dir.mkdir(exist_ok=True)
        json_path = json_dir / f"bcn-{norm_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        result["json_file"] = f"chunks/bcn-{norm_id}.json"

        tracker.mark_success(
            norm_id=norm_id,
            source="xml",
            total_chunks=result["total_chunks"],
            total_articles=result["total_articles"]
        )

        return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)}"
        return result


async def main():
    """Test diverse norms with XML-only pipeline using production code."""
    print("\n" + "=" * 80)
    print("TESTING XML-ONLY PIPELINE WITH DIVERSE NORMS")
    print("=" * 80)
    print(f"Total norms: {len(TEST_NORM_IDS)}")
    print(f"Code path: ProductionScraper")
    print()

    output_dir = Path(__file__).parent / "test_diverse_output"
    output_dir.mkdir(exist_ok=True)
    print(f"Output directory: {output_dir}")
    print()

    config = Config.from_env()
    production_scraper = ProductionScraper(
        config=config,
        instance_id="test-run",
        s3_bucket="test-bucket",  # Not used in test, but required
        skip_ids=set()
    )
    tracker = NormTracker(config.aws)

    print("Initializing DynamoDB tracking...")
    tracker.mark_pending(TEST_NORM_IDS)
    print()

    results = []

    try:
        await production_scraper.scraper.start()

        # Test each norm
        for i, norm_id in enumerate(TEST_NORM_IDS, 1):
            print(f"[{i}/{len(TEST_NORM_IDS)}] Testing norm {norm_id}...", end=" ", flush=True)

            result = await test_norm(norm_id, production_scraper, tracker, output_dir)
            results.append(result)

            if result["error"]:
                print(f"FAILED - {result['error']}")
            else:
                status = f"{result['norm_type']} {result['norm_number']}, {result['total_chunks']} chunks"
                print(f"OK - {status}")

            # Rate limiting (conservative to avoid ban)
            import random
            wait_time = random.uniform(10, 15)  # 10-15 seconds between requests (increased from 5-8)

            # Extra pause every 10 requests
            if i % 10 == 0:
                print(f"\n  [PAUSE] Cooling down 60 seconds after {i} requests...")
                await asyncio.sleep(60)  # Increased from 30s to 60s

            await asyncio.sleep(wait_time)

        # Summary statistics
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)

        successful = [r for r in results if r["error"] is None]
        failed = [r for r in results if r["error"] is not None]

        print(f"\nSuccess rate: {len(successful)}/{len(results)} ({len(successful)*100/len(results):.1f}%)")

        if failed:
            print(f"\nFailed norms ({len(failed)}):")
            for r in failed:
                print(f"  - {r['norm_id']}: {r['error']}")

        if successful:
            print(f"\nSuccessful norms ({len(successful)}):")

            # Aggregate statistics
            total_articles = sum(r["total_articles"] for r in successful)
            total_chunks = sum(r["total_chunks"] for r in successful)
            total_vigentes = sum(r["vigentes"] for r in successful)
            total_derogados = sum(r["derogados"] for r in successful)

            print(f"  Total articles: {total_articles:,}")
            print(f"  Total chunks: {total_chunks:,}")
            print(f"  Avg chunks per norm: {total_chunks/len(successful):.1f}")
            print(f"  Total vigentes: {total_vigentes:,} ({total_vigentes/(total_vigentes+total_derogados)*100:.1f}%)")
            print(f"  Total derogados: {total_derogados:,} ({total_derogados/(total_vigentes+total_derogados)*100:.1f}%)")

            # Distribution by norm type
            print(f"\nDistribution by norm type:")
            norm_types = {}
            for r in successful:
                nt = r["norm_type"]
                if nt not in norm_types:
                    norm_types[nt] = {"count": 0, "chunks": 0, "articles": 0}
                norm_types[nt]["count"] += 1
                norm_types[nt]["chunks"] += r["total_chunks"]
                norm_types[nt]["articles"] += r["total_articles"]

            for nt, stats in sorted(norm_types.items()):
                print(f"  {nt}: {stats['count']} norms, {stats['articles']} articles, {stats['chunks']} chunks")

            # Token statistics
            all_avg_tokens = [r["avg_tokens"] for r in successful]
            min_avg = min(all_avg_tokens)
            max_avg = max(all_avg_tokens)
            overall_avg = sum(all_avg_tokens) / len(all_avg_tokens)
            print(f"\nToken statistics (avg per chunk):")
            print(f"  Min: {min_avg:.0f}")
            print(f"  Max: {max_avg:.0f}")
            print(f"  Overall avg: {overall_avg:.0f}")

            # Top 5 by chunks
            print(f"\nTop 5 by chunk count:")
            top_5 = sorted(successful, key=lambda r: r["total_chunks"], reverse=True)[:5]
            for r in top_5:
                print(f"  {r['norm_id']}: {r['norm_type']} {r['norm_number']} - {r['total_chunks']} chunks")

        # Save detailed results summary
        summary_path = output_dir / "test_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump({
                "test_date": datetime.now().isoformat(),
                "total_tested": len(results),
                "successful": len(successful),
                "failed": len(failed),
                "results": results
            }, f, indent=2, ensure_ascii=False)

        print(f"\nTest summary saved: {summary_path}")
        print(f"Output directory structure:")
        print(f"  {output_dir}/")
        print(f"    xmls/          - Raw XML files from BCN")
        print(f"    chunks/        - Chunked JSON outputs")
        print(f"    test_summary.json - Test results summary")

        # Get DynamoDB tracking stats
        print(f"\n{'='*80}")
        print("DYNAMODB TRACKING STATUS")
        print(f"{'='*80}")
        try:
            db_stats = tracker.get_stats()
            print(f"Total tracked: {db_stats['total']}")
            print(f"Successful: {db_stats['success']} ({db_stats['success_rate']})")
            print(f"Failed: {db_stats['failed']}")
            print(f"Pending: {db_stats['pending']}")

            if db_stats.get('failure_reasons'):
                print(f"\nFailure reasons:")
                for reason, count in db_stats['failure_reasons'].items():
                    print(f"  - {reason}: {count}")

            print(f"\nTo retry failed norms:")
            print(f"  python scripts/retry_failed_norms.py")
        except Exception as e:
            print(f"[Warning] Could not fetch DynamoDB stats: {e}")

        print("\n" + "=" * 80)
        if len(successful) == len(results):
            print("ALL TESTS PASSED")
        else:
            print(f"TESTS COMPLETED WITH {len(failed)} FAILURES")
        print("=" * 80)

        return 0 if len(successful) == len(results) else 1

    except Exception as e:
        print(f"\n[ERROR] Test suite failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        await production_scraper.scraper.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
