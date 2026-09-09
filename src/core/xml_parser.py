"""
Parser for BCN XML format.

Parses the official XML schema from BCN's obtxml service.
Schema: EsquemaIntercambioNorma-v1-0.xsd
"""

import xml.etree.ElementTree as ET
import re
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

    # tipoParte values treated as articles (Disposición Transitoria are
    # transitory provisions, structurally equivalent to regular articles)
    ARTICLE_TIPO_PARTE = {'Artículo', 'Disposición Transitoria'}

    # BCN's plain-text export lays out the article body and its margin notes
    # (e.g. "CPR Art. 19° N° 24", "D.O. 24.10.1980") side by side in fixed-width
    # columns on the same line. A run of 4+ spaces after real content marks the
    # start of the margin-note column; strip it so it doesn't get embedded mid-word
    # (lookbehind requires a non-space char right before the gap, so it never
    # touches a line's own leading indentation).
    _MARGIN_NOTE_PATTERN = re.compile(r'(?<=\S) {4,}\S.*$', re.MULTILINE)

    def _strip_margin_notes(self, text: str) -> str:
        return self._MARGIN_NOTE_PATTERN.sub('', text)

    def _find_article_structures(self, root: ET.Element) -> List[ET.Element]:
        """Find all EstructuraFuncional elements that represent articles (regular or transitory)."""
        return [
            structure for structure in root.findall('.//ns:EstructuraFuncional', self.NS)
            if structure.get('tipoParte') in self.ARTICLE_TIPO_PARTE
        ]

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

            metadata = self._extract_metadata(root, norm_id)
            if not metadata:
                logger.error("metadata_extraction_failed", norm_id=norm_id)
                return None

            full_content = self._extract_full_content(root)

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

            version_date_str = root.get('fechaVersion')
            last_modified = date.fromisoformat(version_date_str) if version_date_str else None

            type_number_elem = identificador.find('.//ns:TipoNumero', self.NS)
            if type_number_elem is None:
                return None

            type_elem = type_number_elem.find('ns:Tipo', self.NS)
            number_elem = type_number_elem.find('ns:Numero', self.NS)

            if type_elem is None or number_elem is None:
                return None

            type_text = type_elem.text.strip()
            number_text = number_elem.text.strip()

            norm_type = self._map_tipo_to_norm_type(type_text)

            organism_elem = identificador.find('.//ns:Organismo', self.NS)
            issuing_body = organism_elem.text.strip() if organism_elem is not None else "MINISTERIO"

            metadata_elem = root.find('ns:Metadatos', self.NS)
            title_elem = metadata_elem.find('ns:TituloNorma', self.NS) if metadata_elem is not None else None
            title_raw = title_elem.text.strip() if title_elem is not None and title_elem.text else f"{type_text} {number_text}"

            # Clean duplicated titles - avoid "Código 1984: CÓDIGO PENAL"
            title_clean = self._clean_duplicate_title(type_text, number_text, title_raw)

            # Try to extract known código name first for better title formatting
            codigo_name = self._extract_codigo_name_static(type_text, number_text, title_raw)
            if codigo_name:
                # Use clean código name: "Código Penal" instead of "Código PENAL: CÓDIGO PENAL"
                final_title = codigo_name
            else:
                final_title = f"{type_text} {number_text}: {title_clean}"

            # Summary (must be at least 50 chars for Pydantic validation)
            base_summary = final_title

            if len(base_summary) < 50:
                pub_date_str = f"Publicado el {publication_date_str}" if publication_date_str else ""
                base_summary = f"{base_summary}. {pub_date_str}"

                if len(base_summary) < 50:
                    base_summary = f"{base_summary}. Emitido por {issuing_body}"

            summary = base_summary[:500]

            official_url = f"https://bcn.cl/leychile/navegar?idNorma={norm_id}"

            subject_tags = self._extract_subject_tags(root)
            common_name = self._extract_common_name(root)

            return {
                "norm_id": norm_id,
                "norm_type": norm_type,
                "norm_number": number_text,
                "title": final_title,
                "publication_date": publication_date,
                "promulgation_date": promulgation_date,
                "last_modified": last_modified,
                "issuing_body": issuing_body,
                "summary": summary,
                "official_url": official_url,
                "subject_tags": subject_tags,
                "common_name": common_name,
                "version": version_date_str,
            }

        except Exception as e:
            logger.error("metadata_extraction_error", error=str(e))
            return None

    def _extract_subject_tags(self, root: ET.Element) -> List[str]:
        """Extract curated BCN subject tags from <Materias>/<Materia>."""
        tags = []
        for materia_elem in root.findall('.//ns:Materias/ns:Materia', self.NS):
            if materia_elem.text and materia_elem.text.strip():
                tags.append(materia_elem.text.strip())
        return tags

    def _extract_common_name(self, root: ET.Element) -> Optional[str]:
        """Extract popular name from <NombresUsoComun>/<NombreUsoComun>."""
        name_elem = root.find('.//ns:NombresUsoComun/ns:NombreUsoComun', self.NS)
        if name_elem is not None and name_elem.text and name_elem.text.strip():
            return name_elem.text.strip()
        return None

    def extract_norm_vigencia(self, xml_content: str) -> bool:
        """
        Extract global in_force status from XML root derogado attribute.

        Extract vigencia from XML root to propagate to non-articulated chunks.

        Returns:
            True if norm is in force (derogado != "1"), False otherwise
        """
        try:
            root = ET.fromstring(xml_content)
            norma = root.find('.//ns:Norma', self.NS)
            if norma is not None:
                derogado = norma.get('derogado', '0')
                return derogado != '1'
            return True
        except Exception as e:
            logger.warning("vigencia_extraction_error", error=str(e))
            return True  # Default to in_force if extraction fails

    def _map_tipo_to_norm_type(self, type_str: str) -> NormType:
        """
        Map XML type to NormType enum.

        BCN's <Tipo> element contains the specific norm type.
        We map it to our expanded NormType enum.
        """
        type_clean = type_str.strip().lower()

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
            'sentencia': NormType.SENTENCIA,
            'certificado': NormType.CERTIFICADO,
            'dictamen': NormType.DICTAMEN,
            'aviso': NormType.AVISO,
            'bando': NormType.BANDO,
            'notificacion': NormType.NOTIFICACION,
            'notificación': NormType.NOTIFICACION,
            'mensaje': NormType.MENSAJE,
            'otro': NormType.OTRO,
        }

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
        elif 'sentencia' in type_clean:
            return NormType.SENTENCIA
        elif 'certificado' in type_clean:
            return NormType.CERTIFICADO
        elif 'dictamen' in type_clean:
            return NormType.DICTAMEN
        elif 'aviso' in type_clean:
            return NormType.AVISO
        elif 'bando' in type_clean:
            return NormType.BANDO
        elif 'notificacion' in type_clean or 'notificación' in type_clean:
            return NormType.NOTIFICACION
        elif 'mensaje' in type_clean:
            return NormType.MENSAJE
        elif 'otro' in type_clean:
            return NormType.OTRO
        elif 'ley' in type_clean:
            return NormType.LEY
        else:
            logger.warning("unknown_norm_type", type_str=type_str)
            return NormType.LEY

    def _clean_duplicate_title(self, type_text: str, number_text: str, title_raw: str) -> str:
        """
        Clean duplicate titles.

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

        if title_lower.startswith(type_lower):
            remaining = title_raw[len(type_text):].strip()
            remaining = remaining.lstrip(':').lstrip('-').strip()

            if remaining == number_text or not remaining:
                return title_raw

            title_clean = remaining
        else:
            title_clean = title_raw

        # Normalize case: convert all-caps to title case for readability
        if title_clean.isupper() and len(title_clean) > 3:
            # "CÓDIGO PENAL" → "Código Penal"
            title_clean = title_clean.title()

        return title_clean

    def _extract_codigo_name_static(self, type_text: str, number_text: str, title_raw: str) -> Optional[str]:
        """
        Extract proper name for códigos from type, number, and title.

        Static version of ChileanLegalNorm._extract_codigo_name() for use in XML parsing.

        Examples:
            "Código", "PENAL", "CÓDIGO PENAL" -> "Código Penal"
            "Código", "1855", "Codigo Civil" -> "Código Civil"

        Returns:
            Clean código name or None if not extractable
        """
        # Well-known códigos mapping
        KNOWN_CODIGOS = {
            'penal': 'Código Penal',
            '1855': 'Código Civil',
            'civil': 'Código Civil',
            'tributario': 'Código Tributario',
            'comercio': 'Código de Comercio',
            'trabajo': 'Código del Trabajo',
            'procedimiento civil': 'Código de Procedimiento Civil',
            'procedimiento penal': 'Código Procesal Penal',
            'mineria': 'Código de Minería',
            'minería': 'Código de Minería',
            'aguas': 'Código de Aguas',
        }

        # Check norm_number first (most reliable)
        norm_num_lower = number_text.lower()
        if norm_num_lower in KNOWN_CODIGOS:
            return KNOWN_CODIGOS[norm_num_lower]

        title_lower = title_raw.lower()
        for key, name in KNOWN_CODIGOS.items():
            if key in title_lower:
                return name

        if ':' in title_raw:
            parts = title_raw.split(':', 1)
            if len(parts) == 2:
                after_colon = parts[1].strip().title()
                # Avoid duplicated words like "Código Código"
                if after_colon.lower().startswith('codigo'):
                    return after_colon

        return None

    def _extract_full_content(self, root: ET.Element) -> str:
        """
        Extract full text content from XML.

        Concatenates all text from all parts.
        Skips binary attachments (images).
        """
        content_parts = []

        header = root.find('ns:Encabezado/ns:Texto', self.NS)
        if header is not None:
            text = self._strip_margin_notes(self._extract_text_without_binaries(header))
            if text:
                content_parts.append(text)

        for structure in root.findall('.//ns:EstructuraFuncional', self.NS):
            text_elem = structure.find('ns:Texto', self.NS)
            if text_elem is not None:
                text = self._strip_margin_notes(self._extract_text_without_binaries(text_elem))
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

        IMPORTANT: Detects nested structures:
        - Articles inside N°/romano/TÍTULO are marked as nested
        - parent_article is set to the parent container number
        """
        try:
            root = ET.fromstring(xml_content)
            hierarchy = {}

            parent_map = {c: p for p in root.iter() for c in p}

            for structure in self._find_article_structures(root):
                id_parte = structure.get('idParte')
                if not id_parte:
                    continue

                name_elem = structure.find('.//ns:NombreParte', self.NS)
                article_label = name_elem.text.strip() if name_elem is not None and name_elem.text else None

                if article_label:
                    # Normalize suffixes to lowercase (390 BIS -> 390 bis)
                    article_label = re.sub(
                        r'\b(BIS|TER|QUATER|QUINQUIES|SEXIES|SEPTIES|OCTIES|NONIES|DECIES)\b',
                        lambda m: m.group(1).lower(),
                        article_label
                    )

                article_number = self._extract_article_number(article_label) if article_label else None

                if not article_label:
                    # No NombreParte. Some nodes with an article-like tipoParte are actually
                    # section-heading containers that wrap real articles as children (e.g. a
                    # "DISPOSICIONES TRANSITORIAS" divider wrapping the actual transitory
                    # articles) -- those must stay excluded, same as before this fallback.
                    # A genuine article missing only its label is a leaf (no nested article
                    # children); keep those with a fallback label so they aren't silently
                    # dropped from the chunker output.
                    has_nested_articles = any(
                        child.get('tipoParte') in self.ARTICLE_TIPO_PARTE
                        for child in structure.findall('.//ns:EstructuraFuncional', self.NS)
                    )
                    if has_nested_articles:
                        continue
                    article_label = f"Artículo {article_number}" if article_number else "Contenido"

                # 1. Legacy: has "DEL ART" in name
                is_nested_legacy = 'DEL ART' in article_label.upper()
                parent_article = None

                if is_nested_legacy:
                    match = re.search(r'DEL ART[^\d]*(\d+)', article_label, re.IGNORECASE)
                    if match:
                        parent_article = int(match.group(1))

                # 2. NEW: Check if inside a parent structure (N°, romano, TÍTULO, etc.)
                is_nested_structural, parent_structural = self._detect_nested_structure(
                    structure, parent_map
                )

                # Only mark as nested if we have a valid parent_article
                if parent_structural is not None:
                    parent_article = parent_structural

                # Only set is_nested=True if we actually found a parent article number
                is_nested = parent_article is not None

                hierarchy_level = self._get_hierarchy_level(structure, root)

                repealed_attr = structure.get('derogado', 'no derogado')
                in_force = repealed_attr == 'no derogado'

                version_date_str = structure.get('fechaVersion')

                transitorio_attr = structure.get('transitorio', 'no transitorio')
                is_transitory = transitorio_attr == 'transitorio'

                hierarchy[id_parte] = {
                    'article_number': article_number,
                    'article_label': article_label,
                    'is_nested': is_nested,
                    'parent_article': parent_article,
                    'hierarchy_level': hierarchy_level,
                    'in_force': in_force,
                    'version_date': version_date_str,
                    'is_transitory': is_transitory,
                }

            # Extract treaty annexes (Tratados internacionales)
            # Anexos contain full treaty text that would otherwise be missed
            for annex in root.findall('.//ns:Anexo', self.NS):
                id_parte = annex.get('idParte')
                if not id_parte:
                    continue

                title_elem = annex.find('.//ns:Titulo', self.NS)
                annex_title = title_elem.text.strip() if title_elem is not None and title_elem.text else "Anexo"

                repealed_attr = annex.get('derogado', 'no derogado')
                in_force = repealed_attr == 'no derogado'

                version_date_str = annex.get('fechaVersion')

                annex_key = f"anexo_{id_parte}"

                hierarchy[annex_key] = {
                    'article_number': None,
                    'article_label': 'Anexo',
                    'annex_title': annex_title,
                    'is_annex': True,
                    'is_nested': False,
                    'parent_article': None,
                    'hierarchy_level': 0,
                    'in_force': in_force,
                    'version_date': version_date_str,
                    'is_transitory': False,
                }

            logger.info(
                "hierarchy_extracted",
                norm_id=norm_id,
                total_articles=len(hierarchy),
                annexes=sum(1 for h in hierarchy.values() if h.get('is_annex', False))
            )

            return hierarchy

        except Exception as e:
            logger.error("hierarchy_extraction_failed", norm_id=norm_id, error=str(e))
            return {}

    def _extract_article_number(self, article_label: str) -> Optional[int]:
        """Extract article number from label."""
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
        # Only actual nested structures (articles within articles)
        # TÍTULO, Capítulo, Sección are NORMAL hierarchy, not nested articles
        CONTAINER_TYPES = [
            'N°',               # Numbers within articles (nested structure)
            'romano',           # Roman numerals within articles
            'Doble Articulado', # Ordenanzas with nested article structure
            'Párrafo'           # Some use Párrafo as container of articles
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

            for structure in self._find_article_structures(root):
                id_parte = structure.get('idParte')
                if not id_parte:
                    continue

                # Get text element
                text_elem = structure.find('ns:Texto', self.NS)
                if text_elem is not None:
                    # Extract all text, skipping binary attachments and margin notes
                    text = self._strip_margin_notes(self._extract_text_without_binaries(text_elem))
                    if text:
                        article_texts[id_parte] = text

            # Extract treaty annex texts
            # Annexes contain full treaty text that would otherwise be missed
            for annex in root.findall('.//ns:Anexo', self.NS):
                id_parte = annex.get('idParte')
                if not id_parte:
                    continue

                # Get text element
                text_elem = annex.find('ns:Texto', self.NS)
                if text_elem is not None:
                    # Extract all text, skipping binary attachments and margin notes
                    text = self._strip_margin_notes(self._extract_text_without_binaries(text_elem))
                    if text:
                        # Use same special key as in hierarchy
                        annex_key = f"anexo_{id_parte}"
                        article_texts[annex_key] = text

            logger.debug(
                "article_texts_extracted",
                total_articles=len(article_texts),
                annexes=sum(1 for k in article_texts.keys() if k.startswith('anexo_'))
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
            for article in self._find_article_structures(root):
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
            # Added "Capítulo" for Constitución and other norms
            if type_val in ['Libro', 'Título', 'Capítulo', 'Párrafo']:
                # Read <TituloParte> from Metadata (this is where BCN stores the full name)
                title_elem = parent.find('.//ns:TituloParte', self.NS)
                if title_elem is not None and title_elem.text:
                    full_text = title_elem.text.strip()

                    # Parse the full text to separate ordinal from name
                    # Examples:
                    # "LIBRO SEGUNDO CRIMENES Y SIMPLES DELITOS Y SUS PENAS"
                    # "TITULO OCTAVO CRIMENES Y SIMPLES DELITOS CONTRA LAS PERSONAS"
                    # "Capítulo I BASES DE LA INSTITUCIONALIDAD"
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

                    elif type_val == 'Capítulo':
                        # Handle chapters (common in Constitución)
                        # Extract "Capítulo I" and "BASES DE LA INSTITUCIONALIDAD"
                        parts = full_text.split(maxsplit=2)
                        if len(parts) >= 2:
                            ordinal = f"{parts[0]} {parts[1]}"
                            name = parts[2] if len(parts) > 2 else ""
                            context['title_ordinal'] = ordinal
                            context['title_name'] = name

                    elif type_val == 'Párrafo':
                        # Extract "§1 bis" and "Del femicidio"
                        # Pattern: "§N" or "§N bis/ter" followed by ". Name"
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

    def extract_treaty_annex(self, xml_content: str, norm_id: int) -> Optional[str]:
        """
        Extract treaty annex content from XML.

        Treaties (Tratados internacionales) often come with the treaty text
        as an annex in the XML. The decree itself is typically short (promulgation),
        while the full treaty text is in an <Anexo> or similar element.

        TODO: This is a PLACEHOLDER that needs investigation of real treaty XML structure.
              Download XML of a treaty (e.g., Tratado de Escazú) and examine:
              - <Anexo> elements
              - <Adjunto> elements
              - Other possible containers for treaty text
              Then implement extraction logic.

        Args:
            xml_content: Raw XML content
            norm_id: Norm ID for logging

        Returns:
            Treaty annex text if found, None otherwise
        """
        try:
            root = ET.fromstring(xml_content)

            # TODO: Implement actual extraction once XML structure is known
            # Possible patterns to investigate:
            # - root.find('.//ns:Anexo', self.NS)
            # - root.find('.//ns:Adjunto', self.NS)
            # - Elements with tipoParte="Anexo" or similar

            logger.info(
                "treaty_annex_extraction_not_implemented",
                norm_id=norm_id,
                note="Placeholder - needs XML structure investigation"
            )

            return None

        except Exception as e:
            logger.warning(
                "treaty_annex_extraction_failed",
                norm_id=norm_id,
                error=str(e)
            )
            return None
