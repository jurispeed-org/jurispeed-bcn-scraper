#!/usr/bin/env python3
"""
Multi-instance scraper launcher.

Divides 300K norm IDs into 5 ranges for parallel EC2 instances.

Usage:
    # Get range for specific instance
    python run_multi_instance.py --instance 1

    # Auto-detect instance from EC2 metadata
    python run_multi_instance.py --auto-detect
"""

import argparse
import subprocess
import sys
import os

# Total range configuration
# Based on priority_norms.txt, max ID is ~1.2M
# We'll scan up to 2M to be safe (includes 404s that will be skipped)
TOTAL_START = 1
TOTAL_END = 2000000  # Updated: scan up to 2M IDs
NUM_INSTANCES = 8    # Updated: use 8× t3.small as per estimation

# Calculate range size
RANGE_SIZE = (TOTAL_END - TOTAL_START + 1) // NUM_INSTANCES


def get_instance_range(instance_num: int) -> tuple[int, int]:
    """
    Get scraping range for specific instance.

    Args:
        instance_num: Instance number (1-5)

    Returns:
        (start_id, end_id) tuple
    """
    if instance_num < 1 or instance_num > NUM_INSTANCES:
        raise ValueError(f"Instance number must be 1-{NUM_INSTANCES}")

    start_id = TOTAL_START + (instance_num - 1) * RANGE_SIZE
    end_id = start_id + RANGE_SIZE - 1

    # Last instance gets any remainder
    if instance_num == NUM_INSTANCES:
        end_id = TOTAL_END

    return start_id, end_id


def get_ec2_instance_id() -> str | None:
    """
    Get EC2 instance ID from metadata service.

    Returns:
        Instance ID or None if not on EC2
    """
    try:
        import requests

        # EC2 metadata endpoint
        url = "http://169.254.169.254/latest/meta-data/instance-id"
        response = requests.get(url, timeout=2)

        if response.status_code == 200:
            return response.text.strip()

    except Exception:
        pass

    return None


def get_instance_number_from_tags() -> int | None:
    """
    Get instance number from EC2 tags.

    Expects tag: "ScraperInstance" = "1", "2", "3", "4", or "5"

    Returns:
        Instance number or None if not found
    """
    try:
        import boto3

        ec2_instance_id = get_ec2_instance_id()
        if not ec2_instance_id:
            return None

        # Get instance tags
        ec2 = boto3.client("ec2")
        response = ec2.describe_tags(
            Filters=[
                {"Name": "resource-id", "Values": [ec2_instance_id]},
                {"Name": "key", "Values": ["ScraperInstance"]},
            ]
        )

        if response["Tags"]:
            tag_value = response["Tags"][0]["Value"]
            return int(tag_value)

    except Exception as e:
        print(f"Error getting instance tags: {e}", file=sys.stderr)

    return None


def print_all_ranges():
    """Print all instance ranges (for planning)."""
    print("\n📊 Scraping Ranges for 5 EC2 Instances\n")
    print("=" * 60)

    total_docs = TOTAL_END - TOTAL_START + 1
    print(f"Total range: {TOTAL_START:,} - {TOTAL_END:,} ({total_docs:,} docs)")
    print(f"Instances: {NUM_INSTANCES}")
    print(f"Docs per instance: ~{RANGE_SIZE:,}")
    print("=" * 60)
    print()

    for i in range(1, NUM_INSTANCES + 1):
        start, end = get_instance_range(i)
        count = end - start + 1
        print(f"Instance {i}:")
        print(f"  Range: {start:,} - {end:,}")
        print(f"  Count: {count:,} docs")
        print(f"  Command: python run_scraper.py --start {start} --end {end} --instance-id ec2-instance-{i} --resume")
        print()


def run_scraper_for_instance(instance_num: int, resume: bool = True):
    """
    Launch scraper for specific instance.

    Args:
        instance_num: Instance number (1-5)
        resume: Whether to resume from checkpoint
    """
    start_id, end_id = get_instance_range(instance_num)
    instance_id = f"ec2-instance-{instance_num}"

    s3_bucket = os.getenv("S3_BUCKET_NAME")
    if not s3_bucket:
        print("❌ Error: S3_BUCKET_NAME environment variable not set", file=sys.stderr)
        sys.exit(1)

    print(f"🚀 Launching scraper for Instance {instance_num}")
    print(f"   Range: {start_id:,} - {end_id:,}")
    print(f"   Instance ID: {instance_id}")
    print(f"   Resume: {resume}")
    print(f"   S3 Bucket: {s3_bucket}")
    print()

    # Build command
    cmd = [
        sys.executable,
        "run_scraper.py",
        "--start", str(start_id),
        "--end", str(end_id),
        "--instance-id", instance_id,
        "--s3-bucket", s3_bucket,
    ]

    if resume:
        cmd.append("--resume")

    # Run scraper
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Scraper failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        print("\n⚠️  Scraper interrupted by user", file=sys.stderr)
        sys.exit(130)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Multi-instance scraper launcher (divides 300K across 5 EC2 instances)"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--instance",
        type=int,
        choices=range(1, NUM_INSTANCES + 1),
        help=f"Instance number (1-{NUM_INSTANCES})",
    )
    group.add_argument(
        "--auto-detect",
        action="store_true",
        help="Auto-detect instance number from EC2 tags",
    )
    group.add_argument(
        "--show-ranges",
        action="store_true",
        help="Show all instance ranges (for planning)",
    )

    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start fresh (don't resume from checkpoint)",
    )

    args = parser.parse_args()

    # Show ranges
    if args.show_ranges:
        print_all_ranges()
        sys.exit(0)

    # Determine instance number
    if args.auto_detect:
        instance_num = get_instance_number_from_tags()
        if not instance_num:
            print("❌ Error: Could not auto-detect instance number", file=sys.stderr)
            print("   Make sure EC2 instance has 'ScraperInstance' tag set to 1-5", file=sys.stderr)
            sys.exit(1)
        print(f"✅ Auto-detected: Instance {instance_num}")
    else:
        instance_num = args.instance

    resume = not args.no_resume

    # Run scraper
    run_scraper_for_instance(instance_num, resume)


if __name__ == "__main__":
    main()
