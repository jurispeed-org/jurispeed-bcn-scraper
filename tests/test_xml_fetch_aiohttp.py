#!/usr/bin/env python3
"""
Quick test to verify aiohttp XML fetching works for previously failed norms.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.scraper import BCNPlaywrightScraper
from core.xml_parser import BCNXMLParser
from utils.config import Config


async def test_failed_norms():
    """Test XML fetching for norms that failed before."""

    # These are the 8 that failed
    failed_norms = [
        1973, 1115996, 1068465, 1062100,
        1123747, 1194869, 1063938, 1080094
    ]

    print("\n" + "=" * 70)
    print("TESTING AIOHTTP XML FETCH FOR PREVIOUSLY FAILED NORMS")
    print("=" * 70)
    print(f"Testing {len(failed_norms)} norms that failed with Playwright")
    print()

    config = Config.from_env()
    scraper = BCNPlaywrightScraper(config.scraper)
    xml_parser = BCNXMLParser()

    try:
        # Start playwright (needed for browser, even though _fetch_xml uses aiohttp now)
        await scraper.start()

        results = []

        for norm_id in failed_norms:
            print(f"Testing norm {norm_id}...", end=" ", flush=True)

            # Try to fetch XML with new aiohttp implementation
            xml = await scraper._fetch_xml(norm_id, timeout=30)

            if xml:
                # Try to parse
                norm = xml_parser.parse(xml, norm_id)
                if norm:
                    norm_type = norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type
                    print(f"SUCCESS - {norm_type} {norm.norm_number}")
                    results.append({
                        "norm_id": norm_id,
                        "status": "success",
                        "type": norm_type,
                        "number": norm.norm_number
                    })
                else:
                    print(f"PARSE FAILED")
                    results.append({"norm_id": norm_id, "status": "parse_failed"})
            else:
                print(f"FETCH FAILED")
                results.append({"norm_id": norm_id, "status": "fetch_failed"})

        print("\n" + "=" * 70)
        print("RESULTS")
        print("=" * 70)

        success = [r for r in results if r["status"] == "success"]
        fetch_failed = [r for r in results if r["status"] == "fetch_failed"]
        parse_failed = [r for r in results if r["status"] == "parse_failed"]

        print(f"\nSuccess: {len(success)}/{len(failed_norms)}")
        print(f"Fetch failed: {len(fetch_failed)}/{len(failed_norms)}")
        print(f"Parse failed: {len(parse_failed)}/{len(failed_norms)}")

        if success:
            print(f"\nRecovered norms:")
            for r in success:
                print(f"  - {r['norm_id']}: {r['type']} {r['number']}")

        if fetch_failed:
            print(f"\nStill failing (fetch):")
            for r in fetch_failed:
                print(f"  - {r['norm_id']}")

        print()
        return 0 if len(success) == len(failed_norms) else 1

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        await scraper.close()


if __name__ == "__main__":
    exit_code = asyncio.run(test_failed_norms())
    sys.exit(exit_code)
