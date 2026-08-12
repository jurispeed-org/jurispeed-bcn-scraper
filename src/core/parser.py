"""
HTML parser for BCN (Biblioteca del Congreso Nacional) legal documents.

Extracts structured metadata from Chilean legal norms HTML.
"""

import re
import structlog
from bs4 import BeautifulSoup
from datetime import date, datetime
from typing import Optional, List
from .models import ChileanLegalNorm, NormType

logger = structlog.get_logger()


class BCNHtmlParser:
    """
    Parser for BCN HTML documents.

    Extracts 10 metadata fields + 2 critical content fields (summary + full text).
    """

    def __init__(self):
        self.stats = {"parsed": 0, "failed": 0, "warnings": 0}

    def parse(self, html: str, norm_id: int) -> Optional[ChileanLegalNorm]:
        """
        Parse BCN HTML into validated legal norm model.

        Args:
            html: Raw HTML from BCN website
            norm_id: BCN database identifier

        Returns:
            Validated ChileanLegalNorm or None if parsing fails
        """
        try:
            soup = BeautifulSoup(html, "lxml")

            # Extract all fields
            data = {
                "norm_id": norm_id,
                "norm_type": self._extract_norm_type(soup, norm_id),
                "norm_number": self._extract_number(soup, norm_id),
                "title": self._extract_title(soup, norm_id),
                "publication_date": self._extract_publication_date(soup, norm_id),
                "promulgation_date": self._extract_promulgation_date(soup, norm_id),
                "last_modified": self._extract_last_modified(soup, norm_id),
                "issuing_body": self._extract_issuing_body(soup, norm_id),
                "version": self._extract_version(soup, norm_id),
                "subject_tags": self._extract_subject_tags(soup, norm_id),
                "official_url": f"https://bcn.cl/leychile/navegar?idNorma={norm_id}",
                "summary": self._extract_summary(soup, norm_id),
                "full_content": self._extract_full_content(soup, norm_id),
            }

            # Pydantic validates automatically
            norm = ChileanLegalNorm(**data)
            self.stats["parsed"] += 1

            logger.info(
                "norm_parsed",
                norm_id=norm_id,
                norm_type=norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type,
                title_length=len(norm.title),
                content_length=len(norm.full_content),
                summary_length=len(norm.summary),
            )

            return norm

        except Exception as e:
            self.stats["failed"] += 1
            logger.error(
                "parse_failed",
                norm_id=norm_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return None

    def _extract_norm_type(self, soup: BeautifulSoup, norm_id: int) -> str:
        """
        Extract tipo_norma (norm type) from HTML.

        Strategy:
        1. Look for explicit type indicator in title/header
        2. Pattern match common prefixes (LEY NUM, CODIGO, DFL, etc.)
        3. Fallback to "ley"
        """
        try:
            # Try to find in title
            title = self._get_title_text(soup)
            title_upper = title.upper()

            # Pattern matching
            if re.search(r"\bCODIGO\b", title_upper):
                return NormType.CODIGO.value
            elif re.search(r"\bD\.?F\.?L\.?\b", title_upper):
                return NormType.DFL.value
            elif re.search(r"\bDECRETO\b", title_upper) and "LEY" not in title_upper:
                return NormType.DECRETO.value
            elif re.search(r"\bREGLAMENTO\b", title_upper):
                return NormType.REGLAMENTO.value
            elif re.search(r"\bLEY\b", title_upper):
                return NormType.LEY.value

            # Default fallback
            logger.warning("norm_type_fallback", norm_id=norm_id, title=title[:50])
            return NormType.LEY.value

        except Exception as e:
            logger.warning("norm_type_extraction_failed", norm_id=norm_id, error=str(e))
            return NormType.LEY.value

    def _extract_number(self, soup: BeautifulSoup, norm_id: int) -> str:
        """
        Extract norm number from title.

        Examples:
        - "LEY NUM. 19.846" → "19846"
        - "CODIGO CIVIL" → "DFL-1"
        - "DFL 830" → "830"
        """
        try:
            title = self._get_title_text(soup)

            # Pattern: "NUM. 12.345" or "Nº 12.345"
            match = re.search(r"N[UÚ]M\.?\s*(\d[\d.,\s-]*)", title, re.IGNORECASE)
            if match:
                number = match.group(1).replace(".", "").replace(",", "").replace(" ", "")
                return number

            # Pattern: "DFL 123" or "DECRETO 456"
            match = re.search(r"(?:DFL|DECRETO)\s+(\d+)", title, re.IGNORECASE)
            if match:
                return match.group(1)

            # Fallback: use norm_id
            logger.warning("norm_number_fallback", norm_id=norm_id, title=title[:50])
            return str(norm_id)

        except Exception as e:
            logger.warning("norm_number_extraction_failed", norm_id=norm_id, error=str(e))
            return str(norm_id)

    def _extract_title(self, soup: BeautifulSoup, norm_id: int) -> str:
        """
        Extract title with intelligent composition.

        Strategy:
        For decrees/laws, combine: tipo + número + descripción (from meta description)
        Example: "Decreto 37: AUTORIZA CIRCULACION DE VEHICULO..."
        """
        try:
            # Get raw title from meta tags
            raw_title = self._get_title_text(soup)

            # Get description from meta tag (don't call _extract_summary to avoid recursion)
            meta_desc = soup.find("meta", {"name": "description"})
            description = meta_desc.get("content", "").strip() if meta_desc else ""

            # If description is too generic, use raw_title
            if not description or len(description) < 20 or "Ley Chile" in description:
                return raw_title

            # If raw_title already contains the description, use it as-is
            if description[:50] in raw_title:
                return raw_title

            # Otherwise, compose: tipo número + description
            # Extract tipo and número from raw title
            match_decreto = re.search(r'Decreto\s+(\d+)', raw_title, re.IGNORECASE)
            match_ley = re.search(r'Ley\s+(?:N[°Úu]?m?\.?\s*)?(\d+[\d.,]*)', raw_title, re.IGNORECASE)
            match_dfl = re.search(r'DFL\s+(\d+)', raw_title, re.IGNORECASE)

            # Compose intelligent title
            if match_decreto:
                norm_number = match_decreto.group(1)
                composed_title = f"Decreto {norm_number}: {description}"
            elif match_ley:
                norm_number = match_ley.group(1).replace(".", "").replace(",", "")
                composed_title = f"Ley {norm_number}: {description}"
            elif match_dfl:
                norm_number = match_dfl.group(1)
                composed_title = f"DFL {norm_number}: {description}"
            else:
                # Use description as title if available
                composed_title = description if len(description) > 20 else raw_title

            # Truncate to reasonable length (max 200 chars)
            if len(composed_title) > 200:
                composed_title = composed_title[:197] + "..."

            return composed_title

        except Exception as e:
            logger.error("title_extraction_failed", norm_id=norm_id, error=str(e))
            raise ValueError(f"Failed to extract title for norm {norm_id}")

    def _get_title_text(self, soup: BeautifulSoup) -> str:
        """Helper to get title text with multiple strategies."""
        # Strategy 1: Meta og:title (best for BCN Playwright-rendered pages)
        meta = soup.find("meta", {"name": "og:title"})
        if meta and meta.get("content"):
            title = meta["content"].strip()
            # Clean "Ley Chile - " prefix
            title = re.sub(r"^Ley Chile\s*-\s*", "", title, flags=re.IGNORECASE)
            title = re.sub(r"\s*-\s*Biblioteca del Congreso Nacional$", "", title)
            if title:
                return title

        # Strategy 2: <title> tag
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            title = title_tag.get_text(strip=True)
            # Clean prefixes/suffixes
            title = re.sub(r"^Ley Chile\s*-\s*", "", title, flags=re.IGNORECASE)
            title = re.sub(r"\s*-\s*Biblioteca del Congreso Nacional$", "", title)
            if len(title) > 10:
                return title

        # Strategy 3: <h1> tag
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)

        raise ValueError("No title found")

    def _parse_chilean_date(self, date_str: str) -> Optional[date]:
        """
        Parse Chilean date formats.

        Formats:
        - "2024-05-15" (ISO)
        - "15-MAY-2024" (BCN short format)
        - "15 de Mayo de 2024" (Spanish long format)
        """
        if not date_str:
            return None

        try:
            # Clean whitespace
            date_str = date_str.strip()

            # Format 1: ISO format "2024-05-15"
            if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                return datetime.strptime(date_str, "%Y-%m-%d").date()

            # Format 2: BCN short format "15-MAY-2024" or "03-AGO-2026"
            month_abbr_map = {
                'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4,
                'may': 5, 'jun': 6, 'jul': 7, 'ago': 8,
                'sep': 9, 'oct': 10, 'nov': 11, 'dic': 12
            }

            match = re.match(r'(\d{1,2})-([A-Z]{3})-(\d{4})', date_str, re.IGNORECASE)
            if match:
                day = int(match.group(1))
                month_abbr = match.group(2).lower()
                year = int(match.group(3))

                if month_abbr in month_abbr_map:
                    month = month_abbr_map[month_abbr]
                    return date(year, month, day)

            # Format 3: Spanish long format "15 de Mayo de 2024"
            month_full_map = {
                'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
                'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
                'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12
            }

            match = re.match(r'(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})', date_str, re.IGNORECASE)
            if match:
                day = int(match.group(1))
                month_name = match.group(2).lower()
                year = int(match.group(3))

                if month_name in month_full_map:
                    month = month_full_map[month_name]
                    return date(year, month, day)

            return None

        except Exception as e:
            logger.debug("date_parse_failed", date_str=date_str, error=str(e))
            return None

    def _extract_publication_date(self, soup: BeautifulSoup, norm_id: int) -> date:
        """
        Extract fecha de publicación (required field).

        Strategy:
        1. Look in .datos div for "Publicación:"
        2. Fallback to meta tag article:published_time
        """
        try:
            # Strategy 1: From .datos div (BCN displays dates here)
            datos_elem = soup.find(class_="datos")
            if datos_elem:
                text = datos_elem.get_text()
                # Pattern: "Publicación:03-AGO-2026"
                match = re.search(r'Publicaci[oó]n:\s*(\d{1,2}-[A-Z]{3}-\d{4})', text, re.IGNORECASE)
                if match:
                    date_str = match.group(1)
                    date_parsed = self._parse_chilean_date(date_str)
                    if date_parsed:
                        return date_parsed

            # Strategy 2: Meta tag article:published_time
            meta = soup.find("meta", {"name": "article:published_time"})
            if meta and meta.get("content"):
                date_parsed = self._parse_chilean_date(meta["content"])
                if date_parsed:
                    return date_parsed

            # Strategy 3: Meta tag article:modified_time
            meta = soup.find("meta", {"name": "article:modified_time"})
            if meta and meta.get("content"):
                date_parsed = self._parse_chilean_date(meta["content"])
                if date_parsed:
                    return date_parsed

            # Fallback: return a default date
            logger.warning("publication_date_fallback", norm_id=norm_id)
            return date(2000, 1, 1)

        except Exception as e:
            logger.error("publication_date_failed", norm_id=norm_id, error=str(e))
            return date(2000, 1, 1)

    def _extract_promulgation_date(
        self, soup: BeautifulSoup, norm_id: int
    ) -> Optional[date]:
        """
        Extract fecha de promulgación (optional).

        Strategy:
        1. Look in .datos div for "Promulgación:"
        2. Look in content text for "Santiago, DD de MONTH de YYYY"
        3. Look for "DD-MONTH-YYYY" format
        """
        try:
            # Strategy 1: From .datos div (BCN displays dates here)
            # Example: "Promulgación:27-JUL-2026"
            datos_elem = soup.find(class_="datos")
            if datos_elem:
                text = datos_elem.get_text()
                # Pattern: "Promulgación:27-JUL-2026"
                match = re.search(r'Promulgaci[oó]n:\s*(\d{1,2}-[A-Z]{3}-\d{4})', text, re.IGNORECASE)
                if match:
                    date_str = match.group(1)
                    date_parsed = self._parse_chilean_date(date_str)
                    if date_parsed:
                        return date_parsed

            # Strategy 2: Get content text
            content_elem = soup.find(class_="texto_norma")
            if not content_elem:
                return None

            text = content_elem.get_text()[:1000]  # First 1000 chars

            # Pattern: "Santiago, DD de MONTH de YYYY"
            # Example: "Santiago, 30 de enero de 1996"
            month_map = {
                'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04',
                'mayo': '05', 'junio': '06', 'julio': '07', 'agosto': '08',
                'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12'
            }

            # Pattern 1: "Santiago, DD de MONTH de YYYY"
            pattern = r'Santiago,?\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})'
            match = re.search(pattern, text, re.IGNORECASE)

            if match:
                day = int(match.group(1))
                month_name = match.group(2).lower()
                year = int(match.group(3))

                if month_name in month_map:
                    month = int(month_map[month_name])
                    return date(year, month, day)

            # Pattern 2: Look in first line for "DD-MONTH-YYYY"
            # Example: "30-ENE-1996"
            month_abbr_map = {
                'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04',
                'may': '05', 'jun': '06', 'jul': '07', 'ago': '08',
                'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12'
            }

            pattern2 = r'(\d{1,2})-([A-Z]{3})-(\d{4})'
            match = re.search(pattern2, text, re.IGNORECASE)

            if match:
                day = int(match.group(1))
                month_abbr = match.group(2).lower()
                year = int(match.group(3))

                if month_abbr in month_abbr_map:
                    month = int(month_abbr_map[month_abbr])
                    return date(year, month, day)

            return None

        except Exception as e:
            logger.debug("promulgation_date_extraction_failed", norm_id=norm_id, error=str(e))
            return None

    def _extract_last_modified(self, soup: BeautifulSoup, norm_id: int) -> Optional[date]:
        """Extract última modificación (optional)."""
        try:
            # TODO: Implement after HTML analysis
            return None
        except Exception:
            return None

    def _extract_issuing_body(self, soup: BeautifulSoup, norm_id: int) -> str:
        """Extract organismo emisor (required)."""
        try:
            # Strategy 1: Meta tag article:author
            meta = soup.find("meta", {"name": "article:author"})
            if meta and meta.get("content"):
                return meta["content"].strip()

            # Fallback
            logger.warning("issuing_body_fallback", norm_id=norm_id)
            return "CONGRESO NACIONAL"

        except Exception as e:
            logger.error("issuing_body_failed", norm_id=norm_id, error=str(e))
            return "CONGRESO NACIONAL"

    def _extract_version(self, soup: BeautifulSoup, norm_id: int) -> Optional[str]:
        """
        Extract version identifier (optional).

        BCN shows "Versión:Única" or "Versión:Con modificaciones"
        If "Única", return None (no version tracking needed)
        """
        try:
            # Strategy 1: From .datos div
            # Example: "Versión:Única -03-AGO-2026"
            datos_elem = soup.find(class_="datos")
            if datos_elem:
                text = datos_elem.get_text()
                # Pattern: "Versión:Única" or "Versión:Con modificaciones"
                match = re.search(r'Versi[oó]n:\s*([^\n\r-]+)', text, re.IGNORECASE)
                if match:
                    version = match.group(1).strip()
                    # If "Única", return None (no version)
                    if 'nica' in version.lower():  # Matches "Única" or "unica"
                        return None
                    # Otherwise return the version string
                    return version

            return None
        except Exception as e:
            logger.debug("version_extraction_failed", norm_id=norm_id, error=str(e))
            return None

    def _extract_subject_tags(self, soup: BeautifulSoup, norm_id: int) -> List[str]:
        """
        Extract materias (subject tags).

        BCN concatenates keywords, with SOME spaces between groups.
        Example: "CIRCULACION VEHICULARGOBERNACION PROVINCIALARICA"

        Strategy:
        1. Split by spaces first (BCN leaves some spaces)
        2. For each part, try to split concatenated words using common patterns
        3. Return cleaned list
        """
        try:
            meta = soup.find("meta", {"name": "keywords"})
            if not meta or not meta.get("content"):
                return []

            keywords_raw = meta["content"].strip()

            # Try comma/semicolon separators first (some pages use them)
            if ',' in keywords_raw or ';' in keywords_raw:
                keywords = re.split(r"[,;]\s*", keywords_raw)
                tags = [k.strip().title() for k in keywords if k.strip() and len(k.strip()) > 3]
                return tags[:15]

            # BCN style: Split by spaces first
            space_parts = keywords_raw.split()

            all_tags = []

            for part in space_parts:
                # For each space-separated part, try to detect word boundaries
                # Common legal/administrative suffixes in Spanish that indicate word end:
                # -CION, -SION, -DAD, -TAD, -MENTE, -DORA, -DORES, -NCIA, -NTE

                # Split before these patterns when followed by uppercase
                # Example: "VEHICULARGOBERNACION" -> "VEHICULAR" + "GOBERNACION"
                #          (split before "GOBERNACION")

                # Insert '|' before common word starts
                temp = re.sub(r'(CION|SION|DAD|TAD|NTE|CIA|DOR|DORA)([A-ZÑÁÉÍÓÚ])', r'\1|\2', part)

                # Also split before common prefixes: SUB, PRE, ANTE, SOBRE, etc.
                # (Not doing this to avoid over-splitting)

                # Split by separator
                sub_parts = temp.split('|')

                # Add each sub-part
                for sub in sub_parts:
                    clean = sub.strip().title()
                    if len(clean) >= 4:  # Min 4 chars
                        all_tags.append(clean)

            # Remove duplicates while preserving order
            seen = set()
            final_tags = []
            for tag in all_tags:
                if tag.lower() not in seen:
                    seen.add(tag.lower())
                    final_tags.append(tag)

            # Limit to 15 tags
            return final_tags[:15]

        except Exception as e:
            logger.warning("subject_tags_failed", norm_id=norm_id, error=str(e))
            return []

    def _extract_summary(self, soup: BeautifulSoup, norm_id: int) -> str:
        """
        Extract or generate intelligent summary (⭐ critical for RAG).

        Strategy:
        1. Meta description tag (BCN provides this)
        2. Look for official summary/abstract if exists
        3. Otherwise, take first 3-5 paragraphs
        4. Clean, normalize, and truncate to 2000 chars
        """
        try:
            # Strategy 1: Meta description (BCN provides good summaries here)
            meta = soup.find("meta", {"name": "description"})
            if meta and meta.get("content"):
                summary = meta["content"].strip()
                # Filter out generic descriptions
                if len(summary) >= 50 and "Ley Chile" not in summary:
                    return self._clean_text(summary)[:2000]

            # Strategy 2: Meta og:description
            meta = soup.find("meta", {"name": "og:description"})
            if meta and meta.get("content"):
                summary = meta["content"].strip()
                if len(summary) >= 50 and "Ley Chile" not in summary:
                    return self._clean_text(summary)[:2000]

            # Strategy 3: Official summary element
            summary_elem = soup.find(class_=re.compile(r"resumen|abstract|summary", re.I))
            if summary_elem:
                summary = summary_elem.get_text(strip=True)
                if len(summary) >= 50:
                    return self._clean_text(summary)[:2000]

            # Strategy 4: First paragraphs from content
            content = self._extract_full_content(soup, norm_id)
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

            # Take first 3-5 paragraphs
            summary_parts = []
            char_count = 0
            for para in paragraphs[:5]:
                if char_count + len(para) > 1800:  # Leave room for ellipsis
                    break
                summary_parts.append(para)
                char_count += len(para)

            if summary_parts:
                summary = " ".join(summary_parts)
                if len(summary) < 50:
                    # Too short, add more context
                    summary = content[:2000]
                return self._clean_text(summary)

            # Fallback: use title as summary
            title = self._get_title_text(soup)
            logger.warning("summary_from_title", norm_id=norm_id)
            return f"Normativa sobre {title.lower()}."

        except Exception as e:
            logger.error("summary_extraction_failed", norm_id=norm_id, error=str(e))
            # Fallback to title
            title = self._get_title_text(soup)
            return f"Normativa sobre {title.lower()}."

    def _extract_full_content(self, soup: BeautifulSoup, norm_id: int) -> str:
        """
        Extract complete legal text (required).

        Strategy:
        1. Find BCN-specific content container (.texto_norma or .cuerpo-norma)
        2. Extract text, preserve structure (paragraphs)
        3. Clean HTML artifacts
        4. Validate minimum length
        """
        try:
            # Strategy 1: BCN-specific selectors (Playwright-rendered pages)
            # Try .texto_norma first (the actual legal text)
            content_elem = soup.find(class_="texto_norma")

            if content_elem:
                text = content_elem.get_text(separator="\n\n", strip=True)
                if len(text) >= 100:
                    return self._clean_text(text)

            # Strategy 2: Try .cuerpo-norma (body container)
            content_elem = soup.find(class_="cuerpo-norma")

            if content_elem:
                text = content_elem.get_text(separator="\n\n", strip=True)
                if len(text) >= 100:
                    return self._clean_text(text)

            # Strategy 3: Generic content container (fallback)
            content_elem = soup.find(
                class_=re.compile(r"contenido-norma|content|texto|articulo", re.I)
            )

            if content_elem:
                text = content_elem.get_text(separator="\n\n", strip=True)
                if len(text) >= 100:
                    return self._clean_text(text)

            # Strategy 4: Get all <p> tags (last resort)
            paragraphs = soup.find_all("p")
            if paragraphs:
                text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
                if len(text) >= 100:
                    return self._clean_text(text)

            raise ValueError("Content too short or empty")

        except Exception as e:
            logger.error("content_extraction_failed", norm_id=norm_id, error=str(e))
            raise ValueError(f"Failed to extract content for norm {norm_id}")

    def _extract_text_with_structure(self, element) -> str:
        """Extract text preserving paragraph structure."""
        # Get text with double newlines between block elements
        text_parts = []
        for child in element.descendants:
            if child.name in ["p", "div", "article", "section"]:
                text = child.get_text(strip=True)
                if text:
                    text_parts.append(text)

        return "\n\n".join(text_parts)

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text."""
        # Remove excessive whitespace
        text = re.sub(r"\s+", " ", text)
        # Remove HTML entities
        text = re.sub(r"&[a-z]+;", "", text, flags=re.IGNORECASE)
        # Normalize quotes
        text = text.replace(""", '"').replace(""", '"').replace("'", "'")
        # Strip
        return text.strip()
