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

            # Dates (both optional - some norms don't have fechaPublicacion)
            fecha_promulgacion = identificador.get('fechaPromulgacion')
            fecha_publicacion = identificador.get('fechaPublicacion')

            promulgation_date = date.fromisoformat(fecha_promulgacion) if fecha_promulgacion else None
            publication_date = date.fromisoformat(fecha_publicacion) if fecha_publicacion else None

            # If no publication date, use promulgation or version date as fallback
            if not publication_date:
                fecha_version = root.get('fechaVersion')
                publication_date = promulgation_date or (date.fromisoformat(fecha_version) if fecha_version else date.today())

            # Last modified (from root)
            fecha_version = root.get('fechaVersion')
            last_modified = date.fromisoformat(fecha_version) if fecha_version else None

            # Type and number
            tipo_numero = identificador.find('.//ns:TipoNumero', self.NS)
            if tipo_numero is None:
                return None

            tipo_elem = tipo_numero.find('ns:Tipo', self.NS)
            numero_elem = tipo_numero.find('ns:Numero', self.NS)

            if tipo_elem is None or numero_elem is None:
                return None

            tipo_text = tipo_elem.text.strip()
            numero_text = numero_elem.text.strip()

            # Map tipo to NormType
            norm_type = self._map_tipo_to_norm_type(tipo_text)

            # Organismo
            organismo_elem = identificador.find('.//ns:Organismo', self.NS)
            issuing_body = organismo_elem.text.strip() if organismo_elem is not None else "MINISTERIO"

            # Metadatos
            metadatos = root.find('ns:Metadatos', self.NS)
            titulo_elem = metadatos.find('ns:TituloNorma', self.NS) if metadatos is not None else None
            title = titulo_elem.text.strip() if titulo_elem is not None and titulo_elem.text else f"{tipo_text} {numero_text}"

            # Summary (must be at least 50 chars for Pydantic validation)
            base_summary = f"{tipo_text} {numero_text}: {title}"

            # Ensure minimum 50 chars
            if len(base_summary) < 50:
                # Add publication info
                fecha_pub_str = f"Publicado el {fecha_publicacion}" if fecha_publicacion else ""
                base_summary = f"{base_summary}. {fecha_pub_str}"

                # If still too short, add organismo
                if len(base_summary) < 50:
                    base_summary = f"{base_summary}. Emitido por {issuing_body}"

            # Truncate if too long
            summary = base_summary[:500]

            # Official URL
            official_url = f"https://bcn.cl/leychile/navegar?idNorma={norm_id}"

            return {
                "norm_id": norm_id,
                "norm_type": norm_type,
                "norm_number": numero_text,
                "title": f"{tipo_text} {numero_text}: {title}",
                "publication_date": publication_date,
                "promulgation_date": promulgation_date,
                "last_modified": last_modified,
                "issuing_body": issuing_body,
                "summary": summary,
                "official_url": official_url,
                "subject_tags": [],
                "version": fecha_version,
            }

        except Exception as e:
            logger.error("metadata_extraction_error", error=str(e))
            return None

    def _map_tipo_to_norm_type(self, tipo: str) -> NormType:
        """Map XML tipo to NormType enum."""
        tipo_lower = tipo.lower()

        if 'ley' in tipo_lower:
            return NormType.LEY
        elif 'codigo' in tipo_lower or 'código' in tipo_lower:
            return NormType.CODIGO
        elif 'dfl' in tipo_lower:
            return NormType.DFL
        elif 'decreto' in tipo_lower:
            return NormType.DECRETO
        elif 'reglamento' in tipo_lower:
            return NormType.REGLAMENTO
        else:
            return NormType.LEY

    def _extract_full_content(self, root: ET.Element) -> str:
        """
        Extract full text content from XML.

        Concatenates all text from all parts.
        """
        content_parts = []

        # Encabezado
        encabezado = root.find('ns:Encabezado/ns:Texto', self.NS)
        if encabezado is not None and encabezado.text:
            content_parts.append(encabezado.text.strip())

        # All EstructuraFuncional texts
        for estructura in root.findall('.//ns:EstructuraFuncional', self.NS):
            texto_elem = estructura.find('ns:Texto', self.NS)
            if texto_elem is not None and texto_elem.text:
                content_parts.append(texto_elem.text.strip())

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
            for estructura in root.findall('.//ns:EstructuraFuncional[@tipoParte="Artículo"]', self.NS):
                id_parte = estructura.get('idParte')
                if not id_parte:
                    continue

                # Get article name
                nombre_elem = estructura.find('.//ns:NombreParte', self.NS)
                article_label = nombre_elem.text.strip() if nombre_elem is not None and nombre_elem.text else None

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
                hierarchy_level = self._get_hierarchy_level(estructura, root)

                # Vigencia (derogado attribute) - GRATIS!
                derogado = estructura.get('derogado', 'no derogado')
                vigente = derogado == 'no derogado'

                # Fecha version
                fecha_version = estructura.get('fechaVersion')

                hierarchy[id_parte] = {
                    'article_number': article_number,
                    'article_label': article_label,
                    'is_nested': is_nested,
                    'parent_article': parent_article,
                    'hierarchy_level': hierarchy_level,
                    'vigente': vigente,  # ✅ Vigencia gratis!
                    'fecha_version': fecha_version,
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

    def extract_article_texts(self, xml_content: str) -> Dict[str, str]:
        """
        Extract article texts from XML.

        Returns dict mapping idParte -> article text.

        This is what the chunker will use.
        """
        try:
            root = ET.fromstring(xml_content)
            article_texts = {}

            for estructura in root.findall('.//ns:EstructuraFuncional[@tipoParte="Artículo"]', self.NS):
                id_parte = estructura.get('idParte')
                if not id_parte:
                    continue

                # Get text
                texto_elem = estructura.find('ns:Texto', self.NS)
                if texto_elem is not None and texto_elem.text:
                    article_texts[id_parte] = texto_elem.text.strip()

            logger.debug(
                "article_texts_extracted",
                total_articles=len(article_texts)
            )

            return article_texts

        except Exception as e:
            logger.error("article_texts_extraction_failed", error=str(e))
            return {}
