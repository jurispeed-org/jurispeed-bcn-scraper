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
import unicodedata
import structlog
from typing import List, Dict, Optional, Tuple
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
        article_max_size: int = 7500,  # max size for a single article before splitting (safety margin for embeddings)
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

    def chunk(self, routing_text: str, metadata: Optional[Dict] = None, html: Optional[str] = None, norm_id: Optional[int] = None, xml_content: Optional[str] = None) -> List[Chunk]:
        """
        Chunk a norm.

        Args:
            routing_text: Plain text used ONLY for routing and for the fallback
                branches. It decides `_detect_articles()`, which selects the branch,
                and it is the text the fallback branches actually chunk. On the XML
                route it is not the chunk source: those chunks come from the parsed
                structure (`article_texts` / `hierarchy`), so perturbing this argument
                leaves them byte-identical -- see tests/test_chunking_text_decoupling.py.

                This is deliberately NOT documented as "the full text of the norm".
                Production passes `norm.chunking_text`, not `norm.full_content`
                (PR9): the two hold the same string today, but they are separate so
                that later making `full_content` complete (adding <Anexo> and
                <Promulgacion>) cannot silently change routing. Annex text carries
                "Articulo N" lines that flip `_detect_articles()` False -> True, which
                on the fallback route changes the branch and the chunks.
            metadata: Optional metadata to attach to all chunks
            html: Optional raw HTML for hierarchy extraction (legacy)
            norm_id: Optional norm ID for hierarchy extraction
            xml_content: Optional XML content (preferred over HTML)

        Returns:
            List of Chunk objects
        """
        if not routing_text or not routing_text.strip():
            raise ValueError("Text cannot be empty")

        text = self._normalize_text(routing_text)

        hierarchy = {}
        article_texts = {}
        binary_map = {}

        if xml_content and norm_id:
            try:
                from core.xml_parser import BCNXMLParser
                parser = BCNXMLParser()
                hierarchy = parser.extract_article_hierarchy(xml_content, norm_id)
                article_texts = parser.extract_article_texts(xml_content)
                binary_map = parser.extract_binary_content(xml_content, norm_id)
                logger.debug("xml_hierarchy_loaded", total_parts=len(hierarchy))
            except Exception as e:
                logger.warning("xml_hierarchy_extraction_failed", error=str(e))
        elif html and norm_id:
            try:
                from core.parser import BCNHtmlParser
                parser = BCNHtmlParser()
                hierarchy = parser.extract_article_hierarchy(html, norm_id)
                part_id_map = self._build_part_id_map(html, hierarchy)
                logger.debug("html_hierarchy_loaded", total_parts=len(hierarchy))
            except Exception as e:
                logger.warning("html_hierarchy_extraction_failed", error=str(e))

        has_articles = self._detect_articles(text)

        # PR4: the XML route is chosen from the real XML structure, not from counting
        # "Articulo N" in the text. _detect_articles() needs 3 textual matches, so a norm
        # with one or two articles used to fall through to the semantic fallback even
        # though its structure was fully available. Measured on the audit corpus: 378 of
        # 1,184 documents were routed by text instead of by structure.
        has_real_structure = self._has_real_structure(hierarchy)

        if hierarchy and article_texts and has_real_structure:
            logger.info("chunking_by_xml_idparte", total_parts=len(hierarchy))

            structural_context = {}
            if xml_content and norm_id:
                try:
                    from core.xml_parser import BCNXMLParser
                    parser = BCNXMLParser()
                    structural_context = parser.extract_structural_context(xml_content, norm_id)
                    logger.info("structural_context_loaded", articles_with_context=len(structural_context))
                except Exception as e:
                    logger.warning("structural_context_extraction_failed", error=str(e))

            article_chunks = self._chunk_by_xml(article_texts, hierarchy, metadata, structural_context, binary_map)

            # NO special chunks - ruta viaja CON el articulo
            chunks = article_chunks

            logger.info(
                "chunking_complete",
                total_chunks=len(chunks)
            )
        elif has_articles and hierarchy and html:
            logger.info("chunking_by_html_idparte", total_parts=len(hierarchy))
            chunks = self._chunk_by_idparte(html, hierarchy, metadata)
        elif has_articles:
            logger.info("chunking_legal_text_with_articles")
            chunks = self._chunk_by_articles(text)
        else:
            norm_type = metadata.get("norm_type", "").lower() if metadata else ""

            non_articulated_types = [
                "orden",           # Orden (like Carabineros)
                "ordenanza",       # Ordenanza municipal
                "oficio",          # Oficio
                "resolucion",      # Resolución
                "circular",        # Circular
                "instruccion",     # Instrucción
                "acuerdo",         # Acuerdo
                "sentencia",
                "certificado",
                "dictamen",
                "aviso",
                "bando",
                "notificacion",
                "mensaje",
                "otro",
            ]

            is_non_articulated = norm_type in non_articulated_types

            if is_non_articulated:
                # Restructure to return dicts with complete metadata
                estimated_tokens = self.estimate_tokens(text)

                norm_in_force = metadata.get("in_force", True) if metadata else True
                norm_citation = metadata.get("norm_citation", "") if metadata else ""
                norm_citation = self._get_display_citation(metadata, norm_citation)

                if estimated_tokens <= self.article_max_size:
                    chunks = [{
                        "text": text,
                        "metadata": {
                            "part_id": None,
                            "in_force": norm_in_force,
                            "force_status": "active" if norm_in_force else "repealed",
                            "is_transitory": False,
                            "article_label": None,
                            "article_number": None,
                            "is_nested": False,
                            "parent_article": None,
                            "formatted_citation": norm_citation,
                            "content_complete": True,
                        }
                    }]
                    logger.info(
                        "single_chunk_non_articulated",
                        norm_type=norm_type,
                        tokens=estimated_tokens
                    )
                else:
                    logger.info(
                        "chunking_non_articulated_by_sections",
                        norm_type=norm_type,
                        tokens=estimated_tokens
                    )
                    section_chunks = self._chunk_by_sections(text)
                    chunks = []
                    for idx, section_text in enumerate(section_chunks):
                        chunks.append({
                            "text": section_text,
                            "metadata": {
                                "part_id": f"section_{idx}",
                                "in_force": norm_in_force,
                                "force_status": "active" if norm_in_force else "repealed",
                                "is_transitory": False,
                                "article_label": None,
                                "article_number": None,
                                "is_nested": False,
                                "parent_article": None,
                                "formatted_citation": norm_citation,
                                "content_complete": True,
                                "is_fragment": len(section_chunks) > 1,
                                "fragment_index": idx + 1,
                                "total_fragments": len(section_chunks),
                            }
                        })
            else:
                logger.info("chunking_generic_text")
                chunks = self._chunk_by_semantic_boundaries(text)

        # Add overlap for context preservation (skip for article-based chunks)
        if not has_articles:
            chunks = self._add_overlap(chunks, text)

        chunk_objects = []
        for i, chunk_data in enumerate(chunks):
            if isinstance(chunk_data, dict):
                chunk_text = chunk_data["text"]
                chunk_metadata = chunk_data["metadata"]
            else:
                chunk_text = chunk_data
                chunk_metadata = metadata.copy() if metadata else {}

                if has_articles:
                    article_num = self._extract_article_number(chunk_text)
                    if article_num is not None:
                        chunk_metadata["article_number"] = article_num

                        if hierarchy and part_id_map:
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
                part_div = soup.find(id=part_id)
                if part_div:
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
            chunk_preview = chunk_text.strip()[:200]

            best_match = None
            best_score = 0

            for part_id, part_preview in part_id_map.items():
                part_info = hierarchy.get(part_id)
                if not part_info:
                    continue

                if part_info.get('article_number') != article_num:
                    continue

                chunk_clean = ''.join(chunk_preview.split())
                part_clean = ''.join(part_preview.split())

                if len(chunk_clean) < 50 or len(part_clean) < 50:
                    continue

                if chunk_clean.startswith(part_clean[:50]) or part_clean.startswith(chunk_clean[:50]):
                    score = 100
                else:
                    common = sum(1 for c in chunk_clean[:100] if c in part_clean[:100])
                    score = common

                if score > best_score:
                    best_score = score
                    best_match = part_info

            if best_match and best_score > 30:
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
        text = text.strip()
        text = re.sub(r" +", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _detect_articles(self, text: str) -> bool:
        """Detect if text contains legal articles."""
        patterns = [
            r"art[ií]culo\s+\d+",  # Artículo/ARTÍCULO/articulo with number
            r"art\.\s+\d+",         # Art. with number (marginal notes)
            r"art[ií]culo\s+[IVXLCDM]+",  # Roman numerals
        ]

        matches = 0
        for pattern in patterns:
            matches += len(re.findall(pattern, text, re.IGNORECASE))

        return matches >= 3

    def _has_real_structure(self, hierarchy: dict) -> bool:
        """
        Whether the hierarchy holds content of its own, beyond the PR3 synthetic parts.

        Since PR3, <Encabezado> and <Promulgacion> enter the hierarchy as synthetic
        parts, so a non-empty hierarchy no longer proves the norm has articles: a
        document whose only content is an 81-character header would satisfy
        `bool(hierarchy)` and would lose its `routing_text` to the XML route. Norm 15989 is
        a real instance of that in the audit corpus.

        Entries without a content_type (the legacy HTML hierarchy, and anything built
        before PR3) count as real, matching the "article" default used when chunk
        metadata is assembled.
        """
        from core.xml_parser import BCNXMLParser

        synthetic = {
            BCNXMLParser.CONTENT_TYPE_HEADER,
            BCNXMLParser.CONTENT_TYPE_PROMULGATION,
        }
        return any(
            info.get("content_type") not in synthetic for info in hierarchy.values()
        )

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
        norm_type_raw = base_metadata.get("norm_type", "ley")
        norm_type_display = self._format_norm_type_for_citation(norm_type_raw)
        # Use pre-built citation from metadata, or fallback to constructing it
        norm_citation = base_metadata.get("norm_citation") or (norm_type_display + " N° " + str(base_metadata.get("norm_number", "")))
        norm_citation = self._get_display_citation(base_metadata, norm_citation)
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
            # Handle annexes (article_number=None, article_label="Anexo")
            article_label = part_info.get("article_label")
            if not article_label:
                article_label = f"Artículo {article_num}" if article_num else "Contenido"

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
                "is_transitory": part_info.get("is_transitory", False),
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

    def _split_annexes_by_treaty_article(
        self,
        article_texts: Dict[str, str],
        hierarchy: dict
    ) -> Tuple[Dict[str, str], dict]:
        """
        Split treaty annexes (Anexo) into one block per treaty article.

        Annex text from BCN XML contains all treaty articles as plain text in a
        single block (e.g. Tratado de Escazu has 26 articles in one <Texto>).
        Without splitting, every chunk gets article_label="Anexo" with no way to
        distinguish which treaty article it belongs to (Bug #3).

        Detects "Articulo N" boundaries and creates synthetic part_ids/hierarchy
        entries per treaty article, so the normal per-article chunking logic
        (including subdivision detection) runs independently on each block.

        PR12: the sub-parts take the POSITION of the annex they came from. Until PR12 this
        deleted `anexo_X` and assigned the sub-keys, and a new dict key always lands at the
        end -- so the sub-parts jumped behind `promulgation_{id}`, which PR3 inserts last.
        Since _chunk_by_xml() iterates article_texts in insertion order, the promulgation
        chunk ended up in the middle of the treaty (index 2 of 12 on norm 252869), and after
        PR11 the stored `full_content` and the stored `chunks[]` of the same document
        disagreed about the order in 62 of 85 measured annex documents.

        So the split now runs in two passes: collect the sub-parts, then rebuild the dicts
        in the original key order, expanding each split annex where it already was. Nothing
        else changes -- not the boundaries, not the block text, not the keys, not the PR7
        de-duplication, not the order of the sub-parts among themselves (which follows the
        text, not the article numbers). The change is purely positional.
        """
        article_boundary = re.compile(r'(?=\bArt[ií]culo\s+\d+\b)', re.IGNORECASE)

        # part_id -> [(sub_key, block, sub_info)], in the order the splitter produces them.
        splits: Dict[str, List[Tuple[str, str, dict]]] = {}

        for part_id, part_info in hierarchy.items():
            if not part_info.get("is_annex"):
                continue

            text = article_texts.get(part_id)
            if not text:
                continue

            blocks = [b.strip() for b in article_boundary.split(text) if b.strip()]
            if len(blocks) <= 1:
                continue  # No detectable per-article structure, keep as single annex block

            # PR7. The sub_key must be unique or blocks silently overwrite each other in
            # new_article_texts and the earlier block is lost with no warning. Two
            # mechanisms did exactly that, both measured (see
            # tests/test_annex_extraction.py):
            #
            #   1. A block with no "Articulo N" fell back to str(i + 1). For the text
            #      before the first article -- the annex preamble, its title, or a tariff
            #      table -- that is "1", which collides with the real Articulo 1 and gets
            #      overwritten by it. This is the common shape: an annex usually opens with
            #      a preamble.
            #   2. An annex holding more than one treaty restarts numbering, so a second
            #      "Articulo 1" collides with the first.
            #
            # Only the key construction changes. Block boundaries, block text and the
            # labels of blocks that do carry a number are untouched.
            used_keys = set()
            sub_parts = []

            for i, block in enumerate(blocks):
                match = re.match(r'Art[ií]culo\s+(\d+)', block, re.IGNORECASE)

                if match:
                    treaty_article_num = match.group(1)
                    sub_key = f"{part_id}_art{treaty_article_num}"
                    article_label = f"Anexo, Artículo {treaty_article_num}"
                else:
                    # Not an article, so it must not be numbered as one.
                    sub_key = f"{part_id}_intro" if i == 0 else f"{part_id}_block{i}"
                    article_label = part_info.get("article_label") or "Anexo"

                # A repeat keeps its own entry instead of replacing the earlier one.
                if sub_key in used_keys:
                    suffix = 2
                    while f"{sub_key}_{suffix}" in used_keys:
                        suffix += 1
                    sub_key = f"{sub_key}_{suffix}"
                used_keys.add(sub_key)

                sub_info = part_info.copy()
                sub_info["article_label"] = article_label
                sub_parts.append((sub_key, block, sub_info))

            splits[part_id] = sub_parts

        if not splits:
            return dict(article_texts), dict(hierarchy)

        # Rebuild both dicts in their original key order, expanding a split annex in place.
        # The two are rebuilt independently on purpose: a part_id can exist in one and not
        # in the other (a known pre-existing asymmetry), and rebuilding one from the other's
        # order would silently drop those entries.
        new_article_texts: Dict[str, str] = {}
        for key, text in article_texts.items():
            if key in splits:
                for sub_key, block, _ in splits[key]:
                    new_article_texts[sub_key] = block
            else:
                new_article_texts[key] = text

        new_hierarchy = {}
        for key, info in hierarchy.items():
            if key in splits:
                for sub_key, _, sub_info in splits[key]:
                    new_hierarchy[sub_key] = sub_info
            else:
                new_hierarchy[key] = info

        return new_article_texts, new_hierarchy

    def _chunk_by_xml(
        self,
        article_texts: Dict[str, str],
        hierarchy: dict,
        base_metadata: Optional[Dict] = None,
        structural_context: Optional[Dict[str, Dict]] = None,
        binary_map: Optional[Dict[str, dict]] = None
    ) -> List[Dict]:
        """
        Chunk by XML article texts with full structural context and binary content detection.

        Args:
            article_texts: Dict mapping idParte -> article text
            hierarchy: Dict mapping idParte -> article metadata
            base_metadata: Base metadata (norm info) to include in all chunks
            structural_context: Dict mapping idParte -> structural context (book/title/section)
            binary_map: Dict mapping idParte -> binary content info (images/tables)

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

        article_texts, hierarchy = self._split_annexes_by_treaty_article(article_texts, hierarchy)

        chunks = []
        structural_context = structural_context or {}

        # Get norm metadata for contextual headers
        norm_type_raw = base_metadata.get("norm_type", "ley")
        norm_type_display = self._format_norm_type_for_citation(norm_type_raw)
        # Use pre-built citation from metadata, or fallback to constructing it
        norm_citation = base_metadata.get("norm_citation") or (norm_type_display + " N° " + str(base_metadata.get("norm_number", "")))
        norm_citation = self._get_display_citation(base_metadata, norm_citation)
        norm_title = base_metadata.get("norm_title", "")

        for part_id, article_text in article_texts.items():
            # Get hierarchy info
            part_info = hierarchy.get(part_id)
            if not part_info:
                logger.warning("no_hierarchy_for_part", part_id=part_id)
                continue

            article_num = part_info.get("article_number")
            # Handle annexes (article_number=None, article_label="Anexo")
            article_label = part_info.get("article_label")
            if not article_label:
                article_label = f"Artículo {article_num}" if article_num else "Contenido"

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
            # Try both string and int keys to handle mismatch
            context = structural_context.get(part_id, {})
            if not context and structural_context:
                # Try alternative key type (string vs int)
                alt_key = str(part_id) if isinstance(part_id, int) else int(part_id) if part_id.isdigit() else None
                if alt_key:
                    context = structural_context.get(alt_key, {})

                # Log warning if still empty after trying both types
                if not context:
                    logger.warning(
                        "no_structural_context_for_article",
                        part_id=part_id,
                        part_id_type=type(part_id).__name__,
                        available_ids_sample=list(structural_context.keys())[:5],
                        total_articles_with_context=len(structural_context)
                    )

            is_transitory = part_info.get("is_transitory", False)
            descriptor = self._article_descriptor(article_label, is_transitory)
            # Transitory provisions carry no number of their own, which left
            # article_number null and made them invisible to numeric filters and
            # ordering. The ordinal derived from the label fills that gap; it is
            # unique only when paired with is_transitory, since permanent art. 41
            # and the 41st transitory provision share the number.
            if article_num is None and descriptor["ordinal"] is not None:
                article_num = descriptor["ordinal"]
            if article_num is None:
                # Many decretos number their permanent articulado with spelled-out
                # ordinals ("Artículo primero"), which leaves no digits to parse.
                # The label itself is kept verbatim; only the numeric field is filled.
                article_num = self._spanish_ordinal_to_int(article_label)

            base_chunk_metadata.update({
                "part_id": part_id,
                "article_number": article_num,
                "article_label": article_label,
                # Canonical spelling, since BCN's own labels are inconsistent
                "article_label_normalized": descriptor["normalized_label"],
                "parent_article": part_info.get("parent_article"),
                "is_nested": part_info.get("is_nested", False),
                "hierarchy_level": part_info.get("hierarchy_level", 1),
                "in_force": in_force,
                "force_status": force_status,
                "is_transitory": is_transitory,
                # What kind of content this chunk holds: article, transitory, annex,
                # header or promulgation. Defaults to "article" so any part built before
                # PR3 keeps its previous meaning.
                "content_type": part_info.get("content_type", "article"),
                # PR5 observability: the part's text still ends mid-sentence in the XML
                # this chunk came from, which in production is already post-repair. It is
                # not is_repaired -- a successfully repaired part is indistinguishable
                # from one that was never truncated. Read from the hierarchy so there is
                # a single source of truth (BCNXMLParser.is_part_truncated); defaults to
                # False for any hierarchy built before PR5.
                "is_truncated": part_info.get("is_truncated", False),
                # PR6 observability: what _strip_margin_notes() removed from this part's
                # text. Copied from the hierarchy, never recomputed, so there is one
                # measurement per part. None for a hierarchy built before PR6.
                "margin_notes": part_info.get("margin_notes"),
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
                norm_citation, norm_title, context, article_label, is_transitory
            )
            full_text = contextual_header + article_text

            # Build formatted citation
            formatted_citation = self._build_formatted_citation(
                norm_citation, article_label, context, is_transitory
            )

            # Build metadata with new structure
            chunk_metadata = base_chunk_metadata.copy()
            chunk_metadata.update({
                "formatted_citation": formatted_citation,  # Formal citation
            })

            # Check for binary content (images/tables) in this article
            binary_info = binary_map.get(part_id) if binary_map else None
            if binary_info:
                # Add binary content metadata
                chunk_metadata["binary_content"] = binary_info
                chunk_metadata["content_complete"] = False

                # Warn inside the chunk text, not only in metadata: a model that
                # only sees the text would otherwise describe the article as if
                # the annex did not exist. The link is the article-level deep
                # link, which opens the attachment in BCN's official viewer.
                attachments = binary_info.get("attachments") or []
                names = ", ".join(
                    a["filename"] for a in attachments if a.get("filename")
                )
                detail = f" ({names})" if names else ""
                binary_note = (
                    f"\n\n[NOTA: Este artículo contiene contenido binario "
                    f"(tabla o imagen escaneada) no indexado{detail}. "
                    f"Para consultarlo, ver el documento oficial: {article_url}]"
                )
                full_text += binary_note

                logger.debug(
                    "binary_content_detected_in_chunk",
                    part_id=part_id,
                    article_number=article_num,
                    binary_type=binary_info.get("type")
                )

            tokens = self.estimate_tokens(full_text)
            # Force splitting if article has many subdivisions (5+)
            # This ensures granular retrieval even if article fits within size limit
            SUBDIVISION_SPLIT_THRESHOLD = 5
            has_many_subdivisions = subdivisions and len(subdivisions) >= SUBDIVISION_SPLIT_THRESHOLD

            if tokens <= self.article_max_size and not has_many_subdivisions:
                # Article fits and has few subdivisions: keep as single chunk
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
                # Article needs splitting: either too large OR has many subdivisions
                if subdivisions and len(subdivisions) > 0:
                    # Split by complete subdivisions (numerales, letras)
                    split_reason = "many_subdivisions" if has_many_subdivisions and tokens <= self.article_max_size else "size_limit"
                    logger.info(
                        "splitting_by_subdivisions",
                        part_id=part_id,
                        article_number=article_num,
                        tokens=tokens,
                        subdivisions_count=len(subdivisions),
                        reason=split_reason
                    )

                    # Text before the first subdivision (e.g. the governing clause
                    # introducing a numeral list) and after the last one is NOT
                    # covered by any subdivision span. Without handling it here it
                    # gets silently dropped from every chunk in this article.
                    preamble_text = article_text[:subdivisions[0]['start']].strip()
                    trailing_text = article_text[subdivisions[-1]['end']:].strip()

                    subdivision_series = self._assign_subdivision_series(subdivisions)

                    for subdiv_idx, subdiv in enumerate(subdivisions):
                        # Extract subdivision text using positions
                        subdiv_text = article_text[subdiv['start']:subdiv['end']]
                        if subdiv_idx == 0 and preamble_text:
                            subdiv_text = preamble_text + "\n" + subdiv_text

                        # Build contextual header with fragment indicator
                        subdivision_context = self._build_subdivision_context(
                            norm_citation, norm_title, context, article_label, subdiv['mark'],
                            parent_numeral=subdiv.get('parent_numeral'),
                            is_transitory=is_transitory
                        )
                        full_sub_text = subdivision_context + subdiv_text

                        # Check if this single subdivision is too large
                        subdiv_tokens = self.estimate_tokens(full_sub_text)

                        if subdiv_tokens > self.article_max_size:
                            # Subdivision itself is too large, split by paragraphs
                            logger.warning(
                                "subdivision_too_large_splitting_by_paragraphs",
                                part_id=part_id,
                                article_number=article_num,
                                subdivision=subdiv['mark'],
                                tokens=subdiv_tokens
                            )

                            # Split subdivision text by paragraphs
                            subdiv_paragraphs = self._split_by_separator(subdiv_text, r"\n\n")

                            for para_idx, para_text in enumerate(subdiv_paragraphs):
                                # Rebuild full text with context header
                                if len(subdiv_paragraphs) > 1:
                                    paragraph_note = (
                                        f"[Parte {para_idx + 1} de {len(subdiv_paragraphs)}]\n\n"
                                    )
                                else:
                                    paragraph_note = ""
                                full_para_text = subdivision_context + paragraph_note + para_text

                                # Add binary note to last paragraph of last subdivision if present
                                if binary_info and subdiv_idx == len(subdivisions) - 1 and para_idx == len(subdiv_paragraphs) - 1:
                                    binary_note = (
                                        "\n\n[NOTA: Este artículo contiene contenido binario (tabla o imagen) "
                                        "no indexado. Para ver el contenido completo, consulte el documento "
                                        "original en el sitio oficial de la Biblioteca del Congreso Nacional.]"
                                    )
                                    full_para_text += binary_note

                                # Mark as paragraph fragment of subdivision
                                para_metadata = chunk_metadata.copy()
                                para_metadata.update({
                                    "is_fragment": True,
                                    "fragment_of": article_label,
                                    "subdivision_mark": subdiv['mark'],
                                    "subdivision_type": subdiv['type'],
                                    "subdivision_index": subdiv_idx + 1,
                                    "total_subdivisions": len(subdivisions),
                                    "parent_numeral": subdiv.get('parent_numeral'),
                                    "paragraph_fragment": True,
                                    "paragraph_index": para_idx + 1,
                                    "total_paragraphs": len(subdiv_paragraphs)
                                })

                                # Update formatted_citation with subdivision (include parent
                                # numeral when this letra is nested under one)
                                base_citation = chunk_metadata.get("formatted_citation", "")
                                subdiv_citation_part = self._build_subdivision_citation_part(
                                    subdiv, *subdivision_series[subdiv_idx]
                                )
                                if len(subdiv_paragraphs) > 1:
                                    # Without this, every paragraph fragment of the same
                                    # subdivision would carry an identical citation, making
                                    # them indistinguishable for exact-citation lookups.
                                    subdiv_citation_part += f" (parte {para_idx + 1} de {len(subdiv_paragraphs)})"
                                para_metadata["formatted_citation"] = f"{base_citation}, {subdiv_citation_part}"

                                chunks.append({
                                    "text": full_para_text,
                                    "metadata": para_metadata
                                })

                                logger.debug(
                                    "subdivision_paragraph_chunk_created",
                                    part_id=part_id,
                                    article_number=article_num,
                                    subdivision=subdiv['mark'],
                                    paragraph=f"{para_idx+1}/{len(subdiv_paragraphs)}",
                                    tokens=self.estimate_tokens(full_para_text)
                                )
                        else:
                            # Subdivision within limit, use as single chunk
                            # Add binary note to last subdivision if present
                            if binary_info and subdiv_idx == len(subdivisions) - 1:
                                binary_note = (
                                    "\n\n[NOTA: Este artículo contiene contenido binario (tabla o imagen) "
                                    "no indexado. Para ver el contenido completo, consulte el documento "
                                    "original en el sitio oficial de la Biblioteca del Congreso Nacional.]"
                                )
                                full_sub_text += binary_note

                            # Clone metadata and mark as fragment
                            sub_metadata = chunk_metadata.copy()
                            sub_metadata.update({
                                "is_fragment": True,
                                "fragment_of": article_label,
                                "subdivision_mark": subdiv['mark'],
                                "subdivision_type": subdiv['type'],
                                "subdivision_index": subdiv_idx + 1,
                                "total_subdivisions": len(subdivisions),
                                "parent_numeral": subdiv.get('parent_numeral')
                            })

                            # Update formatted_citation with subdivision (include parent
                            # numeral when this letra is nested under one)
                            base_citation = chunk_metadata.get("formatted_citation", "")
                            subdiv_citation_part = self._build_subdivision_citation_part(
                                subdiv, *subdivision_series[subdiv_idx]
                            )
                            sub_metadata["formatted_citation"] = f"{base_citation}, {subdiv_citation_part}"

                            chunks.append({
                                "text": full_sub_text,
                                "metadata": sub_metadata
                            })

                            logger.debug(
                                "subdivision_chunk_created",
                                part_id=part_id,
                                article_number=article_num,
                                subdivision=subdiv['mark'],
                                tokens=subdiv_tokens
                            )

                    # Text after the last subdivision (e.g. closing paragraphs not
                    # part of the numeral/letra list) is semantically distinct from
                    # the last subdivision -- emit it as its own chunk instead of
                    # silently dropping it or gluing it onto an unrelated numeral.
                    if trailing_text:
                        trailing_base_citation = chunk_metadata.get("formatted_citation", "")
                        trailing_pieces = [trailing_text]
                        if self.estimate_tokens(contextual_header + trailing_text) > self.article_max_size:
                            trailing_pieces = self._split_by_separator(trailing_text, r"\n\n")

                        for t_idx, t_text in enumerate(trailing_pieces):
                            trailing_metadata = chunk_metadata.copy()
                            trailing_citation = f"{trailing_base_citation} (texto final)"
                            if len(trailing_pieces) > 1:
                                trailing_citation += f" (parte {t_idx + 1} de {len(trailing_pieces)})"
                            trailing_metadata.update({
                                "is_fragment": True,
                                "fragment_of": article_label,
                                "trailing_fragment": True,
                                "fragment_index": t_idx + 1,
                                "total_fragments": len(trailing_pieces),
                                "formatted_citation": trailing_citation,
                            })
                            note = f"\n\n[Texto final del artículo, fuera de la lista de numerales/letras]\n\n"
                            chunks.append({
                                "text": contextual_header + note + t_text,
                                "metadata": trailing_metadata
                            })

                        logger.info(
                            "trailing_text_after_subdivisions_captured",
                            part_id=part_id,
                            article_number=article_num,
                            trailing_chars=len(trailing_text),
                            pieces=len(trailing_pieces)
                        )
                else:
                    # No subdivisions detected, fallback to paragraph split
                    logger.warning(
                        "splitting_large_article_no_subdivisions",
                        part_id=part_id,
                        article_number=article_num,
                        tokens=tokens
                    )
                    sub_chunks_text = self._split_by_separator(article_text, r"\n\n")
                    for sub_idx, sub_text in enumerate(sub_chunks_text):
                        fragment_note = (
                            f"\n\n[Fragmento {sub_idx + 1} de {len(sub_chunks_text)} "
                            f"del artículo completo]\n\n"
                        )
                        full_sub_text = contextual_header + fragment_note + sub_text

                        # Add binary note to last sub-chunk if present
                        if binary_info and sub_idx == len(sub_chunks_text) - 1:
                            binary_note = (
                                "\n\n[NOTA: Este artículo contiene contenido binario (tabla o imagen) "
                                "no indexado. Para ver el contenido completo, consulte el documento "
                                "original en el sitio oficial de la Biblioteca del Congreso Nacional.]"
                            )
                            full_sub_text += binary_note

                        # Mark as fragment split by paragraphs
                        sub_metadata = chunk_metadata.copy()
                        sub_metadata.update({
                            "is_fragment": True,
                            "fragment_of": article_label,
                            "paragraph_fragment": True,
                            "fragment_index": sub_idx + 1,
                            "total_fragments": len(sub_chunks_text)
                        })
                        if len(sub_chunks_text) > 1:
                            # Without this, every fragment of this article would carry the
                            # exact same citation, making them indistinguishable.
                            base_citation = chunk_metadata.get("formatted_citation", "")
                            sub_metadata["formatted_citation"] = (
                                f"{base_citation} (parte {sub_idx + 1} de {len(sub_chunks_text)})"
                            )

                        chunks.append({
                            "text": full_sub_text,
                            "metadata": sub_metadata
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
        norm_type_raw = base_metadata.get("norm_type", "ley")
        norm_type_display = self._format_norm_type_for_citation(norm_type_raw)
        # Use pre-built citation from metadata, or fallback to constructing it
        norm_citation = base_metadata.get("norm_citation") or (norm_type_display + " N° " + str(base_metadata.get("norm_number", "")))
        norm_citation = self._get_display_citation(base_metadata, norm_citation)
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

            if para_tokens > self.max_chunk_size:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    current_tokens = 0

                sub_chunks = self._split_by_separator(para, r"\.\s+")
                chunks.extend(sub_chunks)
                continue

            if current_tokens + para_tokens <= self.target_chunk_size:
                current_chunk += "\n\n" + para if current_chunk else para
                current_tokens += para_tokens

            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())

                current_chunk = para
                current_tokens = para_tokens

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

            if part_tokens > self.max_chunk_size:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    current_tokens = 0

                num_subchunks = (part_tokens // self.max_chunk_size) + 1
                chars_per_chunk = len(part) // num_subchunks

                current_pos = 0

                # NOTE: num_subchunks is only an ESTIMATE used to size chars_per_chunk.
                # The loop must run until current_pos reaches len(part), not stop once
                # num_subchunks pieces are produced -- otherwise the final tail of text
                # (whatever didn't fit in the estimated number of pieces) gets silently
                # dropped. This previously truncated the last fragment of large articles
                # (e.g. Disposicion Transitoria text with no "\n\n" breaks).
                while current_pos < len(part):
                    target_end = min(current_pos + chars_per_chunk, len(part))

                    if target_end < len(part):
                        boundary = target_end
                        for i in range(target_end, max(current_pos, target_end - 100), -1):
                            if part[i] in [' ', '\n', '.', ',', ';', ':', ')']:
                                boundary = i + 1
                                break

                        if boundary == target_end and target_end - current_pos > 100:
                            boundary = target_end
                    else:
                        boundary = target_end

                    sub_part = part[current_pos:boundary].strip()

                    if sub_part:
                        chunks.append(sub_part)

                    current_pos = boundary

                continue

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

    def _chunk_by_sections(self, text: str) -> List[str]:
        """
        Chunk non-articulated norms by structural sections.

        Used for norms without articles (orden, ordenanza, oficio, etc.)
        that exceed article_max_size (8192 tokens).

        Detects sections like:
        - "Vistos:"
        - "Considerando:"
        - "Se ordena:" / "Resuelve:" / "Decreta:" / "Acuerda:"

        Each section becomes a complete chunk (no mid-section cuts).

        Returns:
            List of text chunks, one per section
        """
        chunks = []

        # Section markers (Spanish legal document structure)
        section_patterns = [
            (r'\n\s*(Vistos?:)', 'Vistos'),
            (r'\n\s*(Considerando:)', 'Considerando'),
            (r'\n\s*(Se\s+ordena:)', 'Se ordena'),
            (r'\n\s*(Resuelve:)', 'Resuelve'),
            (r'\n\s*(Decreta:)', 'Decreta'),
            (r'\n\s*(Acuerda:)', 'Acuerda'),
            (r'\n\s*(Dispone:)', 'Dispone'),
        ]

        section_positions = []
        for pattern, section_name in section_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                section_positions.append((match.start(), section_name))

        if not section_positions:
            logger.warning("no_sections_detected_fallback")
            return self._chunk_by_semantic_boundaries(text)

        section_positions.sort(key=lambda x: x[0])

        for i, (start_pos, section_name) in enumerate(section_positions):
            if i < len(section_positions) - 1:
                end_pos = section_positions[i + 1][0]
            else:
                end_pos = len(text)

            section_text = text[start_pos:end_pos].strip()

            tokens = self.estimate_tokens(section_text)

            if tokens <= self.article_max_size:
                chunks.append(section_text)
                logger.debug(
                    "section_chunk_created",
                    section=section_name,
                    tokens=tokens
                )
            else:
                logger.warning(
                    "splitting_large_section",
                    section=section_name,
                    tokens=tokens
                )
                sub_chunks = self._chunk_by_semantic_boundaries(section_text)
                chunks.extend(sub_chunks)

        logger.info(
            "chunking_by_sections_complete",
            total_sections=len(section_positions),
            total_chunks=len(chunks)
        )

        return chunks

    def _add_overlap(self, chunks: List[str], original_text: str) -> List[str]:
        """
        Add overlap between chunks for context preservation.

        Takes last N tokens from previous chunk and prepends to next chunk.

        Only plain-text chunks can be overlapped. A structured chunk is a dict, and
        `f"{overlap} [...] {chunk}"` would serialize it into its own repr: the chunk
        stops being a dict, so the assembly loop in chunk() falls back to the norm-level
        metadata and silently drops part_id, formatted_citation, in_force and
        content_type. _get_last_n_tokens() does not raise on a dict either -- len({...})
        is 2, which is below the character cutoff, so the dict is returned as-is and
        interpolated. The failure is silent in both directions.

        PR4 needs this because the flipped route sends structured chunks down a path
        where has_articles is False. It also covers a pre-existing instance of the same
        bug: the non-articulated branch already built dict chunks and already reached
        this method. Fixing both at once is unavoidable -- they are the same line -- and
        is intentional, not an accidental widening of scope.
        """
        if any(isinstance(chunk, dict) for chunk in chunks):
            logger.debug("overlap_skipped_structured_chunks", total_chunks=len(chunks))
            return chunks

        if len(chunks) <= 1 or self.overlap_tokens == 0:
            return chunks

        overlapped = []

        for i, chunk in enumerate(chunks):
            if i == 0:
                overlapped.append(chunk)
                continue

            prev_chunk = chunks[i - 1]
            overlap_text = self._get_last_n_tokens(prev_chunk, self.overlap_tokens)

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
        article_label: str,
        is_transitory: bool = False
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
        parts.append(self._article_descriptor(article_label, is_transitory)["header"])

        # Join with · separator
        header = " · ".join(parts)
        return f"{header}\n\n"

    def _build_subdivision_context(
        self,
        norm_citation: str,
        norm_title: str,
        context: Dict,
        article_label: str,
        subdivision_mark: str,
        parent_numeral: Optional[str] = None,
        is_transitory: bool = False
    ) -> str:
        """
        Build contextual header for article subdivisions (fragments).

        This creates self-contained chunks where each subdivision includes
        full context about the norm, article, and position.

        Example output:
            "Decreto Ley N° 824 · TÍTULO VI (Disposiciones especiales relativas al
            mercado de capitales) · Artículo 104

            [Fragmento del artículo completo - Numeral 1.-]

            "

        Args:
            norm_citation: Formal citation (e.g., "Decreto Ley N° 824")
            norm_title: Norm title for context
            context: Structural context (book, title, section)
            article_label: Article identifier (e.g., "104")
            subdivision_mark: Subdivision marker (e.g., "1.-", "a)")

        Returns:
            Contextual header string with fragment indicator
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
        parts.append(self._article_descriptor(article_label, is_transitory)["header"])

        # Build header with fragment indicator
        header = " · ".join(parts)

        # Determine subdivision type from mark
        subdiv_type = "Numeral" if subdivision_mark[0].isdigit() else "Letra"

        subdiv_descriptor = f"{subdiv_type} {subdivision_mark}"
        if parent_numeral:
            # Letra nested under a numeral: keep the numeral visible in the header
            # instead of dropping it (e.g. "Numeral 1.- > Letra a)").
            subdiv_descriptor = f"Numeral {parent_numeral} > {subdiv_descriptor}"

        fragment_note = f"\n\n[Fragmento del artículo completo - {subdiv_descriptor}]\n\n"

        return header + fragment_note

    def _get_display_citation(self, base_metadata: Optional[Dict], formal_citation: str) -> str:
        """
        Prefer the norm's common/popular name (e.g. "Constitucion Politica De La
        Republica De Chile") over the formal type+number citation (e.g. "Decreto
        N° 100") for the text that gets embedded and searched -- the formal
        citation alone doesn't let a search for "Constitucion" match its chunks.

        common_name comes from BCN as raw uppercase with no accents; title-case it
        for readability (accents can't be recovered from the source).
        """
        common_name = (base_metadata or {}).get("common_name")
        if common_name:
            return common_name.title()
        return formal_citation

    def _format_norm_type_for_citation(self, norm_type: str) -> str:
        """
        Format norm_type for formal citations.

        Converts enum values to proper display format:
        - "ley" → "Ley"
        - "codigo" → "Código"
        - "orden" → "Orden"
        - "decreto_ley" → "Decreto Ley"

        Args:
            norm_type: Raw norm_type value (from enum.value)

        Returns:
            Formatted type for citation display
        """
        # Mapping for special cases
        SPECIAL_FORMATS = {
            "dfl": "DFL",
            "decreto_ley": "Decreto Ley",
            "decreto_supremo": "Decreto Supremo",
            "ordenanza_municipal": "Ordenanza Municipal",
            "auto_acordado": "Auto Acordado",
        }

        if norm_type in SPECIAL_FORMATS:
            return SPECIAL_FORMATS[norm_type]

        # Default: capitalize first letter, replace underscores
        # "ley" → "Ley", "codigo" → "Codigo"
        formatted = norm_type.replace("_", " ").capitalize()

        # Add accent to "Código" if needed
        if formatted.lower() == "codigo":
            formatted = "Código"

        # Add accent to "Resolución" if needed
        if formatted.lower() == "resolucion":
            formatted = "Resolución"

        # Add accent to "Instrucción" if needed
        if formatted.lower() == "instruccion":
            formatted = "Instrucción"

        return formatted

    # Transitory provisions are labelled with spelled-out ordinals instead of a
    # number, and BCN spells them inconsistently: the Constitución alone uses 56
    # variants mixing case, accents and word breaks ("VIGESIMOSEGUNDA",
    # "VIGÉSIMO OCTAVA", "VIGÉSIMOCUARTA", "Trigésima octava"). Stems are matched
    # accent- and gender-insensitively so all of them resolve to one ordinal.
    _ORDINAL_TENS_STEMS = {
        'decim': 10, 'vigesim': 20, 'trigesim': 30, 'cuadragesim': 40,
        'quincuagesim': 50, 'sexagesim': 60, 'septuagesim': 70,
        'octogesim': 80, 'nonagesim': 90,
    }
    _ORDINAL_UNIT_STEMS = {
        'primer': 1, 'segund': 2, 'tercer': 3, 'cuart': 4, 'quint': 5,
        'sext': 6, 'septim': 7, 'octav': 8, 'noven': 9,
    }
    _ORDINAL_TENS_WORDS = {
        10: 'décima', 20: 'vigésima', 30: 'trigésima', 40: 'cuadragésima',
        50: 'quincuagésima', 60: 'sexagésima', 70: 'septuagésima',
        80: 'octogésima', 90: 'nonagésima',
    }
    _ORDINAL_UNIT_WORDS = {
        1: 'primera', 2: 'segunda', 3: 'tercera', 4: 'cuarta', 5: 'quinta',
        6: 'sexta', 7: 'séptima', 8: 'octava', 9: 'novena',
    }

    @classmethod
    def _spanish_ordinal_to_int(cls, label: str) -> Optional[int]:
        """
        Ordinal value of a spelled-out Spanish ordinal ("CUADRAGÉSIMA PRIMERA" -> 41).

        Returns None when the label is not a recognisable ordinal, so callers can
        fall back to the raw label rather than invent a number.
        """
        if not label:
            return None

        normalized = unicodedata.normalize('NFKD', label).encode('ascii', 'ignore').decode()
        normalized = re.sub(r'[^a-z]', '', normalized.lower())
        if not normalized:
            return None

        for stem, tens in cls._ORDINAL_TENS_STEMS.items():
            if not normalized.startswith(stem):
                continue
            remainder = normalized[len(stem):]
            # "decima" is 10; "decimoprimera"/"decimoctava" carry a unit, with the
            # gender vowel either kept ("vigesimo|cuarta") or absorbed ("decim|octava")
            if remainder in ('', 'o', 'a'):
                return tens
            candidates = [remainder]
            if remainder[:1] in ('o', 'a'):
                candidates.append(remainder[1:])
            for candidate in candidates:
                for unit_stem, unit in cls._ORDINAL_UNIT_STEMS.items():
                    if candidate.startswith(unit_stem):
                        return tens + unit
            return tens

        for unit_stem, unit in cls._ORDINAL_UNIT_STEMS.items():
            if normalized.startswith(unit_stem):
                return unit

        return None

    @classmethod
    def _spanish_ordinal_words(cls, value: int) -> Optional[str]:
        """Canonical feminine spelling of an ordinal ("41" -> "cuadragésima primera")."""
        if value in cls._ORDINAL_UNIT_WORDS:
            return cls._ORDINAL_UNIT_WORDS[value]
        tens, unit = (value // 10) * 10, value % 10
        if tens not in cls._ORDINAL_TENS_WORDS:
            return None
        if unit == 0:
            return cls._ORDINAL_TENS_WORDS[tens]
        return f"{cls._ORDINAL_TENS_WORDS[tens]} {cls._ORDINAL_UNIT_WORDS[unit]}"

    @classmethod
    def _article_descriptor(cls, article_label: str, is_transitory: bool) -> Dict:
        """
        How an article is named in the embedded header and in the citation.

        Permanent articles keep "Artículo 19" / "art. 19". Transitory provisions
        need three fixes for legal lookups:
        - "art. PRIMERA" is not a citable reference; the correct form is
          "disposición transitoria primera".
        - the word "transitoria" only lived in the is_transitory metadata flag,
          which the embedding never sees, so a lawyer searching "cuadragésima
          primera transitoria" had no anchor in the text.
        - the numeric alias lawyers actually type ("art. 41 transitorio") was
          nowhere in the document.
        """
        if not is_transitory:
            return {
                "header": f"Artículo {article_label}",
                "citation": f"art. {article_label}",
                "ordinal": None,
                "normalized_label": article_label,
            }

        ordinal = cls._spanish_ordinal_to_int(article_label)
        words = cls._spanish_ordinal_words(ordinal) if ordinal else None

        if not words:
            logger.debug("transitory_ordinal_unparsed", article_label=article_label)
            name = f"disposición transitoria {article_label}"
            return {
                "header": name.capitalize(),
                "citation": name,
                "ordinal": ordinal,
                "normalized_label": article_label,
            }

        name = f"disposición transitoria {words} (art. {ordinal} transitorio)"
        return {
            "header": name.capitalize(),
            "citation": name,
            "ordinal": ordinal,
            "normalized_label": words.title(),
        }

    @staticmethod
    def _subdivision_sort_value(mark: str) -> Optional[int]:
        """Ordinal value of a subdivision mark ("3.-" -> 3, "c)" -> 3)."""
        number = re.search(r'\d+', mark)
        if number:
            return int(number.group())
        letter = re.search(r'[a-záéíóúñ]', mark.lower())
        if letter:
            return ord(letter.group())
        return None

    def _assign_subdivision_series(self, subdivisions: List[Dict]) -> List[tuple]:
        """
        Numbers restarted subdivision sequences inside a single article.

        Some articles carry two independent lists that both run "1." to "5."
        (Constitución transitory Art. 144 lists the convocation rules and then the
        indigenous-candidacy rules), so the mark alone is not a unique address and
        the citations of both lists collide. Sequences are tracked per
        (type, parent numeral) group, and a group's numbering going backwards or
        repeating marks the start of a new series.

        Returns one (series_number, series_total) pair per subdivision, where the
        total is that subdivision's own group total, so single-series articles get
        (1, 1) and stay unchanged.
        """
        assigned = []
        state = {}  # group key -> (current series, last ordinal seen)

        for subdiv in subdivisions:
            key = (subdiv.get('type'), subdiv.get('parent_numeral'))
            ordinal = self._subdivision_sort_value(subdiv.get('mark', ''))
            current, last = state.get(key, (1, None))

            if ordinal is not None and last is not None and ordinal <= last:
                current += 1

            state[key] = (current, ordinal if ordinal is not None else last)
            assigned.append((key, current))

        totals = {}
        for key, current in assigned:
            totals[key] = max(totals.get(key, 0), current)

        return [(current, totals[key]) for key, current in assigned]

    def _build_subdivision_citation_part(
        self, subdiv: Dict, series_number: int, series_total: int
    ) -> str:
        """Citation fragment for a subdivision, e.g. "numeral 1. (serie 2 de 2)"."""
        subdiv_type_label = "numeral" if subdiv['type'] == 'numeral' else "letra"
        part = f"{subdiv_type_label} {subdiv['mark']}"
        if subdiv.get('parent_numeral'):
            part = f"numeral {subdiv['parent_numeral']}, {part}"
        if series_total > 1:
            part += f" (serie {series_number} de {series_total})"
        return part

    def _build_formatted_citation(
        self,
        norm_citation: str,
        article_label: str,
        context: Dict,
        is_transitory: bool = False
    ) -> str:
        """
        Build formal legal citation.

        Changes:
        - Uses "art." instead of "Artículo" (formal legal citation style)
        - Preserves original case of article_label (no uppercasing)
        - Includes section name if present (e.g., "Del femicidio")

        Example: "Código Penal, art. 390 ter"
        Example with structure: "Código Penal, Libro Segundo, Título Octavo, §1 bis (Del femicidio), art. 390 ter"
        """
        citation_parts = [norm_citation]

        if context.get("book"):
            citation_parts.append(context["book"])
        if context.get("title_ordinal"):
            citation_parts.append(context["title_ordinal"])

        # Include section with name (e.g., "§1 bis (Del femicidio)")
        if context.get("section"):
            section_str = context["section"]
            section_name = context.get("section_name")
            if section_name:
                section_str = f"{section_str} ({section_name})"
            citation_parts.append(section_str)

        # Use "art." instead of "Artículo" (formal citation style)
        # Preserve original case of article_label (no .lower() or .upper())
        citation_parts.append(
            self._article_descriptor(article_label, is_transitory)["citation"]
        )

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
