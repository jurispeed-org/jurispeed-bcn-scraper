"""Storage clients for AWS services."""

from .s3_client import S3Storage
from .opensearch_client import OpenSearchIndexer

__all__ = [
    "S3Storage",
    "OpenSearchIndexer",
]
