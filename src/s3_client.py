"""
Professional S3 client for document storage.

Stores original documents for reference/backup.
"""

import boto3
import structlog
import json
from typing import Dict, Optional
from botocore.exceptions import ClientError

logger = structlog.get_logger()


class S3Storage:
    """
    Professional S3 client for storing legal documents.

    Features:
    - Structured path organization
    - JSON storage
    - Metadata tagging
    - Error handling
    """

    def __init__(
        self,
        bucket_name: str,
        region: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ):
        self.bucket_name = bucket_name
        self.region = region

        # Initialize S3 client
        self.client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

        self.stats = {
            "uploaded": 0,
            "failed": 0,
        }

        logger.info(
            "s3_initialized",
            bucket=bucket_name,
            region=region,
        )

    def store_document(
        self,
        key: str,
        data: Dict,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """
        Store document in S3 as JSON.

        Args:
            key: S3 key (path)
            data: Document data (will be JSON serialized)
            metadata: Optional S3 metadata tags

        Returns:
            True if successful
        """
        try:
            # Serialize to JSON
            body = json.dumps(data, ensure_ascii=False, indent=2)

            # Prepare metadata
            s3_metadata = metadata or {}

            # Upload
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
                Metadata=s3_metadata,
            )

            self.stats["uploaded"] += 1

            logger.debug(
                "document_stored",
                key=key,
                size_bytes=len(body),
            )

            return True

        except ClientError as e:
            self.stats["failed"] += 1
            logger.error(
                "s3_upload_failed",
                key=key,
                error=e.response["Error"]["Message"],
                error_code=e.response["Error"]["Code"],
            )
            return False

        except Exception as e:
            self.stats["failed"] += 1
            logger.error(
                "s3_unexpected_error",
                key=key,
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

    def build_key(
        self,
        knowledge_id: str,
        doc_id: str,
        prefix: str = "originals",
    ) -> str:
        """
        Build S3 key following standard pattern.

        Pattern: {knowledge_id}/{prefix}/{doc_id}.json

        Args:
            knowledge_id: Knowledge base ID (e.g., "normativabcn")
            doc_id: Document ID (e.g., "bcn-12345")
            prefix: Folder prefix (default: "originals")

        Returns:
            S3 key
        """
        return f"{knowledge_id}/{prefix}/{doc_id}.json"

    def get_stats(self) -> Dict:
        """Get storage statistics."""
        return self.stats.copy()
