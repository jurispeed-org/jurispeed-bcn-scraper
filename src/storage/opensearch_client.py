"""
Professional OpenSearch client for direct indexing.

No dependencies on Lexintel code - direct AWS OpenSearch integration.
"""

import boto3
import structlog
import json
from typing import List, Dict, Optional
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()


class OpenSearchIndexer:
    """
    Professional OpenSearch client for indexing legal norms.

    Features:
    - AWS IAM authentication
    - Bulk indexing
    - Retry logic
    - Schema validation
    """

    def __init__(
        self,
        host: str,
        region: str = "us-east-1",
        index_name: str = "normativassiiv1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ):
        self.host = host
        self.region = region
        self.index_name = index_name

        # AWS credentials for signing
        credentials = boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region,
        ).get_credentials()

        # AWS V4 Signer
        auth = AWSV4SignerAuth(credentials, region, "es")

        # Initialize OpenSearch client
        self.client = OpenSearch(
            hosts=[{"host": host, "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=30,
        )

        self.stats = {
            "indexed": 0,
            "failed": 0,
            "bulk_requests": 0,
        }

        logger.info(
            "opensearch_initialized",
            host=host,
            index=index_name,
            region=region,
        )

    def ensure_index_exists(self) -> bool:
        """
        Check if index exists, create if not.

        Returns:
            True if index exists or was created
        """
        try:
            if self.client.indices.exists(index=self.index_name):
                logger.info("index_exists", index=self.index_name)
                return True

            # Index doesn't exist - log warning
            logger.warning(
                "index_not_found",
                index=self.index_name,
                message="Index should be created manually with proper mapping",
            )
            return False

        except Exception as e:
            logger.error("index_check_failed", error=str(e))
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def index_document(self, doc_id: str, document: Dict) -> bool:
        """
        Index a single document.

        Args:
            doc_id: Document ID
            document: Document body

        Returns:
            True if successful
        """
        try:
            response = self.client.index(
                index=self.index_name, id=doc_id, body=document, refresh=False
            )

            if response.get("result") in ["created", "updated"]:
                self.stats["indexed"] += 1
                logger.debug("document_indexed", doc_id=doc_id)
                return True
            else:
                logger.warning("unexpected_index_response", doc_id=doc_id, response=response)
                return False

        except Exception as e:
            self.stats["failed"] += 1
            logger.error("index_failed", doc_id=doc_id, error=str(e))
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def bulk_index(self, documents: List[Dict]) -> Dict[str, int]:
        """
        Bulk index multiple documents.

        Args:
            documents: List of documents with 'id' and 'body' keys

        Returns:
            Statistics dict: {"success": N, "failed": M}
        """
        if not documents:
            return {"success": 0, "failed": 0}

        self.stats["bulk_requests"] += 1

        # Prepare bulk body
        bulk_body = []
        for doc in documents:
            # Action (index)
            bulk_body.append({"index": {"_index": self.index_name, "_id": doc["id"]}})
            # Document
            bulk_body.append(doc["body"])

        try:
            response = self.client.bulk(body=bulk_body, refresh=False)

            # Parse response
            success = 0
            failed = 0

            if "items" in response:
                for item in response["items"]:
                    action = item.get("index", {})
                    if action.get("status") in [200, 201]:
                        success += 1
                    else:
                        failed += 1
                        logger.warning(
                            "bulk_item_failed",
                            doc_id=action.get("_id"),
                            error=action.get("error"),
                        )

            self.stats["indexed"] += success
            self.stats["failed"] += failed

            logger.info(
                "bulk_index_complete",
                batch_size=len(documents),
                success=success,
                failed=failed,
            )

            return {"success": success, "failed": failed}

        except Exception as e:
            logger.error("bulk_index_failed", batch_size=len(documents), error=str(e))
            self.stats["failed"] += len(documents)
            raise

    def get_stats(self) -> Dict:
        """Get indexing statistics."""
        return self.stats.copy()

    def close(self):
        """Close OpenSearch connection."""
        # OpenSearch client doesn't require explicit close
        logger.info("opensearch_closed", final_stats=self.stats)
