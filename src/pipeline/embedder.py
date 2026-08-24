"""
Professional embeddings client for AWS Bedrock.

Direct integration with Cohere Embed v4.
"""

import boto3
import structlog
import json
from typing import List
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from botocore.exceptions import ClientError
from pipeline.smart_batcher import SmartBatcher

logger = structlog.get_logger()


class BedrockEmbedder:
    """
    Professional client for Bedrock embeddings.

    Uses Cohere Embed v4:
    - Model: cohere.embed-v4:0
    - Dimensions: 512 (optimal for legal text)
    - Input type: search_document
    - Max input: 128K tokens
    """

    def __init__(
        self,
        region: str = "us-east-1",
        model_id: str = "cohere.embed-v4:0",
        dimensions: int = 512,
        input_type: str = "search_document",
    ):
        self.region = region
        self.model_id = model_id
        self.dimensions = dimensions
        self.input_type = input_type

        # Initialize Bedrock client
        self.client = boto3.client("bedrock-runtime", region_name=region)

        self.stats = {
            "total_calls": 0,
            "total_texts": 0,
            "total_vectors": 0,
            "errors": 0,
        }

        logger.info(
            "embedder_initialized",
            model=model_id,
            dimensions=dimensions,
            region=region,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((ClientError,)),
    )
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts (batch).

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors (512 dimensions each)

        Raises:
            ValueError if texts is empty or too large
        """
        if not texts:
            raise ValueError("texts cannot be empty")

        if len(texts) > 96:
            # Cohere Embed v4 batch limit
            raise ValueError(f"Batch size {len(texts)} exceeds maximum 96")

        self.stats["total_calls"] += 1
        self.stats["total_texts"] += len(texts)

        logger.debug("embedding_batch", batch_size=len(texts))

        try:
            # Prepare request body
            body = {
                "texts": texts,
                "input_type": self.input_type,
                "embedding_types": ["float"],
                "truncate": "END",  # Truncate if exceeds 128K tokens
            }

            # Call Bedrock
            response = self.client.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )

            # Parse response
            response_body = json.loads(response["body"].read())

            # Extract embeddings
            embeddings = response_body.get("embeddings")

            if not embeddings:
                raise ValueError("No embeddings in response")

            if len(embeddings) != len(texts):
                raise ValueError(
                    f"Embedding count mismatch: expected {len(texts)}, got {len(embeddings)}"
                )

            self.stats["total_vectors"] += len(embeddings)

            logger.debug(
                "embedding_success",
                batch_size=len(texts),
                vector_dimensions=len(embeddings[0]) if embeddings else 0,
            )

            return embeddings

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]

            self.stats["errors"] += 1

            logger.error(
                "bedrock_client_error",
                error_code=error_code,
                error_message=error_message,
                batch_size=len(texts),
            )

            # Retry on throttling
            if error_code in ["ThrottlingException", "TooManyRequestsException"]:
                raise  # Let tenacity retry

            # Don't retry on validation errors
            raise ValueError(f"Bedrock error: {error_code} - {error_message}")

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(
                "embedding_unexpected_error",
                error=str(e),
                error_type=type(e).__name__,
                batch_size=len(texts),
            )
            raise

    def embed_one(self, text: str) -> List[float]:
        """
        Generate embedding for single text.

        Args:
            text: Text string to embed

        Returns:
            Embedding vector (512 dimensions)
        """
        vectors = self.embed_texts([text])
        return vectors[0]

    def embed_batch_with_chunking(
        self, texts: List[str], batch_size: int = 96
    ) -> List[List[float]]:
        """
        Embed large list of texts with automatic smart batching.

        Uses SmartBatcher to optimize batches considering both:
        - Text count limit (96 texts)
        - Token count limit (100K tokens per batch)

        Args:
            texts: List of texts (can be > 96)
            batch_size: Ignored (SmartBatcher handles limits automatically)

        Returns:
            List of all embedding vectors
        """
        # Use SmartBatcher for intelligent batching
        batcher = SmartBatcher()
        batches = batcher.create_batches(texts)

        logger.info(
            "smart_batching_applied",
            total_texts=len(texts),
            batches_created=len(batches),
            avg_batch_size=f"{batcher.stats['avg_batch_size']:.1f}",
        )

        all_embeddings = []

        for batch_idx, batch in enumerate(batches, 1):
            embeddings = self.embed_texts(batch)
            all_embeddings.extend(embeddings)

            logger.debug(
                "batch_progress",
                batch=f"{batch_idx}/{len(batches)}",
                batch_size=len(batch),
                processed=len(all_embeddings),
                total=len(texts),
            )

        return all_embeddings

    def get_stats(self) -> dict:
        """Get embedding statistics."""
        return self.stats.copy()
