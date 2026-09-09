"""Configuration management with environment variables."""

import os
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()


class AWSConfig(BaseModel):
    """AWS service configuration."""

    access_key_id: str = Field(..., description="AWS access key")
    secret_access_key: str = Field(..., description="AWS secret key")
    region: str = Field(default="us-east-1", description="AWS region")
    checkpoint_table: str = Field(
        default="jurispeed-scraper-checkpoints",
        description="DynamoDB table for checkpoints"
    )
    norm_status_table: str = Field(
        default="jurispeed-norm-status",
        description="DynamoDB table for individual norm status tracking"
    )


class LexintelConfig(BaseModel):
    """Lexintel API configuration."""

    api_url: str = Field(..., description="Lexintel API base URL")
    username: str = Field(..., description="OAuth username")
    clientname: str = Field(..., description="OAuth client name")
    password: str = Field(..., description="OAuth password")
    knowledge_id: str = Field(
        default="normativabcn",
        description="Target knowledge base ID"
    )
    opensearch_index: str = Field(
        default="normativassiiv1",
        description="Target OpenSearch index"
    )
    opensearch_region: str = Field(
        default="us-east-1",
        description="AWS region of the OpenSearch domain (for SigV4 signing)"
    )


class ScraperConfig(BaseModel):
    """Scraper behavior configuration."""

    rate_limit_seconds: float = Field(
        default=2.5,
        ge=1.0,
        le=10.0,
        description="Delay between requests"
    )
    max_retries: int = Field(
        default=7,
        ge=1,
        le=15,
        description="Max retry attempts for XML (no HTML fallback)"
    )
    timeout_seconds: int = Field(
        default=30,
        ge=10,
        le=120,
        description="Initial HTTP request timeout (increases per retry)"
    )
    max_timeout_seconds: int = Field(
        default=60,
        ge=30,
        le=180,
        description="Maximum timeout for retries"
    )
    retry_backoff_base: float = Field(
        default=2.0,
        ge=1.0,
        le=5.0,
        description="Base for exponential backoff (seconds)"
    )
    retry_backoff_max: float = Field(
        default=64.0,
        ge=10.0,
        le=120.0,
        description="Maximum backoff time (seconds)"
    )
    checkpoint_every: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Save checkpoint every N docs"
    )


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO", description="Log level")
    format: str = Field(default="json", description="Log format (json or console)")


class Config(BaseModel):
    """Main application configuration."""

    aws: AWSConfig
    lexintel: LexintelConfig
    scraper: ScraperConfig
    logging: LoggingConfig

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            aws=AWSConfig(
                access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
                secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
                region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                checkpoint_table=os.getenv("CHECKPOINT_TABLE_NAME", "jurispeed-scraper-checkpoints"),
            ),
            lexintel=LexintelConfig(
                api_url=os.getenv("LEXINTEL_API_URL", ""),
                username=os.getenv("LEXINTEL_USERNAME", ""),
                clientname=os.getenv("LEXINTEL_CLIENTNAME", ""),
                password=os.getenv("LEXINTEL_PASSWORD", ""),
                knowledge_id=os.getenv("KNOWLEDGE_ID", "normativabcn"),
                opensearch_index=os.getenv("OPENSEARCH_INDEX", "normativassiiv1"),
                opensearch_region=os.getenv("OPENSEARCH_REGION", "us-east-1"),
            ),
            scraper=ScraperConfig(
                rate_limit_seconds=float(os.getenv("RATE_LIMIT_SECONDS", "2.5")),
                max_retries=int(os.getenv("MAX_RETRIES", "3")),
                timeout_seconds=int(os.getenv("TIMEOUT_SECONDS", "30")),
                checkpoint_every=int(os.getenv("CHECKPOINT_EVERY", "1000")),
            ),
            logging=LoggingConfig(
                level=os.getenv("LOG_LEVEL", "INFO"),
                format=os.getenv("LOG_FORMAT", "json"),
            ),
        )

    def validate_required(self) -> None:
        """Validate that required fields are set."""
        errors = []

        # AWS keys are optional - EC2 instances use IAM roles
        # if not self.aws.access_key_id:
        #     errors.append("AWS_ACCESS_KEY_ID is required")
        # if not self.aws.secret_access_key:
        #     errors.append("AWS_SECRET_ACCESS_KEY is required")

        if not self.lexintel.api_url:
            errors.append("LEXINTEL_API_URL is required")
        if not self.lexintel.username:
            errors.append("LEXINTEL_USERNAME is required")
        if not self.lexintel.password:
            errors.append("LEXINTEL_PASSWORD is required")

        if errors:
            raise ValueError(f"Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))
