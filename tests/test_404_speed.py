#!/usr/bin/env python3
"""
Test script to measure 404 vs valid document scraping speed.

Tests a small sample to estimate real throughput.
"""

import asyncio
import time
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

# Change to project root for .env loading
os.chdir(project_root)

from src.utils.config import Config
from src.core.scraper_playwright import BCNPlaywrightScraper


async def test_speed():
    """Test scraping speed for valid IDs vs 404s."""

    # Load config
    config = Config.from_env()
    scraper = BCNPlaywrightScraper(config.scraper)

    # Test IDs - from priority_norms.txt (known valid)
    valid_ids = [
        1206100, 1150763, 1103997, 1098002, 1096101,
        1091913, 1030861, 1030318, 1016846, 1010903,
    ]

    # IDs near 2M that likely don't exist (404s)
    # Testing gaps between known valid IDs
    invalid_ids = [
        1900000, 1900001, 1900002, 1950000, 1950001,
        1980000, 1980001, 1990000, 1990001, 1999999,
    ]

    print("\n" + "="*60)
    print("TESTING SCRAPING SPEED")
    print("="*60)

    try:
        await scraper.start()

        # Test valid IDs
        print(f"\n[1/2] Testing {len(valid_ids)} VALID IDs...")
        valid_times = []

        for norm_id in valid_ids:
            start = time.time()
            result = await scraper.scrape_one(norm_id)
            elapsed = time.time() - start
            valid_times.append(elapsed)

            status = "[SUCCESS]" if result else "[FAILED]"
            print(f"  ID {norm_id}: {elapsed:.2f}s - {status}")

        # Test invalid IDs (404s)
        print(f"\n[2/2] Testing {len(invalid_ids)} INVALID IDs (404s)...")
        invalid_times = []

        for norm_id in invalid_ids:
            start = time.time()
            result = await scraper.scrape_one(norm_id)
            elapsed = time.time() - start
            invalid_times.append(elapsed)

            status = "[404 expected]" if not result else "[UNEXPECTED SUCCESS]"
            print(f"  ID {norm_id}: {elapsed:.2f}s - {status}")

        # Calculate statistics
        print("\n" + "="*60)
        print("RESULTS")
        print("="*60)

        if valid_times:
            avg_valid = sum(valid_times) / len(valid_times)
            print(f"\nValid documents:")
            print(f"  Average time: {avg_valid:.2f}s")
            print(f"  Min: {min(valid_times):.2f}s")
            print(f"  Max: {max(valid_times):.2f}s")

        if invalid_times:
            avg_invalid = sum(invalid_times) / len(invalid_times)
            print(f"\n404s (invalid IDs):")
            print(f"  Average time: {avg_invalid:.2f}s")
            print(f"  Min: {min(invalid_times):.2f}s")
            print(f"  Max: {max(invalid_times):.2f}s")

        # Extrapolate to full scraping job
        if valid_times and invalid_times:
            print("\n" + "="*60)
            print("EXTRAPOLATION FOR 2M IDs")
            print("="*60)

            # Assumptions
            total_ids = 2_000_000
            valid_docs = 411_000
            invalid_docs = total_ids - valid_docs
            num_instances = 8

            print(f"\nAssumptions:")
            print(f"  Total IDs to scan: {total_ids:,}")
            print(f"  Valid docs: {valid_docs:,}")
            print(f"  404s: {invalid_docs:,}")
            print(f"  Instances: {num_instances}")

            # Calculate total time
            total_seconds = (valid_docs * avg_valid) + (invalid_docs * avg_invalid)
            total_seconds_per_instance = total_seconds / num_instances

            days = total_seconds_per_instance / 86400

            print(f"\nEstimated scraping time:")
            print(f"  Total work: {total_seconds:,.0f} seconds")
            print(f"  Per instance: {total_seconds_per_instance:,.0f} seconds")
            print(f"  With {num_instances} instances: {days:.1f} days")

            # Breakdown
            valid_portion = (valid_docs * avg_valid) / total_seconds * 100
            invalid_portion = (invalid_docs * avg_invalid) / total_seconds * 100

            print(f"\nTime breakdown:")
            print(f"  Valid docs: {valid_portion:.1f}% of time")
            print(f"  404s: {invalid_portion:.1f}% of time")

            # Throughput
            docs_per_day = valid_docs / days
            print(f"\nThroughput:")
            print(f"  {docs_per_day:,.0f} valid docs/day")

        print("\n" + "="*60)

    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(test_speed())
