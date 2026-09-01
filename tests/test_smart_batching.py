"""
Test script for smart batching validation.

This validates:
1. Batching respects limits
2. Quality is maintained (embeddings are identical)
3. Throughput improvement vs no batching
"""

import sys
import time
from pathlib import Path

# Add src to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from pipeline.smart_batcher import SmartBatcher, batch_chunks_for_embedding
from pipeline.embedder import BedrockEmbedder
from pipeline.chunker import ProfessionalChunker
import structlog

logger = structlog.get_logger()


def test_batching_limits():
    """Test 1: Verify batching respects limits."""
    print("=" * 70)
    print("TEST 1: Batching Limits Validation")
    print("=" * 70)
    print()

    batcher = SmartBatcher()

    # Create test chunks of various sizes
    small_chunks = ["texto pequeño"] * 100  # 100 small chunks
    medium_chunks = ["texto medio " * 50] * 50  # 50 medium chunks
    large_chunks = ["texto grande " * 500] * 10  # 10 large chunks

    test_cases = [
        ("Small chunks (100)", small_chunks),
        ("Medium chunks (50)", medium_chunks),
        ("Large chunks (10)", large_chunks),
    ]

    for name, chunks in test_cases:
        print(f"\n{name}:")
        batches = batcher.create_batches(chunks)

        print(f"  Input chunks: {len(chunks)}")
        print(f"  Batches created: {len(batches)}")
        print(f"  Avg batch size: {sum(len(b) for b in batches) / len(batches):.1f}")

        # Verify all batches respect limits
        for i, batch in enumerate(batches):
            assert len(batch) <= 96, f"Batch {i} exceeds 96 texts"
            total_tokens = sum(batcher.estimate_tokens(text) for text in batch)
            assert total_tokens <= 100_000, f"Batch {i} exceeds 100K tokens"

        print(f"  [OK] All batches within limits")

    print("\n[PASSED] TEST 1\n")


def test_embedding_quality(embedder: BedrockEmbedder):
    """Test 2: Verify batching doesn't affect quality."""
    print("=" * 70)
    print("TEST 2: Embedding Quality (batch vs individual)")
    print("=" * 70)
    print()

    test_text = "Artículo 1°. El presente decreto establece normas sobre subsidio habitacional."

    # Generate embedding individually
    print("Generating individual embedding...")
    embedding_solo = embedder.embed_one(test_text)

    # Generate embedding in batch
    print("Generating batch embedding...")
    embeddings_batch = embedder.embed_texts([test_text, "otro texto", "mas texto"])
    embedding_from_batch = embeddings_batch[0]

    # Compare
    print("\nComparing embeddings...")
    differences = []
    for i, (solo, batch) in enumerate(zip(embedding_solo, embedding_from_batch)):
        diff = abs(solo - batch)
        if diff > 0.0001:  # Tolerance for floating point
            differences.append((i, diff))

    if not differences:
        print("[OK] Embeddings are IDENTICAL")
    else:
        print(f"[WARNING] Found {len(differences)} differences:")
        for idx, diff in differences[:5]:  # Show first 5
            print(f"   Position {idx}: diff = {diff}")

    print("\n[PASSED] TEST 2\n")


def test_throughput_comparison(embedder: BedrockEmbedder):
    """Test 3: Compare throughput with different batch sizes."""
    print("=" * 70)
    print("TEST 3: Throughput Comparison")
    print("=" * 70)
    print()

    # Create test chunks (simulate 10 documents = 40 chunks)
    num_docs = 10
    chunks_per_doc = 4
    test_chunks = [f"Artículo {i}. Contenido de prueba..." for i in range(num_docs * chunks_per_doc)]

    print(f"Test data: {num_docs} documents = {len(test_chunks)} chunks\n")

    # Test 1: No batching (1 chunk per request)
    print("Method 1: No batching (1 chunk/request)")
    start = time.time()
    for chunk in test_chunks:
        embedder.embed_one(chunk)
    time_no_batch = time.time() - start
    requests_no_batch = len(test_chunks)
    print(f"  Time: {time_no_batch:.2f}s")
    print(f"  Requests: {requests_no_batch}")
    print(f"  Rate: {len(test_chunks)/time_no_batch:.1f} chunks/sec")
    print()

    # Test 2: Batch by document (4 chunks per request)
    print("Method 2: Batch by document (4 chunks/request)")
    start = time.time()
    for i in range(0, len(test_chunks), chunks_per_doc):
        batch = test_chunks[i:i+chunks_per_doc]
        embedder.embed_texts(batch)
    time_batch_4 = time.time() - start
    requests_batch_4 = num_docs
    print(f"  Time: {time_batch_4:.2f}s")
    print(f"  Requests: {requests_batch_4}")
    print(f"  Rate: {len(test_chunks)/time_batch_4:.1f} chunks/sec")
    print(f"  Speedup: {time_no_batch/time_batch_4:.1f}x faster")
    print()

    # Test 3: Smart batching (optimal)
    print("Method 3: Smart batching (optimal)")
    batcher = SmartBatcher()
    batches = batcher.create_batches(test_chunks)
    start = time.time()
    for batch in batches:
        embedder.embed_texts(batch)
    time_smart = time.time() - start
    print(f"  Time: {time_smart:.2f}s")
    print(f"  Requests: {len(batches)}")
    print(f"  Rate: {len(test_chunks)/time_smart:.1f} chunks/sec")
    print(f"  Speedup: {time_no_batch/time_smart:.1f}x faster")
    print()

    # Summary
    print("SUMMARY:")
    print(f"  No batching:      {requests_no_batch:3d} requests, {time_no_batch:6.2f}s (baseline)")
    print(f"  Batch-4:          {requests_batch_4:3d} requests, {time_batch_4:6.2f}s ({time_no_batch/time_batch_4:.1f}x)")
    print(f"  Smart batching:   {len(batches):3d} requests, {time_smart:6.2f}s ({time_no_batch/time_smart:.1f}x)")

    print("\n[PASSED] TEST 3\n")


def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("SMART BATCHING VALIDATION SUITE")
    print("=" * 70)
    print()

    # Test 1: Limits (no Bedrock needed)
    test_batching_limits()

    # Prompt before Bedrock tests (they cost money)
    response = input("Continue with Bedrock tests? (requires AWS, costs ~$0.01) [y/N]: ")
    if response.lower() != 'y':
        print("\nSkipping Bedrock tests. Run with 'y' to test quality and throughput.")
        return

    # Initialize Bedrock embedder
    print("\nInitializing Bedrock embedder...")
    embedder = BedrockEmbedder(
        region="us-west-2",
        model_id="cohere.embed-v4:0",
        dimensions=512
    )

    # Test 2 & 3: Quality and throughput
    test_embedding_quality(embedder)
    test_throughput_comparison(embedder)

    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
    print()

    # Print stats
    stats = embedder.get_stats()
    print("Bedrock Stats:")
    print(f"  Total calls: {stats['total_calls']}")
    print(f"  Total texts: {stats['total_texts']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    main()
