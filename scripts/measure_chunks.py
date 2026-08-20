#!/usr/bin/env python3
"""
Measure real chunks/doc distribution from scraped documents.

This determines if we have 3, 4, 5 or 6 chunks/doc on average,
which changes the indexing timeline by ±26 days.

Usage:
    python scripts/measure_chunks.py
"""

import sys
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from storage.s3_client import S3Storage
from pipeline.chunker import ProfessionalChunker
from utils.config import Config
import json
import structlog

logger = structlog.get_logger()


def main():
    """Measure chunks per document from S3."""
    config = Config.from_env()

    # Get S3 bucket from environment
    bucket = os.getenv("S3_BUCKET_NAME")
    if not bucket:
        print("[ERROR] S3_BUCKET_NAME environment variable not set!")
        sys.exit(1)

    # Initialize S3 client
    s3 = S3Storage(
        bucket_name=bucket,
        region=config.aws.region,
        aws_access_key_id=config.aws.access_key_id,
        aws_secret_access_key=config.aws.secret_access_key,
    )

    chunker = ProfessionalChunker()

    print("\n" + "="*60)
    print("MEASURING CHUNKS PER DOCUMENT")
    print("="*60)

    # S3 configuration
    prefix = "normativabcn/originals/"

    print(f"\nReading from: s3://{bucket}/{prefix}")

    # List all documents
    print("Listing documents in S3...")
    objects = s3.list_objects(prefix)

    if len(objects) == 0:
        print("[ERROR] No documents found in S3!")
        sys.exit(1)

    # Limit to 1000 docs for sampling
    sample_size = min(1000, len(objects))
    objects = objects[:sample_size]
    total_docs = len(objects)

    print(f"Found {len(objects):,} documents in S3")
    print(f"Sampling first {total_docs:,} documents for measurement")
    print("\nProcessing documents...\n")

    chunks_per_doc = []
    errors = 0

    # Process each document
    for i, obj_key in enumerate(objects, 1):
        if i % 100 == 0:
            print(f"  Progress: {i:,}/{total_docs:,} docs ({i*100//total_docs}%)")

        try:
            # Read doc from S3
            doc_json = s3.get_object(obj_key)
            doc = json.loads(doc_json)

            # Chunk the full_content
            chunks = chunker.chunk(
                text=doc["full_content"],
                metadata={"norm_id": doc.get("norm_id", "unknown")}
            )

            chunks_per_doc.append(len(chunks))

        except Exception as e:
            errors += 1
            if errors <= 5:  # Show first 5 errors
                logger.warning("chunk_error", key=obj_key, error=str(e))

    if not chunks_per_doc:
        print("[ERROR] No documents could be processed!")
        sys.exit(1)

    # Calculate statistics
    chunks_per_doc.sort()
    n = len(chunks_per_doc)

    mean = sum(chunks_per_doc) / n
    median = chunks_per_doc[n // 2]
    p90 = chunks_per_doc[int(n * 0.9)]
    p95 = chunks_per_doc[int(n * 0.95)]
    min_chunks = min(chunks_per_doc)
    max_chunks = max(chunks_per_doc)

    # Distribution by bucket
    buckets = {}
    for chunks in chunks_per_doc:
        bucket = min(chunks, 10)  # Cap at 10+ for display
        buckets[bucket] = buckets.get(bucket, 0) + 1

    # Results
    print("\n" + "="*60)
    print("RESULTS")
    print("="*60)
    print(f"\nTotal documents processed: {n:,}")
    if errors:
        print(f"Errors: {errors}")

    print(f"\nChunks per document:")
    print(f"  Mean:   {mean:.2f}")
    print(f"  Median: {median}")
    print(f"  P90:    {p90}")
    print(f"  P95:    {p95}")
    print(f"  Min:    {min_chunks}")
    print(f"  Max:    {max_chunks}")

    # Distribution
    print(f"\nDistribution:")
    for i in sorted(buckets.keys()):
        count = buckets[i]
        pct = count * 100 / n
        bar = "#" * int(pct / 2)
        label = f"{i}+" if i == 10 else str(i)
        print(f"  {label:>3} chunks: {count:>5} docs ({pct:>5.1f}%) {bar}")

    # Impact on timeline
    print(f"\n" + "="*60)
    print("IMPACT ON TIMELINE")
    print("="*60)

    scenarios = [
        (3, 39, "Optimista"),
        (4, 52, "Base (estimado)"),
        (5, 65, "Realista"),
        (6, 78, "Pesimista"),
    ]

    print(f"\nIndexing scenarios (411K docs @ 16.2M tokens/day):")
    for chunks, days, label in scenarios:
        marker = " <-- YOUR RESULT" if abs(mean - chunks) < 0.5 else ""
        print(f"  {chunks} chunks/doc -> {days:>2} days indexing ({label}){marker}")

    # Calculate exact days based on mean
    tokens_per_day = 16_200_000
    tokens_per_chunk = 512
    total_docs = 411_000

    total_tokens = total_docs * mean * tokens_per_chunk
    days_indexing = int(total_tokens / tokens_per_day)

    print(f"\n[RESULT] YOUR TIMELINE:")
    print(f"  Mean chunks/doc: {mean:.2f}")
    print(f"  Estimated indexing: {days_indexing} days")
    print(f"  Total tokens: {total_tokens:,.0f}")

    print("\n" + "="*60)
    print(f"\n[ACTION] Update ESTIMACION_4_DIAS.md with: {mean:.1f} chunks/doc = {days_indexing} days")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
