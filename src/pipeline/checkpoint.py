"""
Checkpoint manager with DynamoDB.

Enables resume functionality for long-running scraping jobs.
"""

import boto3
import structlog
from typing import Optional, Dict
from datetime import datetime
from botocore.exceptions import ClientError
from ..core.models import ScraperStats
from ..utils.config import AWSConfig

logger = structlog.get_logger()


class CheckpointManager:
    """
    Manages scraper checkpoints in DynamoDB.

    Features:
    - Granular checkpoints (every 1000 docs)
    - Resume functionality
    - Progress tracking
    - Error recovery
    """

    def __init__(self, config: AWSConfig, instance_id: str):
        self.config = config
        self.instance_id = instance_id

        # Initialize DynamoDB client
        # If keys are empty, boto3 will use IAM role (EC2 instance profile)
        client_kwargs = {"region_name": config.region}
        if config.access_key_id and config.secret_access_key:
            client_kwargs["aws_access_key_id"] = config.access_key_id
            client_kwargs["aws_secret_access_key"] = config.secret_access_key

        self.dynamodb = boto3.resource("dynamodb", **client_kwargs)

        self.table = self.dynamodb.Table(config.checkpoint_table)

        logger.info(
            "checkpoint_manager_initialized",
            instance_id=instance_id,
            table=config.checkpoint_table,
            region=config.region,
        )

    def save(
        self,
        last_id_processed: int,
        stats: ScraperStats,
        metadata: Optional[Dict] = None,
    ) -> None:
        """
        Save checkpoint to DynamoDB.

        Args:
            last_id_processed: Last norm ID successfully processed
            stats: Current scraper statistics
            metadata: Optional additional metadata
        """
        try:
            item = {
                "instance_id": self.instance_id,
                "last_id_processed": last_id_processed,
                "total_processed": stats.total_processed,
                "success_count": stats.success_count,
                "failed_count": stats.failed_count,
                "skipped_count": stats.skipped_count,
                "retry_count": stats.retry_count,
                "timestamp": datetime.utcnow().isoformat(),
                "status": "running",
            }

            # Add optional metadata
            if metadata:
                item["metadata"] = metadata

            self.table.put_item(Item=item)

            logger.info(
                "checkpoint_saved",
                instance_id=self.instance_id,
                last_id=last_id_processed,
                success_rate=f"{stats.success_rate:.1f}%",
                total=stats.total_processed,
            )

        except ClientError as e:
            logger.error(
                "checkpoint_save_failed",
                instance_id=self.instance_id,
                error=e.response["Error"]["Message"],
                error_code=e.response["Error"]["Code"],
            )
            # Don't raise - checkpoint failure shouldn't stop scraping

        except Exception as e:
            logger.error(
                "checkpoint_save_unexpected_error",
                instance_id=self.instance_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            # Don't raise

    def load(self) -> Optional[Dict]:
        """
        Load last checkpoint from DynamoDB.

        Returns:
            Checkpoint dict or None if no checkpoint exists
        """
        try:
            response = self.table.get_item(Key={"instance_id": self.instance_id})

            if "Item" not in response:
                logger.info("no_checkpoint_found", instance_id=self.instance_id)
                return None

            checkpoint = response["Item"]

            logger.info(
                "checkpoint_loaded",
                instance_id=self.instance_id,
                last_id=checkpoint.get("last_id_processed"),
                timestamp=checkpoint.get("timestamp"),
                total_processed=checkpoint.get("total_processed"),
            )

            return checkpoint

        except ClientError as e:
            logger.error(
                "checkpoint_load_failed",
                instance_id=self.instance_id,
                error=e.response["Error"]["Message"],
                error_code=e.response["Error"]["Code"],
            )
            return None

        except Exception as e:
            logger.error(
                "checkpoint_load_unexpected_error",
                instance_id=self.instance_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    def get_last_id(self) -> Optional[int]:
        """
        Get last processed ID from checkpoint.

        Returns:
            Last norm ID or None if no checkpoint
        """
        checkpoint = self.load()
        if checkpoint:
            return checkpoint.get("last_id_processed")
        return None

    def mark_completed(self, final_stats: ScraperStats) -> None:
        """
        Mark scraping job as completed.

        Args:
            final_stats: Final statistics
        """
        try:
            self.table.update_item(
                Key={"instance_id": self.instance_id},
                UpdateExpression="SET #status = :status, #completed = :completed_at",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#completed": "completed_at"
                },
                ExpressionAttributeValues={
                    ":status": "completed",
                    ":completed_at": datetime.utcnow().isoformat(),
                },
            )

            logger.info(
                "checkpoint_marked_completed",
                instance_id=self.instance_id,
                final_stats=final_stats.to_dict(),
            )

        except Exception as e:
            logger.error(
                "checkpoint_complete_failed",
                instance_id=self.instance_id,
                error=str(e),
            )

    def mark_failed(self, error_message: str) -> None:
        """
        Mark scraping job as failed.

        Args:
            error_message: Error description
        """
        try:
            self.table.update_item(
                Key={"instance_id": self.instance_id},
                UpdateExpression="SET #status = :status, #failed = :failed_at, #error = :error",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#failed": "failed_at",
                    "#error": "error_message"
                },
                ExpressionAttributeValues={
                    ":status": "failed",
                    ":failed_at": datetime.utcnow().isoformat(),
                    ":error": error_message,
                },
            )

            logger.error(
                "checkpoint_marked_failed",
                instance_id=self.instance_id,
                error=error_message,
            )

        except Exception as e:
            logger.error(
                "checkpoint_fail_update_failed",
                instance_id=self.instance_id,
                error=str(e),
            )
