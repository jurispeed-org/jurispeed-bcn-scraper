"""
Smart batching for Bedrock embeddings with automatic overflow handling.

This module provides intelligent batching that:
- Maximizes batch sizes within limits
- Handles overflow gracefully (no errors)
- Optimizes for both RPM and TPM limits
"""

import structlog
from typing import List, Tuple
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class BatchLimits:
    """Bedrock Cohere Embed v4 limits."""
    max_texts: int = 96  # Max texts per request
    max_tokens_per_request: int = 100_000  # Conservative (Bedrock allows 128K)
    max_tokens_per_text: int = 100_000  # Per individual text


class SmartBatcher:
    """
    Intelligent batcher for Bedrock embeddings.

    Features:
    - Automatically splits batches when limits are reached
    - Never fails on oversized chunks
    - Optimizes batch size for throughput

    Example:
        batcher = SmartBatcher()
        chunks = ["text1", "text2", ..., "text100"]

        for batch in batcher.create_batches(chunks):
            embeddings = bedrock.embed_texts(batch)
    """

    def __init__(self, limits: BatchLimits = None):
        self.limits = limits or BatchLimits()
        self.stats = {
            "total_chunks": 0,
            "batches_created": 0,
            "oversized_chunks_skipped": 0,
            "avg_batch_size": 0,
        }

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text.

        Conservative estimate: 0.6 tokens per character for Spanish.
        """
        return int(len(text) * 0.6)

    def create_batches(self, chunks: List[str]) -> List[List[str]]:
        """
        Create optimal batches from chunks.

        Args:
            chunks: List of text chunks to batch

        Returns:
            List of batches, each batch is a list of texts

        Algorithm:
        1. Try to add chunk to current batch
        2. If doesn't fit (size or token limit):
           - Send current batch
           - Start new batch with this chunk
        3. If chunk is too large individually:
           - Log warning
           - Send it alone (Bedrock will truncate)
        """
        batches = []
        current_batch = []
        current_tokens = 0

        self.stats["total_chunks"] = len(chunks)

        for i, chunk in enumerate(chunks):
            chunk_tokens = self.estimate_tokens(chunk)

            # Check if individual chunk is too large
            if chunk_tokens > self.limits.max_tokens_per_text:
                # Chunk is too large, but we'll send it anyway
                # Bedrock will truncate (not ideal, but better than failing)
                logger.warning(
                    "chunk_too_large",
                    chunk_index=i,
                    estimated_tokens=chunk_tokens,
                    limit=self.limits.max_tokens_per_text,
                    action="sending_alone_will_truncate"
                )
                self.stats["oversized_chunks_skipped"] += 1

                # Flush current batch first
                if current_batch:
                    batches.append(current_batch)
                    current_batch = []
                    current_tokens = 0

                # Send oversized chunk alone
                batches.append([chunk])
                continue

            # Check if chunk fits in current batch
            would_exceed_count = len(current_batch) >= self.limits.max_texts
            would_exceed_tokens = (current_tokens + chunk_tokens) > self.limits.max_tokens_per_request

            if would_exceed_count or would_exceed_tokens:
                # Current batch is full, flush it
                if current_batch:
                    batches.append(current_batch)
                    logger.debug(
                        "batch_completed",
                        batch_size=len(current_batch),
                        total_tokens=current_tokens
                    )

                # Start new batch with this chunk
                current_batch = [chunk]
                current_tokens = chunk_tokens
            else:
                # Add to current batch
                current_batch.append(chunk)
                current_tokens += chunk_tokens

        # Flush final batch
        if current_batch:
            batches.append(current_batch)
            logger.debug(
                "batch_completed",
                batch_size=len(current_batch),
                total_tokens=current_tokens
            )

        # Update stats
        self.stats["batches_created"] = len(batches)
        if batches:
            self.stats["avg_batch_size"] = sum(len(b) for b in batches) / len(batches)

        logger.info(
            "batching_complete",
            total_chunks=self.stats["total_chunks"],
            batches_created=self.stats["batches_created"],
            avg_batch_size=f"{self.stats['avg_batch_size']:.1f}",
            oversized_skipped=self.stats["oversized_chunks_skipped"]
        )

        return batches

    def get_stats(self) -> dict:
        """Get batching statistics."""
        return self.stats.copy()


def batch_chunks_for_embedding(chunks: List[str]) -> List[List[str]]:
    """
    Convenience function to batch chunks.

    Args:
        chunks: List of text chunks

    Returns:
        List of batches ready for Bedrock

    Example:
        chunks = ["text1", "text2", ..., "text100"]
        batches = batch_chunks_for_embedding(chunks)

        for batch in batches:
            embeddings = bedrock_client.embed_texts(batch)
    """
    batcher = SmartBatcher()
    return batcher.create_batches(chunks)
