"""
Data models for Chilean legal norms.

Architecture: Hybrid approach
- Code (variables, functions): English
- Legal domain terms: Spanish (ley, dfl, decreto, etc.)
- Adapter layer: to_lexintel_format() maps to existing schema
"""

from pydantic import BaseModel, HttpUrl, Field, field_validator
from datetime import date
from typing import Optional, List
from enum import Enum


class NormType(str, Enum):
    """
    Types of legal norms in Chilean law.

    Spanish terms preserved because they have specific legal meaning:
    - LEY: Law (legislation passed by Congress)
    - CODIGO: Legal code (comprehensive law compilation)
    - DFL: Decree with Force of Law (executive decree with legislative power)
    - DECRETO: Decree (executive regulation)
    - REGLAMENTO: Regulation (administrative rules)
    """

    LEY = "ley"
    CODIGO = "codigo"
    DFL = "dfl"
    DECRETO = "decreto"
    REGLAMENTO = "reglamento"


class ChileanLegalNorm(BaseModel):
    """
    Validated model for Chilean legal norms from BCN.

    Field names use English for code clarity, but preserve Spanish
    domain terminology where translation would lose legal precision.
    """

    # Identifiers
    norm_id: int = Field(..., gt=0, description="BCN database ID")

    # Core metadata
    norm_type: NormType = Field(..., description="Type of legal instrument")
    norm_number: str = Field(..., min_length=1, description="Official norm number")
    title: str = Field(..., min_length=10, max_length=500)

    # Dates
    publication_date: date = Field(..., description="Official publication date (Diario Oficial)")
    promulgation_date: Optional[date] = Field(
        None, description="Date when signed into law"
    )
    last_modified: Optional[date] = Field(None, description="Last amendment date")

    # Issuing authority
    issuing_body: str = Field(
        ..., min_length=5, description="Government entity that issued the norm"
    )
    version: Optional[str] = Field(None, description="Current version identifier")

    # Classification
    subject_tags: List[str] = Field(
        default_factory=list, description="Legal subject matter tags"
    )

    # URLs
    official_url: HttpUrl = Field(..., description="Canonical BCN URL")

    # Content (⭐ critical fields for RAG)
    summary: str = Field(
        ...,
        min_length=50,
        max_length=2000,
        description="Executive summary for RAG retrieval",
    )
    full_content: str = Field(..., min_length=100, description="Complete legal text")

    @field_validator("full_content")
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
