"""
Parser for BCN XML format.

Parses the official XML schema from BCN's obtxml service.
Schema: EsquemaIntercambioNorma-v1-0.xsd
"""

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
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
            title_raw = title_elem.text.strip() if title_elem is not None and title_elem.text else f"{type_text} {number_text}"

            # Clean duplicated titles (Hallazgo #7)
            # Example: if type_text="Código" and title_raw="CÓDIGO PENAL", avoid "Código 1984: CÓDIGO PENAL"
            title_clean = self._clean_duplicate_title(type_text, number_text, title_raw)

            # Summary (must be at least 50 chars for Pydantic validation)
            base_summary = f"{type_text} {number_text}: {title_clean}"

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
                "title": f"{type_text} {number_text}: {title_clean}",
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
        """
        Map XML type to NormType enum.

        BCN's <Tipo> element contains the specific norm type.
        We map it to our expanded NormType enum.

        This fixes Hallazgo #7: norm_type incorrect.
        """
        type_clean = type_str.strip().lower()

        # Exact matches first (most specific)
        EXACT_MAP = {
            'codigo': NormType.CODIGO,
            'código': NormType.CODIGO,
            'dfl': NormType.DFL,
            'decreto ley': NormType.DL,
            'decreto-ley': NormType.DL,
            'decreto con fuerza de ley': NormType.DFL,
            'decreto supremo': NormType.DECRETO_SUPREMO,
            'orden': NormType.ORDEN,
            'ordenanza': NormType.ORDENANZA,
            'ordenanza municipal': NormType.ORDENANZA_MUNICIPAL,
            'oficio': NormType.OFICIO,
            'resolucion': NormType.RESOLUCION,
            'resolución': NormType.RESOLUCION,
            'circular': NormType.CIRCULAR,
            'instruccion': NormType.INSTRUCCION,
            'instrucción': NormType.INSTRUCCION,
            'reglamento': NormType.REGLAMENTO,
            'acuerdo': NormType.ACUERDO,
            'convenio': NormType.CONVENIO,
            'tratado': NormType.TRATADO,
            'auto acordado': NormType.AUTO_ACORDADO,
        }

        # Check exact matches
        if type_clean in EXACT_MAP:
            return EXACT_MAP[type_clean]

        # Partial matches (order matters - most specific first)
        if 'decreto ley' in type_clean or 'decreto-ley' in type_clean:
            return NormType.DL
        elif 'dfl' in type_clean or 'fuerza de ley' in type_clean:
            return NormType.DFL
        elif 'decreto supremo' in type_clean:
            return NormType.DECRETO_SUPREMO
        elif 'decreto' in type_clean:
            return NormType.DECRETO
        elif 'codigo' in type_clean or 'código' in type_clean:
            return NormType.CODIGO
        elif 'ordenanza municipal' in type_clean:
            return NormType.ORDENANZA_MUNICIPAL
        elif 'ordenanza' in type_clean:
            return NormType.ORDENANZA
        elif 'orden' in type_clean:
            return NormType.ORDEN
        elif 'oficio' in type_clean:
            return NormType.OFICIO
        elif 'resolucion' in type_clean or 'resolución' in type_clean:
            return NormType.RESOLUCION
        elif 'circular' in type_clean:
            return NormType.CIRCULAR
        elif 'instruccion' in type_clean or 'instrucción' in type_clean:
            return NormType.INSTRUCCION
        elif 'reglamento' in type_clean:
            return NormType.REGLAMENTO
        elif 'acuerdo' in type_clean:
            return NormType.ACUERDO
        elif 'convenio' in type_clean:
            return NormType.CONVENIO
        elif 'tratado' in type_clean:
            return NormType.TRATADO
        elif 'auto acordado' in type_clean:
            return NormType.AUTO_ACORDADO
        elif 'ley' in type_clean:
            return NormType.LEY
        else:
            # Default fallback
            logger.warning("unknown_norm_type", type_str=type_str)
            return NormType.LEY

    def _clean_duplicate_title(self, type_text: str, number_text: str, title_raw: str) -> str:
        """
        Clean duplicate titles (Hallazgo #7).

        Examples of problems:
        - "Código PENAL: CÓDIGO PENAL" → should be "Código Penal"
        - "Orden 2870: PROTOCOLOS..." → OK (not duplicated)

        Strategy:
        1. If title starts with type_text (ignoring case), remove the duplicate
        2. Normalize case to title case for readability

        Args:
            type_text: Type from XML (e.g., "Código", "Orden")
            number_text: Number from XML (e.g., "1984", "2870")
            title_raw: Raw title from XML

        Returns:
            Cleaned title
        """
        title_lower = title_raw.lower()
        type_lower = type_text.lower()

        # Check if title starts with the type (duplicate)
        if title_lower.startswith(type_lower):
            # Remove the duplicate type from title
            # Example: "CÓDIGO PENAL" → "PENAL"
            remaining = title_raw[len(type_text):].strip()

            # Remove leading punctuation (: - etc.)
            remaining = remaining.lstrip(':').lstrip('-').strip()

            # If remaining is just the number, keep the original title
            if remaining == number_text or not remaining:
                return title_raw

            # Use the remaining part
            title_clean = remaining
        else:
            title_clean = title_raw

        # Normalize case: convert all-caps to title case for readability
        if title_clean.isupper() and len(title_clean) > 3:
            # "CÓDIGO PENAL" → "Código Penal"
            title_clean = title_clean.title()

        return title_clean

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

        IMPORTANT: Detects nested structures (Hallazgo #6):
        - Articles inside N°/romano/TÍTULO are marked as nested
        - parent_article is set to the parent container number
        """
        try:
            root = ET.fromstring(xml_content)
            hierarchy = {}

            # Build parent map for upward navigation
            parent_map = {c: p for p in root.iter() for c in p}

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

                # Check if nested in two ways:
                # 1. Legacy: has "DEL ART" in name
                is_nested_legacy = 'DEL ART' in article_label.upper()
                parent_article = None

                if is_nested_legacy:
                    # Extract parent article number from label
                    import re
                    match = re.search(r'DEL ART[^\d]*(\d+)', article_label, re.IGNORECASE)
                    if match:
                        parent_article = int(match.group(1))

                # 2. NEW: Check if inside a parent structure (N°, romano, TÍTULO, etc.)
                is_nested_structural, parent_structural = self._detect_nested_structure(
                    structure, parent_map
                )

                # Combine both detections
                is_nested = is_nested_legacy or is_nested_structural
                if parent_structural is not None:
                    parent_article = parent_structural

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

    def _detect_nested_structure(
        self,
        article_element: ET.Element,
        parent_map: Dict
    ) -> Tuple[bool, Optional[int]]:
        """
        Detect if an article is nested inside a parent structure (N°, romano, TÍTULO, etc.).

        This fixes Hallazgo #6: Doble articulado no detectado.

        Example XML structure:
        <EstructuraFuncional tipoParte="N°" idParte="10534843">
            <Articulo>2.</Articulo>
            <EstructuraFuncional tipoParte="Artículo" idParte="10534844">
                <Articulo>1º</Articulo>  <!-- This is nested! -->
            </EstructuraFuncional>
        </EstructuraFuncional>

        Args:
            article_element: The article element to check
            parent_map: Dict mapping child -> parent for upward navigation

        Returns:
            (is_nested, parent_number): Tuple with:
                - is_nested: True if article is inside a parent structure
                - parent_number: Number of parent container (e.g., 2 for "N° 2")
        """
        # Parent container types that can contain nested articles
        CONTAINER_TYPES = [
            'N°', 'romano', 'TÍTULO', 'Título', 'Capítulo', 'Sección',
            'Doble Articulado',  # Ordenanzas with nested article structure
            'Párrafo'  # Some norms use Párrafo as container
        ]

        # Traverse up to find parent EstructuraFuncional
        current = article_element
        while current is not None:
            parent = parent_map.get(current)
            if parent is None:
                break

            # Check if parent is EstructuraFuncional with a container type
            if parent.tag.endswith('EstructuraFuncional'):
                parent_tipo = parent.get('tipoParte')

                if parent_tipo in CONTAINER_TYPES:
                    # Found a container! Extract its number
                    parent_number = self._extract_container_number(parent)

                    logger.debug(
                        "nested_structure_detected",
                        article_id=article_element.get('idParte'),
                        parent_tipo=parent_tipo,
                        parent_number=parent_number
                    )

                    return (True, parent_number)

            current = parent

        # Not nested
        return (False, None)

    def _extract_container_number(self, container_element: ET.Element) -> Optional[int]:
        """
        Extract number from a container element (N°, romano, etc.).

        Examples:
        - <Articulo>2.</Articulo> → 2
        - <Articulo>N° 2</Articulo> → 2
        - <Articulo>II</Articulo> → 2 (romano)
        """
        import re

        # Get the <Articulo> or <NombreParte> element
        name_elem = container_element.find('.//ns:Articulo', self.NS)
        if name_elem is None:
            name_elem = container_element.find('.//ns:NombreParte', self.NS)

        if name_elem is not None and name_elem.text:
            text = name_elem.text.strip()

            # Try to extract number
            # Pattern: "2", "2.", "N° 2", "Nº 2"
            match = re.search(r'(\d+)', text)
            if match:
                return int(match.group(1))

            # TODO: Handle roman numerals (II, III, IV, etc.)
            # For now, return None for roman numerals

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

    def extract_binary_content(self, xml_content: str, norm_id: int) -> Dict[str, dict]:
        """
        Detect articles with binary content (images, tables).

        BCN includes <aem:ArchivoBinario> elements with base64-encoded images/tables
        in articles that have visual content. This method detects which articles
        have binary content so we can:
        1. Add metadata flag
        2. Add textual note to warn users
        3. Mark content as incomplete

        Args:
            xml_content: Raw XML string from BCN
            norm_id: Norm ID for logging

        Returns:
            Dict mapping part_id -> binary info:
            {
                "10534846": {
                    "present": True,
                    "type": "image",
                    "filename": "tabla_tarifas.png",
                    "description": "Contenido binario no indexado"
                }
            }
        """
        try:
            root = ET.fromstring(xml_content)
            binary_map = {}

            # Build a child->parent map first (ElementTree doesn't support upward navigation)
            parent_map = {c: p for p in root.iter() for c in p}

            # Find all ArchivoBinario elements (with or without namespace)
            # BCN uses: <aem:ArchivoBinario nombre="filename.png">base64data</aem:ArchivoBinario>
            for archivo in root.iter():
                if 'ArchivoBinario' in archivo.tag:
                    # Found a binary element, now find its parent article
                    filename = archivo.get('nombre', 'unknown')

                    # Traverse up using parent_map to find element with idParte
                    current = archivo
                    while current is not None:
                        parent = parent_map.get(current)
                        if parent is None:
                            break

                        part_id = parent.get('idParte')
                        if part_id:
                            # Found the article containing this binary
                            # Detect type from filename
                            binary_type = 'image'
                            if filename:
                                ext = filename.lower().split('.')[-1]
                                if ext in ['png', 'jpg', 'jpeg', 'gif', 'bmp']:
                                    binary_type = 'image'
                                elif ext in ['pdf']:
                                    binary_type = 'document'
                                else:
                                    binary_type = 'unknown'

                            binary_map[part_id] = {
                                'present': True,
                                'type': binary_type,
                                'filename': filename,
                                'description': 'Contenido binario no indexado (imagen o tabla)'
                            }

                            logger.debug(
                                "binary_content_detected",
                                norm_id=norm_id,
                                part_id=part_id,
                                filename=filename,
                                type=binary_type
                            )
                            break

                        current = parent

            if binary_map:
                logger.info(
                    "binary_content_extraction_complete",
                    norm_id=norm_id,
                    articles_with_binary=len(binary_map)
                )

            return binary_map

        except Exception as e:
            logger.warning(
                "binary_content_extraction_failed",
                norm_id=norm_id,
                error=str(e)
            )
            return {}
