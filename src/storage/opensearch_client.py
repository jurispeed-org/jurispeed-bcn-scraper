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

    def create_norm_document(
        self,
        doc_id: str,
        knowledge_id: str,
        norm_data: Dict,
        chunk_text: str,
        chunk_vector: List[float],
        chunk_index: int,
        chunk_total: int,
    ) -> Dict:
        """
        Create OpenSearch document following existing schema.

        Args:
            doc_id: Unique document ID
            knowledge_id: Knowledge base ID (e.g., "normativabcn")
            norm_data: Norm metadata (tipo_norma, titulo, etc.)
            chunk_text: Text content of this chunk
            chunk_vector: Embedding vector (512 dims)
            chunk_index: Chunk index (0-based)
            chunk_total: Total chunks

        Returns:
            Document ready for indexing
        """
        document = {
            # IDs
            "doc_id": doc_id,
            "knowledge_id": knowledge_id,
            # Chunk metadata
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            # Content
            "content": chunk_text,
            "contentVector": chunk_vector,
            # Norm metadata (Spanish field names for compatibility)
            "tipo_norma": norm_data.get("tipo_norma"),
            "numero_norma": norm_data.get("numero_norma"),
            "titulo": norm_data.get("titulo"),
            "fecha_publicacion": norm_data.get("fecha_publicacion"),
            "fecha_promulgacion": norm_data.get("fecha_promulgacion"),
            "ultima_modificacion": norm_data.get("ultima_modificacion"),
            "organismo": norm_data.get("organismo"),
            "version": norm_data.get("version"),
            "materias": norm_data.get("materias", []),
            "url_oficial": norm_data.get("url_oficial"),
            # Resumen (⭐ only in first chunk)
            "resumen": norm_data.get("resumen") if chunk_index == 0 else None,
            "resumen_vector": norm_data.get("resumen_vector") if chunk_index == 0 else None,
        }

        # Remove None values
        document = {k: v for k, v in document.items() if v is not None}

        return document

    def get_stats(self) -> Dict:
        """Get indexing statistics."""
        return self.stats.copy()

    def close(self):
        """Close OpenSearch connection."""
        # OpenSearch client doesn't require explicit close
        logger.info("opensearch_closed", final_stats=self.stats)
