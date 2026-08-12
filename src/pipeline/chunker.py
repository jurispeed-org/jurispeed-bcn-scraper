"""
Professional text chunking for RAG.

Follows best practices:
- Semantic chunking (by meaning, not arbitrary bytes)
- Token-based limits (not byte-based)
- Overlap for context preservation
- Hierarchical separators
- Metadata enrichment
"""

import re
import structlog
from typing import List, Dict, Optional
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class Chunk:
    """A text chunk with metadata."""

    text: str
    chunk_index: int
    chunk_total: int
    token_count: int
    char_start: int
    char_end: int
    metadata: Dict


class ProfessionalChunker:
    """
    Professional text chunker following RAG best practices.

    Features:
    - Semantic boundaries (paragraphs, sentences)
    - Token-aware chunking (not bytes)
    - Overlap for context preservation
    - Hierarchical separators
    - Article detection for legal texts
    """

    def __init__(
        self,
        target_chunk_size: int = 512,  # tokens (optimal for Cohere v4)
        max_chunk_size: int = 1000,  # tokens (hard limit)
        overlap_tokens: int = 50,  # overlap for context
        min_chunk_size: int = 100,  # avoid tiny chunks
    ):
        self.target_chunk_size = target_chunk_size
        self.max_chunk_size = max_chunk_size
        self.overlap_tokens = overlap_tokens
        self.min_chunk_size = min_chunk_size

        # Hierarchical separators (legal text specific)
        self.separators = [
            r"\n\n(?=Artículo\s+\d+)",  # Article boundaries (highest priority)
            r"\n\n(?=ARTÍCULO\s+\d+)",  # Uppercase variant
            r"\n\n(?=Art\.\s+\d+)",  # Abbreviated
            r"\n\n",  # Double newline (paragraphs)
            r"\n",  # Single newline
            r"\.\s+",  # Sentence boundaries
            r";\s+",  # Semicolons
            r",\s+",  # Commas (last resort)
        ]

        logger.info(
            "chunker_initialized",
            target_size=target_chunk_size,
            max_size=max_chunk_size,
            overlap=overlap_tokens,
        )

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count (rough approximation).

        For Spanish legal text: ~0.6 tokens per character
        This is conservative (Cohere v4 is usually more efficient)
        """
        return int(len(text) * 0.6)

    def chunk(self, text: str, metadata: Optional[Dict] = None) -> List[Chunk]:
        """
        Chunk text using semantic boundaries.

        Args:
            text: Full text to chunk
            metadata: Optional metadata to attach to all chunks

        Returns:
            List of Chunk objects
        """
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")

        # Normalize whitespace
        text = self._normalize_text(text)

        # Detect if legal text with articles
        has_articles = self._detect_articles(text)

        if has_articles:
            logger.info("chunking_legal_text_with_articles")
            chunks = self._chunk_by_articles(text)
        else:
            logger.info("chunking_generic_text")
            chunks = self._chunk_by_semantic_boundaries(text)

        # Add overlap for context preservation
        chunks = self._add_overlap(chunks, text)

        # Convert to Chunk objects with metadata
        chunk_objects = []
        for i, chunk_text in enumerate(chunks):
            chunk_obj = Chunk(
                text=chunk_text,
                chunk_index=i,
                chunk_total=len(chunks),
                token_count=self.estimate_tokens(chunk_text),
                char_start=text.find(chunk_text),
                char_end=text.find(chunk_text) + len(chunk_text),
                metadata=metadata or {},
            )
            chunk_objects.append(chunk_obj)

        logger.info(
            "chunking_complete",
            total_chunks=len(chunk_objects),
            avg_tokens=sum(c.token_count for c in chunk_objects) / len(chunk_objects),
            has_articles=has_articles,
        )

        return chunk_objects

    def _normalize_text(self, text: str) -> str:
        """Normalize text (whitespace, unicode, etc.)."""
        # Normalize unicode
        text = text.strip()
        # Remove excessive whitespace
        text = re.sub(r" +", " ", text)
        # Normalize newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _detect_articles(self, text: str) -> bool:
        """Detect if text contains legal articles."""
        # Check for multiple article markers
        patterns = [
            r"Artículo\s+\d+",
            r"ARTÍCULO\s+\d+",
            r"Art\.\s+\d+",
            r"Artículo\s+[IVXLCDM]+",  # Roman numerals
        ]

        matches = 0
        for pattern in patterns:
            matches += len(re.findall(pattern, text))

        # If more than 3 articles, treat as legal text
        return matches >= 3

    def _chunk_by_articles(self, text: str) -> List[str]:
        """
        Chunk by article boundaries (legal text).

        Strategy:
        1. Split by article markers
        2. If article > max_size, split further by paragraphs
        3. If paragraph > max_size, split by sentences
        """
        chunks = []

        # Split by article boundaries
        article_pattern = r"(?=(?:Artículo|ARTÍCULO|Art\.)\s+\d+)"
        articles = re.split(article_pattern, text)

        for article in articles:
            article = article.strip()
            if not article:
                continue

            tokens = self.estimate_tokens(article)

            if tokens <= self.target_chunk_size:
                # Article fits in one chunk
                chunks.append(article)

            elif tokens <= self.max_chunk_size:
                # Article is larger than target but smaller than max
                # Keep it as one chunk (preserve article integrity)
                chunks.append(article)
                logger.debug("large_article_kept_intact", tokens=tokens)

            else:
                # Article too large, split by paragraphs
                logger.debug("splitting_large_article", tokens=tokens)
                sub_chunks = self._split_by_separator(article, r"\n\n")
                chunks.extend(sub_chunks)

        return chunks

    def _chunk_by_semantic_boundaries(self, text: str) -> List[str]:
        """
        Chunk by semantic boundaries (generic text).

        Uses hierarchical separators.
        """
        chunks = []
        current_chunk = ""
        current_tokens = 0

        # Split by highest priority separator (paragraphs)
        paragraphs = re.split(r"\n\n", text)

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            para_tokens = self.estimate_tokens(para)

            # If paragraph alone exceeds max, split it further
            if para_tokens > self.max_chunk_size:
                # Flush current chunk if not empty
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    current_tokens = 0

                # Split large paragraph by sentences
                sub_chunks = self._split_by_separator(para, r"\.\s+")
                chunks.extend(sub_chunks)
                continue

            # Try to add paragraph to current chunk
            if current_tokens + para_tokens <= self.target_chunk_size:
                current_chunk += "\n\n" + para if current_chunk else para
                current_tokens += para_tokens

            else:
                # Current chunk is full, start new chunk
                if current_chunk:
                    chunks.append(current_chunk.strip())

                current_chunk = para
                current_tokens = para_tokens

        # Add remaining chunk
        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    def _split_by_separator(self, text: str, separator: str) -> List[str]:
        """Split text by separator, respecting size limits."""
        chunks = []
        current_chunk = ""
        current_tokens = 0

        parts = re.split(separator, text)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            part_tokens = self.estimate_tokens(part)

            if current_tokens + part_tokens <= self.max_chunk_size:
                current_chunk += " " + part if current_chunk else part
                current_tokens += part_tokens
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = part
                current_tokens = part_tokens

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    def _add_overlap(self, chunks: List[str], original_text: str) -> List[str]:
        """
        Add overlap between chunks for context preservation.

        Takes last N tokens from previous chunk and prepends to next chunk.
        """
        if len(chunks) <= 1 or self.overlap_tokens == 0:
            return chunks

        overlapped = []

        for i, chunk in enumerate(chunks):
            if i == 0:
                # First chunk, no overlap
                overlapped.append(chunk)
                continue

            # Get overlap from previous chunk
            prev_chunk = chunks[i - 1]
            overlap_text = self._get_last_n_tokens(prev_chunk, self.overlap_tokens)

            # Prepend overlap to current chunk
            overlapped_chunk = f"{overlap_text} [...] {chunk}"
            overlapped.append(overlapped_chunk)

        return overlapped

    def _get_last_n_tokens(self, text: str, n_tokens: int) -> str:
        """
        Get last N tokens from text (approximate).

        For Spanish: ~1.6 chars per token
        """
        n_chars = int(n_tokens * 1.6)
        if len(text) <= n_chars:
            return text

        # Find sentence boundary near the cutoff
        cutoff = text[-n_chars:]
        sentence_start = cutoff.find(". ")

        if sentence_start != -1:
            return cutoff[sentence_start + 2 :]

        return cutoff


# Convenience function
def chunk_text(
    text: str,
    target_size: int = 512,
    overlap: int = 50,
    metadata: Optional[Dict] = None,
) -> List[Chunk]:
    """
    Convenience function for chunking text.

    Args:
        text: Text to chunk
        target_size: Target chunk size in tokens
        overlap: Overlap size in tokens
        metadata: Optional metadata

    Returns:
        List of Chunk objects
    """
    chunker = ProfessionalChunker(
        target_chunk_size=target_size,
        overlap_tokens=overlap,
    )
    return chunker.chunk(text, metadata)
