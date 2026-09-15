"""
Professional S3 client for document storage.

Stores original documents for reference/backup.
"""

import boto3
import gzip
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
        # If keys are empty, boto3 will use IAM role (EC2 instance profile)
        client_kwargs = {"region_name": region}
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key

        self.client = boto3.client("s3", **client_kwargs)

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

    def store_xml(
        self,
        key: str,
        xml_content: str,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """
        Store gzipped XML in S3.

        This must receive the POST-REPAIR XML, i.e. the exact string the parser was
        given -- not the raw HTTP response. BCN's XML is not byte-stable across
        fetches, so re-downloading later does not reproduce the same input.

        Args:
            key: S3 key (path), by convention ending in .xml.gz
            xml_content: XML string exactly as handed to the parser
            metadata: Optional S3 metadata tags

        Returns:
            True if successful
        """
        try:
            raw = xml_content.encode("utf-8")
            # mtime=0 keeps the gzip container byte-identical for identical input,
            # so an unchanged XML does not look changed on re-upload.
            body = gzip.compress(raw, mtime=0)

            self.client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body,
                ContentType="application/xml",
                ContentEncoding="gzip",
                Metadata=metadata or {},
            )

            self.stats["uploaded"] += 1

            logger.debug(
                "xml_stored",
                key=key,
                raw_bytes=len(raw),
                stored_bytes=len(body),
            )

            return True

        except ClientError as e:
            self.stats["failed"] += 1
            logger.error(
                "s3_xml_upload_failed",
                key=key,
                error=e.response["Error"]["Message"],
                error_code=e.response["Error"]["Code"],
            )
            return False

        except Exception as e:
            self.stats["failed"] += 1
            logger.error(
                "s3_xml_unexpected_error",
                key=key,
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

    def get_xml(self, key: str) -> str:
        """
        Get gzipped XML back from S3 as the original string.

        Enables reprocessing a norm without hitting BCN again.

        Args:
            key: S3 key of a .xml.gz object

        Returns:
            XML content, identical to what was passed to store_xml()
        """
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            body = response["Body"].read()

            # boto3 does not transparently decompress; if it ever did, the payload
            # would already be plain XML and gzip would raise BadGzipFile.
            try:
                body = gzip.decompress(body)
            except (OSError, gzip.BadGzipFile):
                pass

            return body.decode("utf-8")

        except ClientError as e:
            logger.error(
                "s3_xml_get_failed",
                key=key,
                error=e.response["Error"]["Message"],
            )
            raise

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

    def build_xml_key(
        self,
        knowledge_id: str,
        doc_id: str,
        prefix: str = "xml",
    ) -> str:
        """
        Build S3 key for a persisted post-repair XML.

        Pattern: {knowledge_id}/{prefix}/{doc_id}.xml.gz

        Args:
            knowledge_id: Knowledge base ID (e.g., "normativabcn")
            doc_id: Document ID (e.g., "bcn-12345")
            prefix: Folder prefix (default: "xml")

        Returns:
            S3 key
        """
        return f"{knowledge_id}/{prefix}/{doc_id}.xml.gz"

    def get_stats(self) -> Dict:
        """Get storage statistics."""
        return self.stats.copy()

    def list_objects(self, prefix: str) -> list[str]:
        """
        List all object keys under a prefix.

        Args:
            prefix: S3 prefix to list

        Returns:
            List of S3 keys
        """
        try:
            keys = []
            paginator = self.client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix=prefix)

            for page in pages:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        keys.append(obj["Key"])

            return keys

        except ClientError as e:
            logger.error(
                "s3_list_failed",
                prefix=prefix,
                error=e.response["Error"]["Message"],
            )
            return []

    def get_object(self, key: str) -> str:
        """
        Get object content from S3.

        Args:
            key: S3 key

        Returns:
            Object content as string
        """
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            return response["Body"].read().decode("utf-8")

        except ClientError as e:
            logger.error(
                "s3_get_failed",
                key=key,
                error=e.response["Error"]["Message"],
            )
            raise
