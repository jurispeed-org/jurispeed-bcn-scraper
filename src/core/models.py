"""
Data models for Chilean legal norms.

Architecture: Hybrid approach
- Code (variables, functions): English
- Legal domain terms: Spanish (ley, dfl, decreto, etc.)
- Adapter layer: to_lexintel_format() maps to existing schema
"""

from pydantic import BaseModel, HttpUrl, Field, field_validator, model_validator
from datetime import date
from typing import Optional, List
from enum import Enum


class NormType(str, Enum):
    """
    Types of legal norms in Chilean law (28 types from BCN).

    Spanish terms preserved because they have specific legal meaning.
    This enum covers the most common types from BCN's taxonomy.
    """

    LEY = "ley"
    CODIGO = "codigo"

    DFL = "dfl"  # Decreto con Fuerza de Ley
    DL = "decreto_ley"  # Decreto Ley
    DECRETO = "decreto"
    DECRETO_SUPREMO = "decreto_supremo"

    REGLAMENTO = "reglamento"
    RESOLUCION = "resolucion"
    ORDEN = "orden"
    ORDENANZA = "ordenanza"
    OFICIO = "oficio"
    CIRCULAR = "circular"
    INSTRUCCION = "instruccion"

    ORDENANZA_MUNICIPAL = "ordenanza_municipal"

    ACUERDO = "acuerdo"
    CONVENIO = "convenio"
    TRATADO = "tratado"
    AUTO_ACORDADO = "auto_acordado"

    SENTENCIA = "sentencia"
    CERTIFICADO = "certificado"
    DICTAMEN = "dictamen"
    AVISO = "aviso"
    BANDO = "bando"
    NOTIFICACION = "notificacion"
    MENSAJE = "mensaje"
    OTRO = "otro"


class ChileanLegalNorm(BaseModel):
    """
    Validated model for Chilean legal norms from BCN.

    Field names use English for code clarity, but preserve Spanish
    domain terminology where translation would lose legal precision.
    """

    norm_id: int = Field(..., gt=0, description="BCN database ID")

    norm_type: NormType = Field(..., description="Type of legal instrument")
    norm_number: str = Field(..., min_length=1, description="Official norm number")
    # No upper bound: BCN titles are free prose and legitimately run past 500
    # chars (decretos that enumerate what they modify). Capping them rejected
    # valid norms outright. `summary`, which is derived from the title, is
    # truncated at the parser instead of validated away.
    title: str = Field(..., min_length=10)

    publication_date: date = Field(..., description="Official publication date (Diario Oficial)")
    promulgation_date: Optional[date] = Field(
        None, description="Date when signed into law"
    )
    last_modified: Optional[date] = Field(None, description="Last amendment date")

    issuing_body: str = Field(
        ..., min_length=5, description="Government entity that issued the norm"
    )
    version: Optional[str] = Field(None, description="Current version identifier")

    subject_tags: List[str] = Field(
        default_factory=list, description="Legal subject matter tags"
    )
    common_name: Optional[str] = Field(
        None, description="Popular name (e.g. 'Codigo del Trabajo', 'Constitucion')"
    )

    official_url: HttpUrl = Field(..., description="Canonical BCN URL")

    # Content (⭐ critical fields for RAG)
    summary: str = Field(
        ...,
        min_length=50,
        max_length=2000,
        description="Executive summary for RAG retrieval",
    )
    # Floor guards against placeholders ("-", "SIN TEXTO"), not against short
    # norms: a decreto whose whole operative text declares a national date runs
    # to 86 chars and is complete. An actually empty norm has no <Texto> at all,
    # so length is the wrong test for it and a high floor only discards valid text.
    # PR11: the name is finally accurate. `_extract_full_content()` now walks <Encabezado>,
    # every <EstructuraFuncional>, every <Anexo> and <Promulgacion>, in that order. PR10
    # added the promulgation, PR11 the annexes; before them both were absent while present in
    # hierarchy, article_texts and the chunks. There is still no max_length, deliberately:
    # after PR11 the largest measured document (norm 198321, 11 annexes) reaches 1.35M chars,
    # and a cap would silently truncate a treaty. Pinned by
    # tests/test_chunking_text_decoupling.py.
    full_content: str = Field(..., min_length=30, description="Complete legal text")

    # PR9: pipeline input, kept separate from the stored `full_content`.
    #
    # This is the text the chunker uses for routing (`_detect_articles()`) and as the
    # source of the fallback-route chunks. Since PR10 it is NO LONGER the same string as
    # `full_content`, and since PR11 the two can differ by megabytes. That is the point of
    # having two fields: `full_content` gained <Promulgacion> and then <Anexo> without either
    # reaching the chunker, where annex text (full of "Articulo N" lines) would flip
    # `_detect_articles()` and alter the fallback chunks.
    #
    # Not stored: excluded from serialization, and neither the S3 document built in
    # run_scraper.process_norm_data() nor to_lexintel_format() mentions it.
    chunking_text: str = Field(
        default="",
        exclude=True,
        description="Text used only for chunker routing and fallback chunking",
    )

    @model_validator(mode="after")
    def default_chunking_text_to_full_content(self):
        """
        Back-compat for constructors that do not set `chunking_text`.

        The XML parser sets it explicitly. The legacy HTML parser
        (core/parser.py, unreachable from production) builds the model from a plain
        dict and does not, and an empty string would make `chunk()` raise.
        """
        if not self.chunking_text:
            self.chunking_text = self.full_content
        return self

    @field_validator("full_content", "chunking_text")
    @classmethod
    def validate_content_clean(cls, v: str) -> str:
        """Ensure content doesn't contain unparsed HTML."""
        if "<html" in v.lower() or "javascript:" in v.lower():
            raise ValueError("Content contains unparsed HTML")
        return v.strip()

    @field_validator("subject_tags")
    @classmethod
    def normalize_tags(cls, v: List[str]) -> List[str]:
        """Normalize tags to lowercase for consistency."""
        return [tag.strip().lower() for tag in v if tag.strip()]

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str) -> str:
        """Clean and normalize title."""
        return " ".join(v.strip().split())

    @property
    def formal_citation(self) -> str:
        """
        Generate formal citation for this norm.

        Examples:
            - "Ley N° 824"
            - "DFL N° 830"
            - "Código Penal"
            - "Código Civil"

        Returns:
            Formal citation string suitable for legal references
        """
        norm_type_value = self.norm_type.value if hasattr(self.norm_type, 'value') else str(self.norm_type)

        # Special handling for códigos with proper names
        if norm_type_value.lower() == "codigo":
            # Extract code name from title
            codigo_name = self._extract_codigo_name()
            if codigo_name:
                return codigo_name

        # Standard format for other norm types
        type_map = {
            "ley": "Ley",
            "codigo": "Código",
            "dfl": "DFL",
            "decreto_ley": "Decreto Ley",
            "decreto": "Decreto",
            "decreto_supremo": "Decreto Supremo",
            "reglamento": "Reglamento",
            "resolucion": "Resolución",
            "orden": "Orden",
            "ordenanza": "Ordenanza",
            "ordenanza_municipal": "Ordenanza Municipal",
            "oficio": "Oficio",
            "circular": "Circular",
            "instruccion": "Instrucción",
            "acuerdo": "Acuerdo",
            "convenio": "Convenio",
            "tratado": "Tratado",
            "auto_acordado": "Auto Acordado",
        }

        formal_type = type_map.get(norm_type_value.lower(), norm_type_value.title())

        return f"{formal_type} N° {self.norm_number}"

    def _extract_codigo_name(self) -> Optional[str]:
        """
        Extract proper name for códigos from title.

        Examples:
            "Código PENAL: CÓDIGO PENAL" -> "Código Penal"
            "Código 1855: Codigo Civil" -> "Código Civil"
            "Código Tributario: ..." -> "Código Tributario"

        Returns:
            Clean código name or None if not extractable
        """
        title = self.title.lower()

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
            'aguas': 'Código de Aguas',
        }

        # Check norm_number first (most reliable)
        norm_num_lower = self.norm_number.lower()
        if norm_num_lower in KNOWN_CODIGOS:
            return KNOWN_CODIGOS[norm_num_lower]

        # Check title for known patterns
        for key, name in KNOWN_CODIGOS.items():
            if key in title:
                return name

        # Fallback: try to extract from title after colon
        if ':' in self.title:
            parts = self.title.split(':', 1)
            if len(parts) == 2:
                # "Código PENAL: CÓDIGO PENAL" -> take the clean part
                after_colon = parts[1].strip().title()
                # Avoid duplicated words like "Código Código"
                if after_colon.lower().startswith('codigo'):
                    return after_colon

        # If can't extract, return standard format
        return None

    def get_article_citation(
        self,
        article_number: int,
        is_nested: bool = False,
        parent_article: Optional[int] = None
    ) -> str:
        """
        Generate formal citation for a specific article.

        Args:
            article_number: Article number (e.g., 2)
            is_nested: True if article is nested within another (e.g., "DEL ART 1")
            parent_article: Parent article number if nested

        Returns:
            Full citation including article and hierarchy context

        Examples:
            >>> norm.get_article_citation(2)
            "LEY N° 824, Artículo 2"

            >>> norm.get_article_citation(2, is_nested=True, parent_article=1)
            "LEY N° 824, Artículo 2 (del texto aprobado en Artículo 1)"
        """
        base_citation = f"{self.formal_citation}, Artículo {article_number}"

        if is_nested and parent_article:
            # This article is part of the law TEXT approved in a parent decree article
            base_citation += f" (del texto aprobado en Artículo {parent_article})"

        return base_citation

    def get_article_url(self, part_id: Optional[str] = None) -> str:
        """
        Generate URL to specific article using idParte.

        Args:
            part_id: BCN's part ID (e.g., "p8656021")

        Returns:
            Full URL to article

        Examples:
            >>> norm.get_article_url("p8656021")
            "https://www.bcn.cl/leychile/navegar?idNorma=6368&idParte=8656021"

            >>> norm.get_article_url()
            "https://www.bcn.cl/leychile/navegar?idNorma=6368"
        """
        base_url = str(self.official_url)

        if part_id:
            # Add idParte parameter for direct article linking
            separator = "&" if "?" in base_url else "?"
            return f"{base_url}{separator}idParte={part_id}"

        return base_url

    def to_lexintel_format(self) -> dict:
        """
        Convert to Lexintel API format.

        Maps our internal English field names to the Spanish field names
        expected by the existing Lexintel OpenSearch schema.

        Returns:
            Dictionary ready for Lexintel API upload
        """
        return {
            "doc_id": f"bcn-{self.norm_id}",
            "knowledge_id": "normativabcn",
            # Map to existing OpenSearch Spanish field names
            "tipo_norma": self.norm_type.value,
            "numero_norma": self.norm_number,
            "titulo": self.title,
            "fecha_publicacion": self.publication_date.isoformat(),
            "fecha_promulgacion": (
                self.promulgation_date.isoformat() if self.promulgation_date else None
            ),
            "ultima_modificacion": (
                self.last_modified.isoformat() if self.last_modified else None
            ),
            "organismo": self.issuing_body,
            "version": self.version,
            "materias": self.subject_tags,
            "url_oficial": str(self.official_url),
            # RAG fields
            "resumen": self.summary,
            "contenido_completo": self.full_content,
        }

    model_config = {
        "use_enum_values": True,
        "json_encoders": {date: lambda v: v.isoformat()},
    }


class ScraperStats(BaseModel):
    """Statistics for scraper run."""

    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0  # 404s
    retry_count: int = 0
    total_processed: int = 0

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.total_processed == 0:
            return 0.0
        return (self.success_count / self.total_processed) * 100

    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "success": self.success_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "retries": self.retry_count,
            "total": self.total_processed,
            "success_rate": f"{self.success_rate:.2f}%",
        }
