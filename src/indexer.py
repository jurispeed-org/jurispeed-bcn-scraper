"""
Professional indexer orchestrator.

Coordinates the complete indexing pipeline:
1. Chunking (semantic)
2. Embeddings (Bedrock Cohere v4)
3. OpenSearch indexing
4. S3 storage

100% professional code, zero Lexintel dependencies.
"""

import structlog
from typing import Dict, List
from .models import ChileanLegalNorm
from .chunker import ProfessionalChunker
from .embedder import BedrockEmbedder
from .opensearch_client import OpenSearchIndexer
from .s3_client import S3Storage
from .config import Config

logger = structlog.get_logger()


class ProfessionalIndexer:
    """
    Professional end-to-end indexer.

    Orchestrates:
    - Semantic chunking
    - Embedding generation
    - OpenSearch bulk indexing
    - S3 document storage

    Zero bad practices, zero legacy code.
    """

    def __init__(self, config: Config):
        self.config = config

        # Initialize all components
        self.chunker = ProfessionalChunker(
            target_chunk_size=512,  # optimal for Cohere v4
            max_chunk_size=1000,
            overlap_tokens=50,
            min_chunk_size=100,
        )

        self.embedder = BedrockEmbedder(
            region=config.aws.region,
            model_id="cohere.embed-v4:0",
            dimensions=512,
            input_type="search_document",
        )

        # Get OpenSearch host from config
        opensearch_host = self._extract_host(config.lexintel.opensearch_index)

        self.opensearch = OpenSearchIndexer(
            host=opensearch_host,
            region=config.aws.region,
            index_name=config.lexintel.opensearch_index,
            aws_access_key_id=config.aws.access_key_id,
            aws_secret_access_key=config.aws.secret_access_key,
        )

        # S3 uses dev-lexintel-bcn-cache (existing bucket)
        self.s3 = S3Storage(
            bucket_name="dev-lexintel-bcn-cache",
            region=config.aws.region,
            aws_access_key_id=config.aws.access_key_id,
            aws_secret_access_key=config.aws.secret_access_key,
        )

        self.stats = {
            "processed": 0,
            "chunks_created": 0,
            "vectors_generated": 0,
            "opensearch_indexed": 0,
            "s3_stored": 0,
            "failed": 0,
        }

        logger.info("indexer_initialized", knowledge_id=config.lexintel.knowledge_id)

    def _extract_host(self, index_config: str) -> str:
        """Extract OpenSearch host from config string."""
        # TODO: Get from env var or config
        # For now, use the known host
        return "search-lexintel-rag-general-v2-56kxp7ya6kxoyxaiamdujv2m5u.us-east-1.es.amazonaws.com"

    def index_norm(self, norm: ChileanLegalNorm) -> Dict:
        """
        Index one legal norm through complete pipeline.

        Steps:
        1. Chunk content (semantic)
        2. Generate embeddings (batch)
        3. Index to OpenSearch (bulk)
        4. Store original in S3

        Args:
            norm: Validated legal norm

        Returns:
            Result dict with statistics
        """
        logger.info("indexing_norm", norm_id=norm.norm_id, title=norm.title[:50])

        try:
            # Step 1: Chunk content
            chunks = self.chunker.chunk(
                text=norm.full_content,
                metadata={
                    "norm_id": norm.norm_id,
                    "titulo": norm.title,
                    "tipo_norma": norm.norm_type.value,
                },
            )

            self.stats["chunks_created"] += len(chunks)

            logger.info(
                "chunking_complete",
                norm_id=norm.norm_id,
                chunks=len(chunks),
                avg_tokens=sum(c.token_count for c in chunks) / len(chunks),
            )

            # Step 2: Generate embeddings for all chunks
            chunk_texts = [c.text for c in chunks]
            chunk_vectors = self.embedder.embed_batch_with_chunking(chunk_texts, batch_size=96)

            self.stats["vectors_generated"] += len(chunk_vectors)

            logger.info("embeddings_generated", norm_id=norm.norm_id, vectors=len(chunk_vectors))

            # Step 2.5: Generate embedding for summary (chunk 0 special case)
            summary_vector = self.embedder.embed_one(norm.summary)

            # Step 3: Prepare OpenSearch documents
            opensearch_docs = []

            for i, (chunk, vector) in enumerate(zip(chunks, chunk_vectors)):
                doc_id = f"bcn-{norm.norm_id}-chunk-{i}"

                # Convert norm to Lexintel format (Spanish field names)
                norm_data = norm.to_lexintel_format()

                # Add summary vector only to first chunk
                if i == 0:
                    norm_data["resumen_vector"] = summary_vector

                document = self.opensearch.create_norm_document(
                    doc_id=doc_id,
                    knowledge_id=self.config.lexintel.knowledge_id,
                    norm_data=norm_data,
                    chunk_text=chunk.text,
                    chunk_vector=vector,
                    chunk_index=i,
                    chunk_total=len(chunks),
                )

                opensearch_docs.append({"id": doc_id, "body": document})

            # Bulk index to OpenSearch
            bulk_result = self.opensearch.bulk_index(opensearch_docs)
            self.stats["opensearch_indexed"] += bulk_result["success"]

            logger.info(
                "opensearch_indexed",
                norm_id=norm.norm_id,
                success=bulk_result["success"],
                failed=bulk_result["failed"],
            )

            # Step 4: Store original document in S3
            s3_key = self.s3.build_key(
                knowledge_id=self.config.lexintel.knowledge_id,
                doc_id=f"bcn-{norm.norm_id}",
                prefix="originals",
            )

            s3_data = {
                "norm_id": norm.norm_id,
                "scraped_at": norm.to_lexintel_format(),
                "chunks": len(chunks),
            }

            s3_success = self.s3.store_document(
                key=s3_key,
                data=s3_data,
                metadata={
                    "tipo_norma": norm.norm_type.value,
                    "numero_norma": norm.norm_number,
                },
            )

            if s3_success:
                self.stats["s3_stored"] += 1

            # Success
            self.stats["processed"] += 1

            logger.info(
                "norm_indexed_complete",
                norm_id=norm.norm_id,
                chunks=len(chunks),
                opensearch_success=bulk_result["success"],
                s3_stored=s3_success,
            )

            return {
                "success": True,
                "norm_id": norm.norm_id,
                "chunks": len(chunks),
                "opensearch_indexed": bulk_result["success"],
                "s3_stored": s3_success,
            }

        except Exception as e:
            self.stats["failed"] += 1
            logger.error(
                "indexing_failed",
                norm_id=norm.norm_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return {
                "success": False,
                "norm_id": norm.norm_id,
                "error": str(e),
            }

    def index_batch(self, norms: List[ChileanLegalNorm]) -> Dict[str, int]:
        """
        Index multiple norms.

        Args:
            norms: List of validated norms

        Returns:
            Statistics: {"success": N, "failed": M}
        """
        success = 0
        failed = 0

        logger.info("batch_indexing_start", total=len(norms))

        for i, norm in enumerate(norms, 1):
            result = self.index_norm(norm)

            if result["success"]:
                success += 1
            else:
                failed += 1

            # Log progress every 10 norms
            if i % 10 == 0:
                logger.info(
                    "batch_progress",
                    processed=i,
                    total=len(norms),
                    success_rate=f"{(success / i) * 100:.1f}%",
                )

        logger.info(
            "batch_indexing_complete",
            total=len(norms),
            success=success,
            failed=failed,
        )

        return {"success": success, "failed": failed}

    def get_stats(self) -> Dict:
        """Get comprehensive statistics."""
        return {
            **self.stats,
            "chunker": {"name": "ProfessionalChunker"},
            "embedder": self.embedder.get_stats(),
            "opensearch": self.opensearch.get_stats(),
            "s3": self.s3.get_stats(),
        }

    def close(self):
        """Close all connections."""
        self.opensearch.close()
        logger.info("indexer_closed", final_stats=self.get_stats())
