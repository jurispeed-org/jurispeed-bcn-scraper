"""
Professional indexer orchestrator.

Reads pre-chunked documents from S3 and indexes them to OpenSearch.

Pipeline:
1. Read JSON from S3 (chunks already processed during scraping)
2. Generate embeddings (Bedrock Cohere v4)
3. Bulk index to OpenSearch

NO re-chunking: chunks are created during scraping with XML metadata.
"""

import json
import structlog
from typing import Dict, List, Optional
from pipeline.embedder import BedrockEmbedder
from storage.opensearch_client import OpenSearchIndexer
from storage.s3_client import S3Storage
from utils.config import Config

logger = structlog.get_logger()


class ProductionIndexer:
    """
    Production indexer that reads pre-chunked documents from S3.

    Key design:
    - Chunks are created during scraping (with XML metadata)
    - Indexer only generates embeddings and indexes to OpenSearch
    - No re-chunking happens here

    This ensures test and production use same chunking logic.
    """

    def __init__(self, config: Config, s3_bucket: str):
        self.config = config
        self.s3_bucket = s3_bucket

        # Initialize embedder
        self.embedder = BedrockEmbedder(
            region=config.aws.region,
            model_id="global.cohere.embed-v4:0",
            dimensions=512,
            input_type="search_document",
        )

        # Initialize OpenSearch
        # Get host from env var OPENSEARCH_HOST
        import os
        opensearch_host = os.getenv("OPENSEARCH_HOST")
        if not opensearch_host:
            raise ValueError("OPENSEARCH_HOST environment variable required")

        self.opensearch = OpenSearchIndexer(
            host=opensearch_host,
            region=config.lexintel.opensearch_region,
            index_name=config.lexintel.opensearch_index,
            aws_access_key_id=config.aws.access_key_id,
            aws_secret_access_key=config.aws.secret_access_key,
        )

        # Initialize S3 client for reading documents
        self.s3 = S3Storage(
            bucket_name=s3_bucket,
            region=config.aws.region,
            aws_access_key_id=config.aws.access_key_id,
            aws_secret_access_key=config.aws.secret_access_key,
        )

        self.stats = {
            "docs_processed": 0,
            "chunks_indexed": 0,
            "embeddings_generated": 0,
            "opensearch_indexed": 0,
            "failed": 0,
        }

        logger.info(
            "indexer_initialized",
            s3_bucket=s3_bucket,
            opensearch_host=opensearch_host,
            knowledge_id=config.lexintel.knowledge_id
        )

    def index_document_from_s3(self, s3_key: str) -> Dict:
        """
        Index one document from S3.

        Reads pre-chunked JSON from S3, generates embeddings, indexes to OpenSearch.

        Args:
            s3_key: S3 key of document (e.g., "normativabcn/originals/bcn-242302.json")

        Returns:
            Result dict with statistics
        """
        try:
            # Read document from S3
            json_content = self.s3.get_object(s3_key)
            doc_data = json.loads(json_content)

            norm_id = doc_data["norm_id"]
            chunks = doc_data.get("chunks", [])

            if not chunks:
                logger.warning("no_chunks_in_document", s3_key=s3_key, norm_id=norm_id)
                self.stats["failed"] += 1
                return {
                    "success": False,
                    "norm_id": norm_id,
                    "error": "No chunks in document",
                }

            logger.info(
                "indexing_document",
                norm_id=norm_id,
                s3_key=s3_key,
                total_chunks=len(chunks),
            )

            # Extract chunk texts for embedding
            chunk_texts = [chunk["content"] for chunk in chunks]

            # Generate embeddings in batch
            chunk_vectors = self.embedder.embed_batch_with_chunking(
                chunk_texts,
                batch_size=96
            )

            self.stats["embeddings_generated"] += len(chunk_vectors)

            logger.info(
                "embeddings_generated",
                norm_id=norm_id,
                vectors=len(chunk_vectors),
            )

            # Prepare OpenSearch documents
            opensearch_docs = []

            for i, (chunk, vector) in enumerate(zip(chunks, chunk_vectors)):
                doc_id = f"bcn-{norm_id}-chunk-{i}"
                metadata = chunk.get("metadata", {})

                # Build OpenSearch document with flat, schema-aligned fields
                document = {
                    "doc_id": doc_id,
                    "knowledge_id": self.config.lexintel.knowledge_id,
                    # Searchable fields
                    "contentVector": vector,
                    "content": chunk["content"],
                    "title": doc_data.get("title"),
                    "common_name": doc_data.get("common_name"),
                    "subject_tags": doc_data.get("subject_tags", []),
                    "norm_type": doc_data.get("norm_type"),
                    "norm_number": doc_data.get("norm_number"),
                    "norm_id": str(norm_id),
                    "article_label": metadata.get("article_label"),
                    # Numeric form of the article label, for range filters and
                    # ordering ("articles 20 to 25"). Not unique on its own: art. 1
                    # and the 1st transitory provision are both 1, so filters must
                    # pair it with is_transitory.
                    "article_number": metadata.get("article_number"),
                    # Canonical spelling of the label, since BCN's own spelling of
                    # transitory ordinals is inconsistent.
                    "article_label_normalized": metadata.get("article_label_normalized"),
                    "in_force": metadata.get("in_force"),
                    "force_status": metadata.get("force_status"),
                    "is_transitory": metadata.get("is_transitory", False),
                    "publication_date": doc_data.get("publication_date"),
                    "formatted_citation": metadata.get("formatted_citation"),
                    # Stored only (not searchable)
                    "url": metadata.get("official_url"),
                    "chunk_index": chunk.get("chunk_index", i),
                    "total_chunks": len(chunks),
                    "norm_citation": doc_data.get("formal_citation"),
                    "issuing_body": doc_data.get("issuing_body"),
                }

                opensearch_docs.append({"id": doc_id, "body": document})

            # Bulk index to OpenSearch
            bulk_result = self.opensearch.bulk_index(opensearch_docs)
            self.stats["opensearch_indexed"] += bulk_result["success"]
            self.stats["chunks_indexed"] += bulk_result["success"]

            if bulk_result["failed"] > 0:
                logger.warning(
                    "partial_indexing_failure",
                    norm_id=norm_id,
                    success=bulk_result["success"],
                    failed=bulk_result["failed"],
                )

            self.stats["docs_processed"] += 1

            logger.info(
                "document_indexed_complete",
                norm_id=norm_id,
                chunks=len(chunks),
                opensearch_success=bulk_result["success"],
                opensearch_failed=bulk_result["failed"],
            )

            return {
                "success": True,
                "norm_id": norm_id,
                "chunks": len(chunks),
                "opensearch_indexed": bulk_result["success"],
            }

        except json.JSONDecodeError as e:
            self.stats["failed"] += 1
            logger.error(
                "json_parse_failed",
                s3_key=s3_key,
                error=str(e),
            )
            return {
                "success": False,
                "s3_key": s3_key,
                "error": f"JSON parse error: {str(e)}",
            }

        except Exception as e:
            self.stats["failed"] += 1
            logger.error(
                "indexing_failed",
                s3_key=s3_key,
                error=str(e),
                error_type=type(e).__name__,
            )
            return {
                "success": False,
                "s3_key": s3_key,
                "error": str(e),
            }

    def list_documents_from_s3(self, prefix: str = None) -> List[str]:
        """
        List all documents in S3 ready for indexing.

        Args:
            prefix: S3 prefix to list (default: {knowledge_id}/originals/)

        Returns:
            List of S3 keys
        """
        if prefix is None:
            prefix = f"{self.config.lexintel.knowledge_id}/originals/"

        logger.info("listing_s3_documents", prefix=prefix)

        keys = self.s3.list_objects(prefix)

        logger.info("s3_documents_found", total=len(keys), prefix=prefix)

        return keys

    def index_batch_from_s3(
        self,
        s3_keys: Optional[List[str]] = None,
        prefix: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, int]:
        """
        Index multiple documents from S3.

        Args:
            s3_keys: Specific list of S3 keys to index
            prefix: S3 prefix to list and index all documents (if s3_keys not provided)
            limit: Max number of documents to process (optional)

        Returns:
            Statistics: {"success": N, "failed": M, "total": T}
        """
        # Get list of documents to index
        if s3_keys is None:
            s3_keys = self.list_documents_from_s3(prefix)

        # Apply limit if specified
        if limit:
            s3_keys = s3_keys[:limit]

        total = len(s3_keys)
        success = 0
        failed = 0

        logger.info("batch_indexing_start", total=total)

        for i, s3_key in enumerate(s3_keys, 1):
            result = self.index_document_from_s3(s3_key)

            if result["success"]:
                success += 1
            else:
                failed += 1

            # Log progress every 10 documents
            if i % 10 == 0:
                logger.info(
                    "batch_progress",
                    processed=i,
                    total=total,
                    success=success,
                    failed=failed,
                    success_rate=f"{(success / i) * 100:.1f}%",
                )

        logger.info(
            "batch_indexing_complete",
            total=total,
            success=success,
            failed=failed,
            success_rate=f"{(success / total) * 100:.1f}%" if total > 0 else "0%",
        )

        return {"success": success, "failed": failed, "total": total}

    def get_stats(self) -> Dict:
        """Get comprehensive statistics."""
        return {
            **self.stats,
            "embedder": self.embedder.get_stats(),
            "opensearch": self.opensearch.get_stats(),
        }

    def close(self):
        """Close all connections."""
        self.opensearch.close()
        logger.info("indexer_closed", final_stats=self.get_stats())
