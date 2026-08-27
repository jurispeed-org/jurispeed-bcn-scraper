"""
Parser for BCN XML format.

Parses the official XML schema from BCN's obtxml service.
Schema: EsquemaIntercambioNorma-v1-0.xsd
"""

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from datetime import date
import structlog
from core.models import ChileanLegalNorm, NormType

logger = structlog.get_logger()


class BCNXMLParser:
    """
    Parser for BCN's official XML format.

    Extracts norm metadata and article structure from XML.
    Much cleaner than HTML parsing.
    """

    # XML namespace
    NS = {'ns': 'http://www.leychile.cl/esquemas'}

    @staticmethod
    def is_future_version(version_date_str: Optional[str]) -> bool:
        """
        Check if version date is in the future (deferred validity).

        Args:
            version_date_str: ISO format date string (YYYY-MM-DD)

        Returns:
            True if date is in the future, False otherwise
        """
        if not version_date_str:
            return False

        try:
            parsed_date = date.fromisoformat(version_date_str)
            return parsed_date > date.today()
        except (ValueError, TypeError):
            logger.warning("invalid_version_date", version_date=version_date_str)
            return False

    def parse(self, xml_content: str, norm_id: int) -> Optional[ChileanLegalNorm]:
        """
        Parse XML to ChileanLegalNorm.

        Args:
            xml_content: Raw XML string
            norm_id: BCN norm ID

        Returns:
            ChileanLegalNorm object or None if parse fails
        """
        try:
            root = ET.fromstring(xml_content)

            # Extract metadata
            metadata = self._extract_metadata(root, norm_id)
            if not metadata:
                logger.error("metadata_extraction_failed", norm_id=norm_id)
                return None

            # Extract full content
            full_content = self._extract_full_content(root)

            # Create norm object
            norm = ChileanLegalNorm(**metadata, full_content=full_content)

            logger.info(
                "xml_parsed_successfully",
                norm_id=norm_id,
                norm_type=norm.norm_type,
                content_length=len(full_content)
            )

            return norm

        except ET.ParseError as e:
            logger.error("xml_parse_error", norm_id=norm_id, error=str(e))
            return None
        except Exception as e:
            logger.error("unexpected_parse_error", norm_id=norm_id, error=str(e))
            return None

    def _extract_metadata(self, root: ET.Element, norm_id: int) -> Optional[Dict]:
        """Extract metadata from XML."""
        try:
            # Get Identificador
            identificador = root.find('ns:Identificador', self.NS)
            if identificador is None:
                return None

            # Dates (both optional - some norms don't have publication date)
            promulgation_date_str = identificador.get('fechaPromulgacion')
            publication_date_str = identificador.get('fechaPublicacion')

            promulgation_date = date.fromisoformat(promulgation_date_str) if promulgation_date_str else None
            publication_date = date.fromisoformat(publication_date_str) if publication_date_str else None

            # If no publication date, use promulgation or version date as fallback
            if not publication_date:
                version_date_str = root.get('fechaVersion')
                publication_date = promulgation_date or (date.fromisoformat(version_date_str) if version_date_str else date.today())

            # Last modified (from root)
            version_date_str = root.get('fechaVersion')
            last_modified = date.fromisoformat(version_date_str) if version_date_str else None

            # Type and number
            type_number_elem = identificador.find('.//ns:TipoNumero', self.NS)
            if type_number_elem is None:
                return None

            type_elem = type_number_elem.find('ns:Tipo', self.NS)
            number_elem = type_number_elem.find('ns:Numero', self.NS)

            if type_elem is None or number_elem is None:
                return None

            type_text = type_elem.text.strip()
            number_text = number_elem.text.strip()

            # Map type to NormType
            norm_type = self._map_tipo_to_norm_type(type_text)

            # Issuing body
            organism_elem = identificador.find('.//ns:Organismo', self.NS)
            issuing_body = organism_elem.text.strip() if organism_elem is not None else "MINISTERIO"

            # Metadata
            metadata_elem = root.find('ns:Metadatos', self.NS)
            title_elem = metadata_elem.find('ns:TituloNorma', self.NS) if metadata_elem is not None else None
            title = title_elem.text.strip() if title_elem is not None and title_elem.text else f"{type_text} {number_text}"

            # Summary (must be at least 50 chars for Pydantic validation)
            base_summary = f"{type_text} {number_text}: {title}"

            # Ensure minimum 50 chars
            if len(base_summary) < 50:
                # Add publication info
                pub_date_str = f"Publicado el {publication_date_str}" if publication_date_str else ""
                base_summary = f"{base_summary}. {pub_date_str}"

                # If still too short, add issuing body
                if len(base_summary) < 50:
                    base_summary = f"{base_summary}. Emitido por {issuing_body}"

            # Truncate if too long
            summary = base_summary[:500]

            # Official URL
            official_url = f"https://bcn.cl/leychile/navegar?idNorma={norm_id}"

            return {
                "norm_id": norm_id,
                "norm_type": norm_type,
                "norm_number": number_text,
                "title": f"{type_text} {number_text}: {title}",
                "publication_date": publication_date,
                "promulgation_date": promulgation_date,
                "last_modified": last_modified,
                "issuing_body": issuing_body,
                "summary": summary,
                "official_url": official_url,
                "subject_tags": [],
                "version": version_date_str,
            }

        except Exception as e:
            logger.error("metadata_extraction_error", error=str(e))
            return None

    def _map_tipo_to_norm_type(self, type_str: str) -> NormType:
        """Map XML type to NormType enum."""
        type_lower = type_str.lower()

        if 'ley' in type_lower:
            return NormType.LEY
        elif 'codigo' in type_lower or 'código' in type_lower:
            return NormType.CODIGO
        elif 'dfl' in type_lower:
            return NormType.DFL
        elif 'decreto' in type_lower:
            return NormType.DECRETO
        elif 'reglamento' in type_lower:
            return NormType.REGLAMENTO
        else:
            return NormType.LEY

    def _extract_full_content(self, root: ET.Element) -> str:
        """
        Extract full text content from XML.

        Concatenates all text from all parts.
        Skips binary attachments (images).
        """
        content_parts = []

        # Header
        header = root.find('ns:Encabezado/ns:Texto', self.NS)
        if header is not None:
            text = self._extract_text_without_binaries(header)
            if text:
                content_parts.append(text)

        # All functional structure texts
        for structure in root.findall('.//ns:EstructuraFuncional', self.NS):
            text_elem = structure.find('ns:Texto', self.NS)
            if text_elem is not None:
                text = self._extract_text_without_binaries(text_elem)
                if text:
                    content_parts.append(text)

        return '\n\n'.join(content_parts)

    def extract_article_hierarchy(self, xml_content: str, norm_id: int) -> Dict[str, Dict]:
        """
        Extract article hierarchy from XML.

        Returns dict mapping idParte -> article metadata.

        This is MUCH simpler than HTML version:
        - idParte is direct attribute
        - derogado is direct attribute (vigencia!)
        - hierarchy is native XML tree
        - fechaVersion per article
        """
        try:
            root = ET.fromstring(xml_content)
            hierarchy = {}

            # Find all articles
            for structure in root.findall('.//ns:EstructuraFuncional[@tipoParte="Artículo"]', self.NS):
                id_parte = structure.get('idParte')
                if not id_parte:
                    continue

                # Get article name
                name_elem = structure.find('.//ns:NombreParte', self.NS)
                article_label = name_elem.text.strip() if name_elem is not None and name_elem.text else None

                if not article_label:
                    continue

                # Extract article number
                article_number = self._extract_article_number(article_label)

                # Check if nested (has "DEL ART" in name)
                is_nested = 'DEL ART' in article_label.upper()
                parent_article = None

                if is_nested:
                    # Extract parent article number
                    import re
                    match = re.search(r'DEL ART[^\d]*(\d+)', article_label, re.IGNORECASE)
                    if match:
                        parent_article = int(match.group(1))

                # Get hierarchy level (count parent elements)
                hierarchy_level = self._get_hierarchy_level(structure, root)

                # Validity status (repealed attribute from XML)
                repealed_attr = structure.get('derogado', 'no derogado')
                in_force = repealed_attr == 'no derogado'

                # Version date
                version_date_str = structure.get('fechaVersion')

                hierarchy[id_parte] = {
                    'article_number': article_number,
                    'article_label': article_label,
                    'is_nested': is_nested,
                    'parent_article': parent_article,
                    'hierarchy_level': hierarchy_level,
                    'in_force': in_force,
                    'version_date': version_date_str,
                }

            logger.info(
                "hierarchy_extracted",
                norm_id=norm_id,
                total_articles=len(hierarchy)
            )

            return hierarchy

        except Exception as e:
            logger.error("hierarchy_extraction_failed", norm_id=norm_id, error=str(e))
            return {}

    def _extract_article_number(self, article_label: str) -> Optional[int]:
        """Extract article number from label."""
        import re

        # Pattern: "1", "2 (DEL ART 1)", etc.
        match = re.match(r'(\d+)', article_label)
        if match:
            return int(match.group(1))

        return None

    def _get_hierarchy_level(self, element: ET.Element, root: ET.Element) -> int:
        """
        Get hierarchy level by counting parent EstructuraFuncional elements.

        Much simpler than HTML version (no need to count <ul> parents).
        """
        level = 0
        current = element

        # Walk up the tree counting EstructuraFuncional parents
        while True:
            parent = self._find_parent(current, root)
            if parent is None:
                break

            # Check if parent is also EstructuraFuncional
            if parent.tag.endswith('EstructuraFuncional'):
                level += 1

            current = parent

        return level

    def _find_parent(self, element: ET.Element, root: ET.Element) -> Optional[ET.Element]:
        """Find parent element in tree."""
        for parent in root.iter():
            if element in parent:
                return parent
        return None

    def _extract_text_without_binaries(self, element: ET.Element) -> str:
        """
        Extract all text from element, skipping binary attachments (images).

        BCN includes inline `<aem:ArchivoBinario>` elements with base64 images.
        These must be skipped to avoid:
        1. Corrupting text extraction
        2. Cutting off text that comes after images

        Uses itertext() to get all text nodes, filtering out binary elements.
        """
        text_parts = []

        # Get element's direct text (before any children)
        if element.text:
            text_parts.append(element.text)

        # Iterate through all children
        for child in element:
            # Skip binary attachments (images)
            # Check tag with or without namespace
            tag = child.tag
            if 'ArchivoBinario' in tag:
                # Skip this element entirely (it's a binary image)
                # But get the tail text (text after this element)
                if child.tail:
                    text_parts.append(child.tail)
                continue

            # For other children, recurse
            child_text = self._extract_text_without_binaries(child)
            if child_text:
                text_parts.append(child_text)

            # Get tail text (text after this child element)
            if child.tail:
                text_parts.append(child.tail)

        # Join and clean up
        full_text = ''.join(text_parts).strip()
        return full_text

    def extract_article_texts(self, xml_content: str) -> Dict[str, str]:
        """
        Extract article texts from XML.

        Returns dict mapping idParte -> article text.

        This is what the chunker will use.

        IMPORTANT: Removes binary attachments (images) that BCN includes inline.
        """
        try:
            root = ET.fromstring(xml_content)
            article_texts = {}

            for structure in root.findall('.//ns:EstructuraFuncional[@tipoParte="Artículo"]', self.NS):
                id_parte = structure.get('idParte')
                if not id_parte:
                    continue

                # Get text element
                text_elem = structure.find('ns:Texto', self.NS)
                if text_elem is not None:
                    # Extract all text, skipping binary attachments
                    text = self._extract_text_without_binaries(text_elem)
                    if text:
                        article_texts[id_parte] = text

            logger.debug(
                "article_texts_extracted",
                total_articles=len(article_texts)
            )

            return article_texts

        except Exception as e:
            logger.error("article_texts_extraction_failed", error=str(e))
            return {}

    def extract_structural_context(self, xml_content: str, norm_id: int) -> Dict[str, Dict]:
        """
        Extract structural context (Book, Title, Section/Paragraph) for each article.

        Returns dict mapping idParte -> structural context with full names.

        Structure:
        {
            "article_id_parte": {
                "book": "LIBRO SEGUNDO",
                "book_name": "CRIMENES Y SIMPLES DELITOS Y SUS PENAS",
                "title_ordinal": "TITULO OCTAVO",
                "title_name": "CRIMENES Y SIMPLES DELITOS CONTRA LAS PERSONAS",
                "section": "§1 bis",
                "section_name": "Del femicidio"
            }
        }

        This is CRITICAL for hybrid search:
        - BM25 needs the structural names in text
        - Reranker needs them visible (not just metadata)
        - "femicidio", "malversacion", "bigamia" exist ONLY in section names
        """
        try:
            root = ET.fromstring(xml_content)
            context_map = {}

            # Find all articles and walk up to get their structural context
            for article in root.findall('.//ns:EstructuraFuncional[@tipoParte="Artículo"]', self.NS):
                id_parte = article.get('idParte')
                if not id_parte:
                    continue

                # Walk up the tree to find structural ancestors
                context = self._find_structural_ancestors(article, root)
                if context:
                    context_map[id_parte] = context

            logger.info(
                "structural_context_extracted",
                norm_id=norm_id,
                total_articles=len(context_map)
            )

            return context_map

        except Exception as e:
            logger.error("structural_context_extraction_failed", norm_id=norm_id, error=str(e))
            return {}

    def _find_structural_ancestors(self, element: ET.Element, root: ET.Element) -> Dict:
        """
        Walk up the XML tree to find Libro, Titulo, Parrafo ancestors.

        Reads <TituloParte> (not <NombreParte>, which is empty for structural elements).
        """
        context = {}
        current = element

        # Walk up the tree
        while True:
            parent = self._find_parent(current, root)
            if parent is None:
                break

            type_val = parent.get('tipoParte')
            if type_val in ['Libro', 'Título', 'Párrafo']:
                # Read <TituloParte> from Metadata (this is where BCN stores the full name)
                title_elem = parent.find('.//ns:TituloParte', self.NS)
                if title_elem is not None and title_elem.text:
                    full_text = title_elem.text.strip()

                    # Parse the full text to separate ordinal from name
                    # Examples:
                    # "LIBRO SEGUNDO CRIMENES Y SIMPLES DELITOS Y SUS PENAS"
                    # "TITULO OCTAVO CRIMENES Y SIMPLES DELITOS CONTRA LAS PERSONAS"
                    # "§1 bis. Del femicidio"

                    if type_val == 'Libro':
                        # Extract "LIBRO SEGUNDO" and "CRIMENES..."
                        parts = full_text.split(maxsplit=2)  # ["LIBRO", "SEGUNDO", "rest"]
                        if len(parts) >= 2:
                            ordinal = f"{parts[0]} {parts[1]}"
                            name = parts[2] if len(parts) > 2 else ""
                            context['book'] = ordinal
                            context['book_name'] = name

                    elif type_val == 'Título':
                        # Extract "TITULO OCTAVO" and "CRIMENES..."
                        parts = full_text.split(maxsplit=2)
                        if len(parts) >= 2:
                            ordinal = f"{parts[0]} {parts[1]}"
                            name = parts[2] if len(parts) > 2 else ""
                            context['title_ordinal'] = ordinal
                            context['title_name'] = name

                    elif type_val == 'Párrafo':
                        # Extract "§1 bis" and "Del femicidio"
                        # Pattern: "§N" or "§N bis/ter" followed by ". Name"
                        import re
                        match = re.match(r'^(§\s*\d+(?:\s+[a-z]+)?)\.\s*(.*)$', full_text, re.IGNORECASE)
                        if match:
                            context['section'] = match.group(1).strip()
                            context['section_name'] = match.group(2).strip()
                        else:
                            # Fallback: treat entire text as section
                            context['section'] = full_text
                            context['section_name'] = ""

            current = parent

        return context
