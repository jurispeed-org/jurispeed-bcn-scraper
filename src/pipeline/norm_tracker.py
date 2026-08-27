"""
Individual norm status tracker with DynamoDB.

Tracks status of each norm (success/failed/pending) for retry logic.
Works alongside CheckpointManager which tracks overall progress.
"""

import boto3
import structlog
from typing import Optional, List, Dict
from datetime import datetime
from botocore.exceptions import ClientError
from enum import Enum
from utils.config import AWSConfig


logger = structlog.get_logger()


class NormStatus(str, Enum):
    """Status of individual norm scraping."""
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDIDA = "refundida"  # Norm replaced by texto refundido


class NormTracker:
    """
    Tracks individual norm scraping status in DynamoDB.

    Table schema:
    - norm_id (number, partition key)
    - status (string): pending|success|failed|refundida
    - attempts (number): retry attempts count
    - source (string): xml|html
    - failure_reason (string): error message if failed
    - refundido_por (number): idNorma of the texto refundido that replaces this (only for refundida status)
    - reason (string): explanation for refundida status
    - last_attempt_at (string): ISO timestamp
    - success_at (string): ISO timestamp
    - updated_at (string): ISO timestamp
    - total_chunks (number)
    - total_articles (number)
    """

    def __init__(self, config: AWSConfig):
        self.config = config

        # Initialize DynamoDB client
        client_kwargs = {"region_name": config.region}
        if config.access_key_id and config.secret_access_key:
            client_kwargs["aws_access_key_id"] = config.access_key_id
            client_kwargs["aws_secret_access_key"] = config.secret_access_key

        self.dynamodb = boto3.resource("dynamodb", **client_kwargs)
        self.table = self.dynamodb.Table(config.norm_status_table)

        logger.info(
            "norm_tracker_initialized",
            table=config.norm_status_table,
            region=config.region
        )

    def mark_pending(self, norm_ids: List[int]):
        """Mark norms as pending (batch operation)."""
        try:
            with self.table.batch_writer() as batch:
                for norm_id in norm_ids:
                    batch.put_item(Item={
                        "norm_id": norm_id,
                        "status": NormStatus.PENDING.value,
                        "attempts": 0,
                        "created_at": datetime.utcnow().isoformat()
                    })

            logger.info("marked_pending_batch", count=len(norm_ids))

        except ClientError as e:
            logger.error(
                "mark_pending_failed",
                error=e.response["Error"]["Message"],
                count=len(norm_ids)
            )

    def mark_success(
        self,
        norm_id: int,
        source: str,
        total_chunks: Optional[int] = None,
        total_articles: Optional[int] = None
    ):
        """Mark norm as successfully scraped."""
        try:
            now = datetime.utcnow().isoformat()

            self.table.update_item(
                Key={"norm_id": norm_id},
                UpdateExpression="""
                    SET #status = :status,
                        success_at = :now,
                        last_attempt_at = :now,
                        #source = :source,
                        total_chunks = :chunks,
                        total_articles = :articles
                    REMOVE failure_reason
                """,
                ExpressionAttributeNames={
                    "#status": "status",
                    "#source": "source"
                },
                ExpressionAttributeValues={
                    ":status": NormStatus.SUCCESS.value,
                    ":now": now,
                    ":source": source,
                    ":chunks": total_chunks,
                    ":articles": total_articles
                }
            )

            logger.info(
                "marked_success",
                norm_id=norm_id,
                source=source,
                chunks=total_chunks
            )

        except ClientError as e:
            logger.error(
                "mark_success_failed",
                norm_id=norm_id,
                error=e.response["Error"]["Message"]
            )

    def mark_failed(self, norm_id: int, reason: str):
        """Mark norm as failed and increment attempts."""
        try:
            now = datetime.utcnow().isoformat()

            self.table.update_item(
                Key={"norm_id": norm_id},
                UpdateExpression="""
                    SET #status = :status,
                        last_attempt_at = :now,
                        failure_reason = :reason,
                        attempts = if_not_exists(attempts, :zero) + :one
                """,
                ExpressionAttributeNames={
                    "#status": "status"
                },
                ExpressionAttributeValues={
                    ":status": NormStatus.FAILED.value,
                    ":now": now,
                    ":reason": reason,
                    ":zero": 0,
                    ":one": 1
                }
            )

            logger.warning(
                "marked_failed",
                norm_id=norm_id,
                reason=reason[:100]  # Truncate long errors
            )

        except ClientError as e:
            logger.error(
                "mark_failed_failed",
                norm_id=norm_id,
                error=e.response["Error"]["Message"]
            )

    def mark_refundida(self, refundido_por: int, refundida_law_number: str, reason: str):
        """
        Mark that a texto refundido was detected, storing the original law number.

        Note: We DON'T know the norm_id of the original yet (only the law number).
        The cleanup script will resolve law_number -> norm_id via BCN API.

        Args:
            refundido_por: ID of the DFL/texto refundido (the one we just scraped)
            refundida_law_number: Law number of the original (e.g., "18290", "20000")
            reason: Explanation (e.g., "Detected: TEXTO REFUNDIDO en DFL 1/2007")
        """
        try:
            now = datetime.utcnow().isoformat()

            # Store as special entry with refundido_por as key
            # We'll use a prefix to distinguish from regular norms
            # Key format: "refundicion_{refundido_por}"
            special_key = f"refundicion_{refundido_por}"

            self.table.put_item(
                Item={
                    "norm_id": 999999999,  # Placeholder (GSI will use status)
                    "status": NormStatus.REFUNDIDA.value,
                    "refundido_por": refundido_por,
                    "refundida_law_number": refundida_law_number,
                    "reason": reason,
                    "detected_at": now,
                    "special_key": special_key  # For easier identification
                }
            )

            logger.info(
                "refundicion_detected_stored",
                refundido_por=refundido_por,
                refundida_law_number=refundida_law_number,
                reason=reason[:100]
            )

        except ClientError as e:
            logger.error(
                "mark_refundida_failed",
                refundido_por=refundido_por,
                refundida_law_number=refundida_law_number,
                error=e.response["Error"]["Message"]
            )

    def get_failed(self, max_attempts: int = 5) -> List[Dict]:
        """
        Get list of failed norms that can be retried.

        Returns norms where:
        - status = failed
        - attempts < max_attempts

        Note: This uses scan() which is expensive for large tables.
        For production, consider using GSI on status.
        """
        try:
            response = self.table.scan(
                FilterExpression="#status = :failed AND attempts < :max_attempts",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": NormStatus.FAILED.value,
                    ":max_attempts": max_attempts
                }
            )

            failed = []
            for item in response.get("Items", []):
                failed.append({
                    "norm_id": item["norm_id"],
                    "attempts": item.get("attempts", 0),
                    "failure_reason": item.get("failure_reason", "Unknown")
                })

            logger.info("retrieved_failed_norms", count=len(failed))
            return failed

        except ClientError as e:
            logger.error(
                "get_failed_query_error",
                error=e.response["Error"]["Message"]
            )
            return []

    def get_pending(self, limit: Optional[int] = None) -> List[int]:
        """
        Get list of pending norm IDs.

        Note: Uses scan() - expensive for large tables.
        """
        try:
            scan_kwargs = {
                "FilterExpression": "#status = :pending",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":pending": NormStatus.PENDING.value},
                "ProjectionExpression": "norm_id"
            }

            if limit:
                scan_kwargs["Limit"] = limit

            response = self.table.scan(**scan_kwargs)
            norm_ids = [item["norm_id"] for item in response.get("Items", [])]

            logger.info("retrieved_pending_norms", count=len(norm_ids))
            return sorted(norm_ids)

        except ClientError as e:
            logger.error(
                "get_pending_query_error",
                error=e.response["Error"]["Message"]
            )
            return []

    def get_all_by_status(self, status: NormStatus) -> List[Dict]:
        """
        Get all norms with specific status.

        Uses GSI status-index for efficient query (if available).
        Falls back to scan if GSI doesn't exist.

        Args:
            status: NormStatus enum value

        Returns:
            List of norm items with full attributes
        """
        try:
            # Try using GSI first (more efficient)
            try:
                response = self.table.query(
                    IndexName="status-index",
                    KeyConditionExpression="status = :status",
                    ExpressionAttributeValues={
                        ":status": status.value
                    }
                )

                items = response.get("Items", [])

                # Handle pagination
                while "LastEvaluatedKey" in response:
                    response = self.table.query(
                        IndexName="status-index",
                        KeyConditionExpression="status = :status",
                        ExpressionAttributeValues={":status": status.value},
                        ExclusiveStartKey=response["LastEvaluatedKey"]
                    )
                    items.extend(response.get("Items", []))

                logger.info("queried_by_status_gsi",
                           status=status.value,
                           count=len(items))
                return items

            except ClientError as gsi_error:
                # GSI doesn't exist, fall back to scan
                if "ResourceNotFoundException" in str(gsi_error):
                    logger.warning("status_gsi_not_found_falling_back_to_scan",
                                  status=status.value)

                    response = self.table.scan(
                        FilterExpression="#status = :status",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={":status": status.value}
                    )

                    items = response.get("Items", [])

                    # Handle pagination for scan
                    while "LastEvaluatedKey" in response:
                        response = self.table.scan(
                            FilterExpression="#status = :status",
                            ExpressionAttributeNames={"#status": "status"},
                            ExpressionAttributeValues={":status": status.value},
                            ExclusiveStartKey=response["LastEvaluatedKey"]
                        )
                        items.extend(response.get("Items", []))

                    logger.info("scanned_by_status",
                               status=status.value,
                               count=len(items))
                    return items
                else:
                    raise

        except ClientError as e:
            logger.error("get_all_by_status_failed",
                        status=status.value,
                        error=e.response["Error"]["Message"])
            return []

    def get_stats(self) -> Dict:
        """
        Get aggregate statistics.

        WARNING: Uses scan() - expensive! Only use for reporting.
        """
        try:
            # Scan entire table (expensive!)
            response = self.table.scan()
            items = response.get("Items", [])

            # Count by status
            status_counts = {
                NormStatus.PENDING.value: 0,
                NormStatus.SUCCESS.value: 0,
                NormStatus.FAILED.value: 0,
                NormStatus.REFUNDIDA.value: 0
            }

            total_chunks = 0
            total_articles = 0
            xml_count = 0
            html_count = 0
            failure_reasons = {}

            for item in items:
                status = item.get("status")
                status_counts[status] = status_counts.get(status, 0) + 1

                if status == NormStatus.SUCCESS.value:
                    chunks = item.get("total_chunks", 0)
                    articles = item.get("total_articles", 0)
                    if chunks:
                        total_chunks += chunks
                    if articles:
                        total_articles += articles

                    source = item.get("source")
                    if source == "xml":
                        xml_count += 1
                    elif source == "html":
                        html_count += 1

                elif status == NormStatus.FAILED.value:
                    reason = item.get("failure_reason", "Unknown")
                    failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

            total = len(items)
            success = status_counts[NormStatus.SUCCESS.value]

            return {
                "total": total,
                "pending": status_counts[NormStatus.PENDING.value],
                "success": success,
                "failed": status_counts[NormStatus.FAILED.value],
                "refundida": status_counts[NormStatus.REFUNDIDA.value],
                "success_rate": f"{(success / total * 100):.1f}%" if total > 0 else "0%",
                "total_chunks": total_chunks,
                "total_articles": total_articles,
                "avg_chunks": round(total_chunks / success, 1) if success > 0 else 0,
                "avg_articles": round(total_articles / success, 1) if success > 0 else 0,
                "xml_count": xml_count,
                "html_count": html_count,
                "failure_reasons": failure_reasons
            }

        except ClientError as e:
            logger.error(
                "get_stats_failed",
                error=e.response["Error"]["Message"]
            )
            return {}

    def reset_failed_to_pending(self):
        """
        Reset all failed norms back to pending for retry.

        Note: This scans and updates - expensive operation!
        """
        try:
            # Get all failed norms
            response = self.table.scan(
                FilterExpression="#status = :failed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":failed": NormStatus.FAILED.value}
            )

            failed_norms = response.get("Items", [])
            count = 0

            # Update each one
            for item in failed_norms:
                norm_id = item["norm_id"]
                self.table.update_item(
                    Key={"norm_id": norm_id},
                    UpdateExpression="""
                        SET #status = :pending
                        REMOVE failure_reason
                    """,
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":pending": NormStatus.PENDING.value}
                )
                count += 1

            logger.info("reset_failed_to_pending", count=count)
            return count

        except ClientError as e:
            logger.error(
                "reset_failed_error",
                error=e.response["Error"]["Message"]
            )
            return 0
