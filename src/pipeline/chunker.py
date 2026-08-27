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
from core.xml_parser import BCNXMLParser

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
        max_chunk_size: int = 2048,  # tokens (hard limit for non-article chunks)
        overlap_tokens: int = 200,  # overlap for context (increased for legal text)
        min_chunk_size: int = 100,  # avoid tiny chunks
        article_max_size: int = 8192,  # max size for a single article before splitting (4× increased)
    ):
        self.target_chunk_size = target_chunk_size
        self.max_chunk_size = max_chunk_size
        self.overlap_tokens = overlap_tokens
        self.min_chunk_size = min_chunk_size
        self.article_max_size = article_max_size

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

    def chunk(self, text: str, metadata: Optional[Dict] = None, html: Optional[str] = None, norm_id: Optional[int] = None, xml_content: Optional[str] = None) -> List[Chunk]:
        """
        Chunk text using semantic boundaries.

        Args:
            text: Full text to chunk
            metadata: Optional metadata to attach to all chunks
            html: Optional raw HTML for hierarchy extraction (legacy)
            norm_id: Optional norm ID for hierarchy extraction
            xml_content: Optional XML content (preferred over HTML)

        Returns:
            List of Chunk objects
        """
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")

        # Normalize whitespace
        text = self._normalize_text(text)

        # Extract hierarchy from XML (preferred) or HTML
        hierarchy = {}
        article_texts = {}

        if xml_content and norm_id:
            # Use XML parser (preferred)
            try:
                from core.xml_parser import BCNXMLParser
                parser = BCNXMLParser()
                hierarchy = parser.extract_article_hierarchy(xml_content, norm_id)
                article_texts = parser.extract_article_texts(xml_content)
                logger.debug("xml_hierarchy_loaded", total_parts=len(hierarchy))
            except Exception as e:
                logger.warning("xml_hierarchy_extraction_failed", error=str(e))
        elif html and norm_id:
            # Fallback to HTML parser (legacy)
            try:
                from core.parser import BCNHtmlParser
                parser = BCNHtmlParser()
                hierarchy = parser.extract_article_hierarchy(html, norm_id)
                part_id_map = self._build_part_id_map(html, hierarchy)
                logger.debug("html_hierarchy_loaded", total_parts=len(hierarchy))
            except Exception as e:
                logger.warning("html_hierarchy_extraction_failed", error=str(e))

        # Detect if legal text with articles
        has_articles = self._detect_articles(text)

        # Use idParte-based chunking if hierarchy available
        if has_articles and hierarchy:
            if article_texts:
                # XML path with structural context
                logger.info("chunking_by_xml_idparte", total_parts=len(hierarchy))

                # Extract structural context (book/title/section) for each article
                structural_context = {}
                if xml_content and norm_id:
                    try:
                        from core.xml_parser import BCNXMLParser
                        parser = BCNXMLParser()
                        structural_context = parser.extract_structural_context(xml_content, norm_id)
                        logger.info("structural_context_loaded", articles_with_context=len(structural_context))
                    except Exception as e:
                        logger.warning("structural_context_extraction_failed", error=str(e))

                # Chunk articles with structural context
                article_chunks = self._chunk_by_xml(article_texts, hierarchy, metadata, structural_context)

                # NO special chunks - ruta viaja CON el articulo
                chunks = article_chunks

                logger.info(
                    "chunking_complete",
                    total_chunks=len(chunks)
                )
            elif html:
                # HTML path (legacy)
                logger.info("chunking_by_html_idparte", total_parts=len(hierarchy))
                chunks = self._chunk_by_idparte(html, hierarchy, metadata)
            else:
                logger.info("chunking_legal_text_with_articles")
                chunks = self._chunk_by_articles(text)
        elif has_articles:
            logger.info("chunking_legal_text_with_articles")
            chunks = self._chunk_by_articles(text)
        else:
            logger.info("chunking_generic_text")
            chunks = self._chunk_by_semantic_boundaries(text)

        # Add overlap for context preservation (skip for article-based chunks)
        if not has_articles:
            chunks = self._add_overlap(chunks, text)

        # Convert to Chunk objects with metadata
        chunk_objects = []
        for i, chunk_data in enumerate(chunks):
            # Handle both dict (from idParte) and string (from regex) chunks
            if isinstance(chunk_data, dict):
                # idParte-based chunk (already has metadata)
                chunk_text = chunk_data["text"]
                chunk_metadata = chunk_data["metadata"]
            else:
                # Regex-based chunk (need to extract metadata)
                chunk_text = chunk_data
                chunk_metadata = metadata.copy() if metadata else {}

                # If has articles, extract article number and add to metadata
                if has_articles:
                    article_num = self._extract_article_number(chunk_text)
                    if article_num is not None:
                        chunk_metadata["article_number"] = article_num

                        # Add hierarchy metadata if available
                        if hierarchy and part_id_map:
                            # Find part_id for this chunk based on article number and text
                            part_info = self._find_chunk_hierarchy(chunk_text, article_num, hierarchy, part_id_map)
                            if part_info:
                                chunk_metadata.update(part_info)

            chunk_obj = Chunk(
                text=chunk_text,
                chunk_index=i,
                chunk_total=len(chunks),
                token_count=self.estimate_tokens(chunk_text),
                char_start=text.find(chunk_text) if isinstance(chunk_data, str) else 0,
                char_end=(text.find(chunk_text) + len(chunk_text)) if isinstance(chunk_data, str) else len(chunk_text),
                metadata=chunk_metadata,
            )
            chunk_objects.append(chunk_obj)

        logger.info(
            "chunking_complete",
            total_chunks=len(chunk_objects),
            avg_tokens=sum(c.token_count for c in chunk_objects) / len(chunk_objects),
            has_articles=has_articles,
        )

        return chunk_objects

    def _build_part_id_map(self, html: str, hierarchy: dict) -> dict:
        """
        Build map of part_id → text snippet for matching chunks.

        Returns dict: {part_id: text_preview}
        """
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            part_map = {}

            for part_id in hierarchy.keys():
                # Find the div with this ID
                part_div = soup.find(id=part_id)
                if part_div:
                    # Get first 200 chars of text for matching
                    text = part_div.get_text(strip=True)[:200]
                    part_map[part_id] = text

            return part_map

        except Exception as e:
            logger.warning("part_id_map_build_failed", error=str(e))
            return {}

    def _find_chunk_hierarchy(self, chunk_text: str, article_num: int, hierarchy: dict, part_id_map: dict) -> Optional[dict]:
        """
        Find hierarchy metadata for a chunk.

        Matches chunk to part_id by:
        1. Article number match
        2. Text similarity

        Returns hierarchy metadata or None
        """
        try:
            # Get first 200 chars of chunk for matching
            chunk_preview = chunk_text.strip()[:200]

            # Find matching part_id
            best_match = None
            best_score = 0

            for part_id, part_preview in part_id_map.items():
                # Get hierarchy info for this part
                part_info = hierarchy.get(part_id)
                if not part_info:
                    continue

                # Check if article number matches
                if part_info.get('article_number') != article_num:
                    continue

                # Calculate text similarity (simple overlap)
                # Remove whitespace for comparison
                chunk_clean = ''.join(chunk_preview.split())
                part_clean = ''.join(part_preview.split())

                if len(chunk_clean) < 50 or len(part_clean) < 50:
                    continue

                # Check if chunk starts with part text (or vice versa)
                if chunk_clean.startswith(part_clean[:50]) or part_clean.startswith(chunk_clean[:50]):
                    score = 100
                else:
                    # Calculate character overlap
                    common = sum(1 for c in chunk_clean[:100] if c in part_clean[:100])
                    score = common

                if score > best_score:
                    best_score = score
                    best_match = part_info

            if best_match and best_score > 30:  # Threshold
                return {
                    "parent_article": best_match.get("parent_article"),
                    "is_nested": best_match.get("is_nested", False),
                    "hierarchy_level": best_match.get("hierarchy_level", 1),
                    "article_label": best_match.get("article_label")
                }

            return None

        except Exception as e:
            logger.warning("chunk_hierarchy_match_failed", error=str(e))
            return None

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
        # Check for multiple article markers (case-insensitive)
        patterns = [
            r"art[ií]culo\s+\d+",  # Artículo/ARTÍCULO/articulo with number
            r"art\.\s+\d+",         # Art. with number (marginal notes)
            r"art[ií]culo\s+[IVXLCDM]+",  # Roman numerals
        ]

        matches = 0
        for pattern in patterns:
            matches += len(re.findall(pattern, text, re.IGNORECASE))

        # If more than 3 articles, treat as legal text
        return matches >= 3

    def _extract_article_number(self, text: str) -> Optional[int]:
        """
        Extract article number from text (case-insensitive).

        Supports formats:
        - "Artículo 1°" or "ARTÍCULO 1°" (with accent)
        - "ARTICULO 2" (without accent, BCN format)
        - "Art. 3"
        - "artículo 123 bis" (lowercase)

        Returns:
            Article number as integer, or None if not found
        """
        # Case-insensitive pattern matching all variants
        pattern = r"(?:art[ií]culo|art\.)\s+(\d+)"

        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                pass

        return None

    def _chunk_by_articles(self, text: str) -> List[str]:
        """
        Chunk by article boundaries (legal text).

        IMPROVED STRATEGY for BCN normativa:
        - Divides ONLY on real article starts: "ARTICULO N°.-" (with dash)
        - Does NOT divide on marginal notes: "[...Art. N°...]"
        - Each ARTICLE = 1 complete chunk (unless > article_max_size)
        - This ensures granular retrieval (article-level, not law-level)

        Example:
        - Ley 824: 128 articles → 128 chunks (one per article)
        - Decreto simple: No articles → 1 chunk (semantic boundaries)
        - Query: "renta devengada" → Returns specific article, not whole law
        """
        chunks = []

        # Strategy: Split only on article markers that appear at line start or after punctuation
        # This avoids splitting on "según el artículo 41, la norma..." (inline reference)
        # But catches "ARTICULO 1°.-" or "\n\nArticulo 2°.-" (real article)
        #
        # Pattern explanation:
        # (?:^|\n)           - Start of string or newline
        # (?=                - Lookahead (don't consume)
        #   (?:ARTICULO|ARTÍCULO|Artículo)  - Article marker (uppercase/title case)
        #   \s+\d+[°º]       - Space + number + degree symbol
        # )
        article_pattern = r"(?:^|\n)(?=(?:ARTICULO|ARTÍCULO|Artículo)\s+\d+[°º])"
        articles = re.split(article_pattern, text, flags=re.MULTILINE)

        for article in articles:
            article = article.strip()
            if not article:
                continue

            tokens = self.estimate_tokens(article)

            # Extract article number for metadata
            article_num = self._extract_article_number(article)

            # Keep article COMPLETE (no 512 token limit)
            if tokens <= self.article_max_size:
                # Article complete = 1 chunk
                chunks.append(article)
                logger.debug(
                    "article_chunk_created",
                    article_number=article_num,
                    tokens=tokens
                )

            else:
                # Article too large (rare), split by paragraphs WITH OVERLAP
                logger.warning(
                    "splitting_very_large_article",
                    article_number=article_num,
                    tokens=tokens,
                    threshold=self.article_max_size
                )
                sub_chunks = self._split_by_separator(article, r"\n\n")

                # Add overlap between sub-chunks to preserve legal context
                if len(sub_chunks) > 1:
                    sub_chunks = self._add_overlap(sub_chunks, article)

                chunks.extend(sub_chunks)

        return chunks

    def _chunk_by_idparte(self, html: str, hierarchy: dict, base_metadata: Optional[Dict] = None) -> List[Dict]:
        """
        DEPRECATED: Legacy HTML chunking (XML path preferred).

        Chunk by idParte from HTML with substructure parsing.

        Uses the part_id from the TOC to extract exact article boundaries
        from the HTML. Then parses each article into numerales, letras (NO incisos).

        Args:
            html: Raw HTML
            hierarchy: Dict mapping part_id -> article metadata
            base_metadata: Base metadata (norm info) to include in all chunks

        Returns:
            List of dicts with 'text' and 'metadata' keys
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("beautifulsoup4_not_installed")
            raise ImportError("beautifulsoup4 required for idParte chunking")

        try:
            from pipeline.article_parser import parse_article_substructure
        except ImportError:
            logger.error("article_parser_not_found")
            raise ImportError("article_parser required for substructure")

        soup = BeautifulSoup(html, "lxml")
        chunks = []

        # Get norm metadata for contextual headers
        norm_citation = base_metadata.get("norm_type", "") + " N° " + str(base_metadata.get("norm_number", ""))
        norm_title = base_metadata.get("norm_title", "")

        for part_id, part_info in hierarchy.items():
            # Find div with this ID
            part_div = soup.find(id=part_id)
            if not part_div:
                logger.warning("part_div_not_found", part_id=part_id)
                continue

            # Extract text
            article_text = part_div.get_text(separator="\n", strip=True)
            if not article_text:
                logger.warning("empty_article_text", part_id=part_id)
                continue

            # Build base metadata for article
            article_num = part_info.get("article_number")
            article_label = part_info.get("article_label", f"Artículo {article_num}")

            base_chunk_metadata = base_metadata.copy() if base_metadata else {}

            # Build article-specific URL with idParte
            article_url = base_chunk_metadata.get("official_url", "")
            if article_url:
                # Convert Pydantic Url to string if needed
                article_url_str = str(article_url) if not isinstance(article_url, str) else article_url
                if part_id:
                    # Add idParte parameter to URL for direct article linking
                    if "?" in article_url_str:
                        article_url = f"{article_url_str}&idParte={part_id}"
                    else:
                        article_url = f"{article_url_str}?idParte={part_id}"
                else:
                    article_url = article_url_str

            # Extract validity info (if available from hierarchy)
            version_date = part_info.get("version_date")
            in_force = part_info.get("in_force", True)

            # Check for deferred validity (future fechaVersion)
            if BCNXMLParser.is_future_version(fecha_version):
                in_force = False  # Override: not yet in effect
                force_status = "deferred"
                logger.debug(
                    "deferred_validity_detected",
                    part_id=part_id,
                    article_number=article_num,
                    version_date=version_date
                )
            else:
                force_status = "active" if in_force else "repealed"

            base_chunk_metadata.update({
                "part_id": part_id,
                "article_number": article_num,
                "article_label": article_label,
                "parent_article": part_info.get("parent_article"),
                "is_nested": part_info.get("is_nested", False),
                "hierarchy_level": part_info.get("hierarchy_level", 1),
                "in_force": in_force,
                "force_status": force_status,
                "version_date": version_date,
                "official_url": article_url,  # Override with article-specific URL
            })

            # Parse substructure (only numerales and letras, NO incisos)
            subparts = parse_article_substructure(article_text)

            if len(subparts) >= 1:
                # Article has substructure, create one chunk per subpart
                logger.debug(
                    "article_with_substructure",
                    part_id=part_id,
                    article_number=article_num,
                    total_subparts=len(subparts)
                )

                for subpart in subparts:
                    # Build contextual header with FULL structural path
                    # CRITICAL: Include book/title/section names in text for:
                    # 1. BM25 lexical search (e.g., "femicidio" only exists in section_name)
                    # 2. Reranker visibility (reranker reads text, not metadata)
                    # 3. Semantic embeddings (model embeds full context)
                    contextual_header = self._build_full_contextual_header(
                        norm_citation, norm_title, context, article_label, subpart
                    )

                    full_text = contextual_header + subpart.text

                    # Build metadata for this subpart
                    chunk_metadata = base_chunk_metadata.copy()
                    chunk_metadata.update({
                        "substructure_type": subpart.type,
                        "numeral": subpart.number if subpart.type == "numeral" else None,
                        "letra": subpart.letter if subpart.type == "letra" else None,
                    })

                    tokens = self.estimate_tokens(full_text)

                    # Check if subpart is too large
                    if tokens <= self.article_max_size:
                        chunks.append({
                            "text": full_text,
                            "metadata": chunk_metadata
                        })
                        logger.debug(
                            "subpart_chunk_created",
                            part_id=part_id,
                            article_number=article_num,
                            subpart_type=subpart.type,
                            subpart_id=f"{subpart.type}_{subpart.number or subpart.letter}",
                            tokens=tokens
                        )
                    else:
                        # Subpart too large, split by sentences
                        logger.warning(
                            "splitting_large_subpart",
                            part_id=part_id,
                            article_number=article_num,
                            subpart_type=subpart.type,
                            tokens=tokens
                        )
                        sub_texts = self._split_by_separator(subpart.text, r"\.\s+")
                        for sub_text in sub_texts:
                            full_sub_text = contextual_header + sub_text
                            chunks.append({
                                "text": full_sub_text,
                                "metadata": chunk_metadata.copy()
                            })

            else:
                # No substructure or single subpart, treat as whole article
                contextual_header = self._build_full_contextual_header(
                    norm_citation, norm_title, context, article_label, None
                )
                full_text = contextual_header + article_text

                tokens = self.estimate_tokens(full_text)
                if tokens <= self.article_max_size:
                    chunks.append({
                        "text": full_text,
                        "metadata": base_chunk_metadata
                    })
                    logger.debug(
                        "article_chunk_no_substructure",
                        part_id=part_id,
                        article_number=article_num,
                        tokens=tokens
                    )
                else:
                    # Article too large, split by paragraphs
                    logger.warning(
                        "splitting_large_article_no_substructure",
                        part_id=part_id,
                        article_number=article_num,
                        tokens=tokens
                    )
                    sub_chunks_text = self._split_by_separator(article_text, r"\n\n")
                    for sub_text in sub_chunks_text:
                        full_sub_text = contextual_header + sub_text
                        chunks.append({
                            "text": full_sub_text,
                            "metadata": base_chunk_metadata.copy()
                        })

        logger.info("idparte_chunking_complete", total_chunks=len(chunks))
        return chunks

    def _chunk_by_xml(
        self,
        article_texts: Dict[str, str],
        hierarchy: dict,
        base_metadata: Optional[Dict] = None,
        structural_context: Optional[Dict[str, Dict]] = None
    ) -> List[Dict]:
        """
        Chunk by XML article texts with full structural context.

        Args:
            article_texts: Dict mapping idParte -> article text
            hierarchy: Dict mapping idParte -> article metadata
            base_metadata: Base metadata (norm info) to include in all chunks
            structural_context: Dict mapping idParte -> structural context (book/title/section)

        Returns:
            List of dicts with 'text' and 'metadata' keys

        CRITICAL: Structural context must be in BOTH text and metadata:
        - Text: for BM25 lexical search and reranker visibility
        - Metadata: for filters and citation construction
        """
        try:
            from pipeline.article_parser import parse_article_substructure
        except ImportError:
            logger.error("article_parser_not_found")
            raise ImportError("article_parser required for substructure")

        chunks = []
        structural_context = structural_context or {}

        # Get norm metadata for contextual headers
        norm_citation = base_metadata.get("norm_type", "") + " N° " + str(base_metadata.get("norm_number", ""))
        norm_title = base_metadata.get("norm_title", "")

        for part_id, article_text in article_texts.items():
            # Get hierarchy info
            part_info = hierarchy.get(part_id)
            if not part_info:
                logger.warning("no_hierarchy_for_part", part_id=part_id)
                continue

            article_num = part_info.get("article_number")
            article_label = part_info.get("article_label", f"Artículo {article_num}")

            # Build base metadata for article
            base_chunk_metadata = base_metadata.copy() if base_metadata else {}

            # Build article-specific URL with idParte
            article_url = base_chunk_metadata.get("official_url", "")
            if article_url:
                # Convert Pydantic Url to string if needed
                article_url_str = str(article_url) if not isinstance(article_url, str) else article_url
                if part_id:
                    # Add idParte parameter to URL for direct article linking
                    if "?" in article_url_str:
                        article_url = f"{article_url_str}&idParte={part_id}"
                    else:
                        article_url = f"{article_url_str}?idParte={part_id}"
                else:
                    article_url = article_url_str

            # Extract validity/force info
            version_date = part_info.get("version_date")
            in_force = part_info.get("in_force", True)

            # Check for deferred validity (future version date)
            if BCNXMLParser.is_future_version(version_date):
                in_force = False  # Override: not yet in effect
                force_status = "deferred"
                logger.debug(
                    "deferred_validity_detected",
                    part_id=part_id,
                    article_number=article_num,
                    version_date=version_date
                )
            else:
                force_status = "active" if in_force else "repealed"

            # Get structural context for this article
            context = structural_context.get(part_id, {})

            base_chunk_metadata.update({
                "part_id": part_id,
                "article_number": article_num,
                "article_label": article_label,
                "parent_article": part_info.get("parent_article"),
                "is_nested": part_info.get("is_nested", False),
                "hierarchy_level": part_info.get("hierarchy_level", 1),
                "in_force": in_force,
                "force_status": force_status,
                "version_date": version_date,
                "official_url": article_url,  # Override with article-specific URL
                # Structural context (CRITICAL for filters and citations)
                "book": context.get("book"),
                "book_name": context.get("book_name"),
                "title_ordinal": context.get("title_ordinal"),
                "title_name": context.get("title_name"),
                "section": context.get("section"),
                "section_name": context.get("section_name"),
            })

            # Extract subdivisions (numerales, letras) as metadata ONLY
            # Keep article COMPLETE - do NOT split
            from pipeline.article_parser import extract_subdivisions_metadata
            subdivisions = extract_subdivisions_metadata(article_text)

            # Build contextual header with FULL structural path
            # Use · separator for cleaner hierarchy display
            contextual_header = self._build_contextual_header_with_dot_separator(
                norm_citation, norm_title, context, article_label
            )
            full_text = contextual_header + article_text

            # Build structured path for metadata
            structured_path = self._build_structured_path(context)

            # Build formatted citation
            formatted_citation = self._build_formatted_citation(
                norm_citation, article_label, context
            )

            # Build metadata with new structure
            chunk_metadata = base_chunk_metadata.copy()
            chunk_metadata.update({
                "literal_text": article_text,  # Article text without header
                "subdivisions": subdivisions,   # List of subdivision positions
                "path": structured_path,        # Hierarchical path as nested dict
                "formatted_citation": formatted_citation,  # Formal citation
            })

            tokens = self.estimate_tokens(full_text)
            if tokens <= self.article_max_size:
                chunks.append({
                    "text": full_text,
                    "metadata": chunk_metadata
                })
                logger.debug(
                    "article_chunk_created",
                    part_id=part_id,
                    article_number=article_num,
                    tokens=tokens,
                    subdivisions=len(subdivisions)
                )
            else:
                # Article too large (rare), split by paragraphs as fallback
                logger.warning(
                    "splitting_large_article",
                    part_id=part_id,
                    article_number=article_num,
                    tokens=tokens
                )
                sub_chunks_text = self._split_by_separator(article_text, r"\n\n")
                for sub_text in sub_chunks_text:
                    full_sub_text = contextual_header + sub_text
                    chunks.append({
                        "text": full_sub_text,
                        "metadata": chunk_metadata.copy()
                    })

        logger.info("xml_chunking_complete", total_chunks=len(chunks))
        return chunks

    def _extract_special_chunks(
        self,
        full_content: str,
        article_texts: Dict[str, str],
        base_metadata: Optional[Dict] = None
    ) -> List[Dict]:
        """
        Extract non-article content (preambulo, structural headers) as special chunks.

        Identifies:
        - Preambulo: Text before first article (decrees, signatures, certifications)
        - Structural headers: LIBRO, TÍTULO, §, etc.

        Args:
            full_content: Complete document text
            article_texts: Dict of part_id -> article text
            base_metadata: Base metadata to include

        Returns:
            List of special chunks with type metadata
        """
        special_chunks = []

        # Get norm metadata for contextual headers
        norm_citation = base_metadata.get("norm_type", "") + " N° " + str(base_metadata.get("norm_number", ""))
        norm_title = base_metadata.get("norm_title", "")

        # Combine all article texts to find what's NOT in articles
        all_article_text = "\n\n".join(article_texts.values())

        # Split full_content into paragraphs
        paragraphs = full_content.split('\n\n')

        # Track current structural context
        current_book = None
        current_title = None
        current_section = None

        preambulo_parts = []
        in_preambulo = True

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # FIRST: Check for structural headers (LIBRO, TÍTULO, §)
            # These must be detected BEFORE checking if in article
            is_structural_header = False
            header_type = None
            header_label = None

            # LIBRO pattern
            book_match = re.match(r'^LIBRO\s+(PRIMERO|SEGUNDO|TERCERO|CUARTO|QUINTO|I{1,4}|[IVX]+)', paragraph, re.IGNORECASE)
            if book_match:
                current_book = paragraph
                is_structural_header = True
                header_type = "book"
                header_label = paragraph

            # TÍTULO pattern (only if not LIBRO)
            if not is_structural_header:
                title_match = re.match(r'^TÍ?TULO\s+(PRIMERO|PRELIMINAR|I{1,4}|[IVX]+|\d+)', paragraph, re.IGNORECASE)
                if title_match:
                    current_title = paragraph
                    is_structural_header = True
                    header_type = "title"
                    header_label = paragraph

            # § (section/paragraph) pattern (only if not LIBRO or TÍTULO)
            if not is_structural_header:
                section_match = re.match(r'^§\s*(\d+|[IVX]+)', paragraph, re.IGNORECASE)
                if section_match:
                    current_section = paragraph
                    is_structural_header = True
                    header_type = "section"
                    header_label = paragraph

            # If structural header found, end preambulo and create header chunk
            if is_structural_header:
                # End preambulo if we're in it
                if in_preambulo and preambulo_parts:
                    preambulo_text = "\n\n".join(preambulo_parts)
                    contextual_header = f"{norm_citation}, {norm_title}, Preámbulo:\n\n"
                    full_text = contextual_header + preambulo_text

                    tokens = self.estimate_tokens(full_text)
                    chunk_metadata = base_metadata.copy() if base_metadata else {}
                    chunk_metadata.update({
                        "chunk_type": "preambulo",
                        "article_number": None,
                        "part_id": None,
                        "in_force": True,
                    })

                    special_chunks.append({
                        "text": full_text,
                        "metadata": chunk_metadata
                    })

                    logger.debug("preambulo_chunk_created", tokens=tokens)
                    preambulo_parts = []

                in_preambulo = False

                # Build contextual header with current structural hierarchy
                context_parts = [norm_citation, norm_title]
                if current_book and header_type != "book":
                    context_parts.append(current_book)
                if current_title and header_type not in ["book", "title"]:
                    context_parts.append(current_title)

                contextual_header = ", ".join(context_parts) + f", {header_label}:\n\n"
                full_text = contextual_header + paragraph

                tokens = self.estimate_tokens(full_text)
                chunk_metadata = base_metadata.copy() if base_metadata else {}
                chunk_metadata.update({
                    "chunk_type": header_type,
                    "structural_header": header_label,
                    "book": current_book,
                    "title_ordinal": current_title,
                    "section": current_section,
                    "article_number": None,
                    "part_id": None,
                    "in_force": True,
                })

                special_chunks.append({
                    "text": full_text,
                    "metadata": chunk_metadata
                })

                logger.debug(
                    "structural_header_chunk_created",
                    header_type=header_type,
                    header_label=header_label[:50],
                    tokens=tokens
                )

                continue  # Move to next paragraph

            # Not a structural header - check if in article or preambulo
            para_signature = paragraph[:100] if len(paragraph) > 100 else paragraph
            is_in_article = para_signature in all_article_text

            if is_in_article:
                # End of preambulo
                if in_preambulo and preambulo_parts:
                    preambulo_text = "\n\n".join(preambulo_parts)
                    contextual_header = f"{norm_citation}, {norm_title}, Preámbulo:\n\n"
                    full_text = contextual_header + preambulo_text

                    tokens = self.estimate_tokens(full_text)
                    chunk_metadata = base_metadata.copy() if base_metadata else {}
                    chunk_metadata.update({
                        "chunk_type": "preambulo",
                        "article_number": None,
                        "part_id": None,
                        "in_force": True,
                    })

                    special_chunks.append({
                        "text": full_text,
                        "metadata": chunk_metadata
                    })

                    logger.debug("preambulo_chunk_created", tokens=tokens)
                    preambulo_parts = []

                in_preambulo = False
                continue

            # Not structural header, not in article -> must be preambulo
            if in_preambulo:
                preambulo_parts.append(paragraph)

        # Handle remaining preambulo if document has no articles
        if in_preambulo and preambulo_parts:
            preambulo_text = "\n\n".join(preambulo_parts)
            contextual_header = f"{norm_citation}, {norm_title}, Preámbulo:\n\n"
            full_text = contextual_header + preambulo_text

            tokens = self.estimate_tokens(full_text)
            chunk_metadata = base_metadata.copy() if base_metadata else {}
            chunk_metadata.update({
                "chunk_type": "preambulo",
                "article_number": None,
                "part_id": None,
                "in_force": True,
            })

            special_chunks.append({
                "text": full_text,
                "metadata": chunk_metadata
            })

            logger.debug("preambulo_chunk_created_end", tokens=tokens)

        logger.info("special_chunks_extracted", total=len(special_chunks))
        return special_chunks

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
        """
        Split text by separator, respecting size limits.

        For legal text: if a paragraph exceeds max_chunk_size, split it
        into equal parts to preserve maximum context.
        """
        chunks = []
        current_chunk = ""
        current_tokens = 0

        parts = re.split(separator, text)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            part_tokens = self.estimate_tokens(part)

            # If this paragraph alone exceeds max_chunk_size
            if part_tokens > self.max_chunk_size:
                # Flush current chunk
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    current_tokens = 0

                # Split large paragraph into equal parts
                # Calculate how many sub-chunks we need
                num_subchunks = (part_tokens // self.max_chunk_size) + 1
                chars_per_chunk = len(part) // num_subchunks

                for i in range(num_subchunks):
                    start = i * chars_per_chunk
                    end = (i + 1) * chars_per_chunk if i < num_subchunks - 1 else len(part)
                    sub_part = part[start:end].strip()

                    if sub_part:
                        chunks.append(sub_part)

                continue

            # Normal case: try to add to current chunk
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

    def _build_contextual_header_with_dot_separator(
        self,
        norm_citation: str,
        norm_title: str,
        context: Dict,
        article_label: str
    ) -> str:
        """
        Build contextual header with · (middot) separators for hierarchy.

        Example: "Código Penal · Libro Segundo · Título Octavo · §1 bis · Artículo 390 ter\n\n"

        CRITICAL: Includes full structural names in text for BM25 and reranker.
        """
        parts = [norm_citation]

        # Add structural hierarchy with names
        if context.get("book"):
            book_full = context["book"]
            if context.get("book_name"):
                book_full += f" ({context['book_name']})"
            parts.append(book_full)

        if context.get("title_ordinal"):
            title_full = context["title_ordinal"]
            if context.get("title_name"):
                title_full += f" ({context['title_name']})"
            parts.append(title_full)

        if context.get("section"):
            section_full = context["section"]
            if context.get("section_name"):
                section_full += f" - {context['section_name']}"
            parts.append(section_full)

        # Add article
        parts.append(f"Artículo {article_label}")

        # Join with · separator
        header = " · ".join(parts)
        return f"{header}\n\n"

    def _build_structured_path(self, context: Dict) -> Dict:
        """
        Build structured path as nested dictionary.

        Returns:
            {
                "book": {"ordinal": "LIBRO SEGUNDO", "name": "..."},
                "title": {"ordinal": "TITULO OCTAVO", "name": "..."},
                "section": {"ordinal": "§1 bis", "name": "Del femicidio"}
            }
        """
        path = {}

        if context.get("book"):
            path["book"] = {
                "ordinal": context["book"],
                "name": context.get("book_name", "")
            }

        if context.get("title_ordinal"):
            path["title"] = {
                "ordinal": context["title_ordinal"],
                "name": context.get("title_name", "")
            }

        if context.get("section"):
            path["section"] = {
                "ordinal": context["section"],
                "name": context.get("section_name", "")
            }

        return path

    def _build_formatted_citation(
        self,
        norm_citation: str,
        article_label: str,
        context: Dict
    ) -> str:
        """
        Build formal legal citation.

        Example: "Código Penal, Artículo 390 ter"
        Example with structure: "Código Penal, Libro Segundo, Título Octavo, §1 bis, Artículo 390 ter"
        """
        citation_parts = [norm_citation]

        if context.get("book"):
            citation_parts.append(context["book"])
        if context.get("title_ordinal"):
            citation_parts.append(context["title_ordinal"])
        if context.get("section"):
            citation_parts.append(context["section"])

        citation_parts.append(f"Artículo {article_label}")

        return ", ".join(citation_parts)

    def _build_full_contextual_header(
        self,
        norm_citation: str,
        norm_title: str,
        context: Dict,
        article_label: str,
        subpart: Optional[any] = None
    ) -> str:
        """
        Build full contextual header with complete structural path.

        CRITICAL: This puts book/title/section NAMES in the text, not just metadata.

        Why in text:
        - BM25 lexical search: "femicidio" only exists in section_name
        - Reranker: reads text, not metadata
        - Embeddings: model embeds full context

        Example output:
        "Código Penal, Libro Segundo, Crímenes y simples delitos contra las personas,
         Del femicidio, artículo 390 ter:"

        Args:
            norm_citation: e.g., "Código Penal"
            norm_title: Full norm title
            context: Structural context dict with book/title/section
            article_label: e.g., "390 ter"
            subpart: Optional subpart (numeral or letra, NO inciso)

        Returns:
            Full contextual header string
        """
        path_parts = [norm_citation]

        # Add structural path with NAMES (not just ordinals)
        if context.get("book"):
            # Include both ordinal and name: "Libro Segundo, Crímenes y simples delitos..."
            book_full = context["book"]
            if context.get("book_name"):
                book_full += f", {context['book_name']}"
            path_parts.append(book_full)

        if context.get("title_ordinal"):
            # Include both ordinal and name
            title_full = context["title_ordinal"]
            if context.get("title_name"):
                title_full += f", {context['title_name']}"
            path_parts.append(title_full)

        if context.get("section"):
            # Include both ordinal and name: "§1 bis, Del femicidio"
            section_full = context["section"]
            if context.get("section_name"):
                section_full += f", {context['section_name']}"
            path_parts.append(section_full)

        # Add article
        path_parts.append(f"artículo {article_label}")

        # Add subpart if present (only numerales and letras, NO incisos)
        if subpart:
            if subpart.type == "numeral":
                path_parts.append(f"N°{subpart.number}")
            elif subpart.type == "letra":
                path_parts.append(f"letra {subpart.letter})")

        # Join with comma-space
        header = ", ".join(path_parts) + ":\n\n"

        return header


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
