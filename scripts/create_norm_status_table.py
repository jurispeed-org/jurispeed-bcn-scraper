#!/usr/bin/env python3
"""
Create DynamoDB table for individual norm status tracking.

Table: jurispeed-norm-status
Purpose: Track FAILED norms for retry logic

Production strategy:
- Only failed norms are registered in DynamoDB
- Successful norms are in S3 (source of truth)
- Keeps costs low (~6% of norms fail = ~25K writes vs 411K)
- Simple retry logic: query failed norms and retry them
"""

import boto3
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import Config
from botocore.exceptions import ClientError


def create_norm_status_table(config):
    """
    Create DynamoDB table for norm status tracking.

    Schema:
    - norm_id (Number, Partition Key)
    - status (String): pending|success|failed
    - attempts (Number): retry count
    - source (String): xml|html
    - failure_reason (String)
    - last_attempt_at (String): ISO timestamp
    - success_at (String): ISO timestamp
    - total_chunks (Number)
    - total_articles (Number)
    - created_at (String): ISO timestamp

    GSI: status-index for fast queries on status
    """
    # Initialize DynamoDB client
    client_kwargs = {"region_name": config.aws.region}
    if config.aws.access_key_id and config.aws.secret_access_key:
        client_kwargs["aws_access_key_id"] = config.aws.access_key_id
        client_kwargs["aws_secret_access_key"] = config.aws.secret_access_key

    dynamodb = boto3.client("dynamodb", **client_kwargs)

    table_name = config.aws.norm_status_table

    print(f"\n{'='*70}")
    print(f"Creating DynamoDB table: {table_name}")
    print(f"Region: {config.aws.region}")
    print(f"{'='*70}\n")

    try:
        # Create table
        response = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {
                    "AttributeName": "norm_id",
                    "KeyType": "HASH"  # Partition key
                }
            ],
            AttributeDefinitions=[
                {
                    "AttributeName": "norm_id",
                    "AttributeType": "N"  # Number
                },
                {
                    "AttributeName": "status",
                    "AttributeType": "S"  # String (for GSI)
                }
            ],
            # Global Secondary Index for fast status queries
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {
                            "AttributeName": "status",
                            "KeyType": "HASH"
                        }
                    ],
                    "Projection": {
                        "ProjectionType": "ALL"  # Include all attributes
                    },
                    "ProvisionedThroughput": {
                        "ReadCapacityUnits": 5,
                        "WriteCapacityUnits": 5
                    }
                }
            ],
            # On-Demand billing (pay per request)
            BillingMode="PROVISIONED",
            ProvisionedThroughput={
                "ReadCapacityUnits": 5,
                "WriteCapacityUnits": 5
            },
            # Tags
            Tags=[
                {"Key": "Project", "Value": "Jurispeed"},
                {"Key": "Purpose", "Value": "NormStatusTracking"},
                {"Key": "Environment", "Value": "Production"}
            ]
        )

        print(f"[OK] Table creation initiated successfully")
        print(f"\nTable ARN: {response['TableDescription']['TableArn']}")
        print(f"Table Status: {response['TableDescription']['TableStatus']}")
        print(f"\nWaiting for table to become active...")

        # Wait for table to be created
        waiter = dynamodb.get_waiter('table_exists')
        waiter.wait(
            TableName=table_name,
            WaiterConfig={'Delay': 5, 'MaxAttempts': 20}
        )

        print(f"[OK] Table is now active and ready to use")

        # Get final table description
        desc = dynamodb.describe_table(TableName=table_name)
        table_info = desc['Table']

        print(f"\n{'='*70}")
        print("TABLE DETAILS")
        print(f"{'='*70}")
        print(f"Name: {table_info['TableName']}")
        print(f"Status: {table_info['TableStatus']}")
        print(f"Item Count: {table_info['ItemCount']}")
        print(f"Size: {table_info['TableSizeBytes']} bytes")
        print(f"Billing Mode: {table_info.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')}")

        if 'ProvisionedThroughput' in table_info:
            pt = table_info['ProvisionedThroughput']
            print(f"\nProvisioned Throughput:")
            print(f"  Read Capacity Units: {pt['ReadCapacityUnits']}")
            print(f"  Write Capacity Units: {pt['WriteCapacityUnits']}")

        print(f"\nGlobal Secondary Indexes:")
        for gsi in table_info.get('GlobalSecondaryIndexes', []):
            print(f"  - {gsi['IndexName']}")
            print(f"    Status: {gsi['IndexStatus']}")
            print(f"    Keys: {gsi['KeySchema']}")

        print(f"\n{'='*70}")
        print("ESTIMATED COSTS (us-east-1)")
        print(f"{'='*70}")
        print(f"Provisioned capacity (5 RCU + 5 WCU):")
        print(f"  Table: ~$2.92/month")
        print(f"  GSI: ~$2.92/month")
        print(f"  Total: ~$5.84/month")
        print(f"\nStorage: $0.25 per GB-month")
        print(f"  Estimated for 100k norms: ~$0.05/month")
        print(f"\n[WARNING]  Consider switching to On-Demand billing if usage is sporadic")

        print(f"\n{'='*70}")
        print("NEXT STEPS")
        print(f"{'='*70}")
        print(f"1. Update .env with table name (already configured):")
        print(f"   NORM_STATUS_TABLE={table_name}")
        print(f"2. Use NormTracker in your scraping scripts")
        print(f"3. Monitor costs in AWS Console > DynamoDB > {table_name}")
        print(f"\n[OK] Setup complete!\n")

        return True

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']

        if error_code == 'ResourceInUseException':
            print(f"[ERROR] Table already exists: {table_name}")
            print(f"\nTo view existing table:")
            print(f"  aws dynamodb describe-table --table-name {table_name}")
            print(f"\nTo delete and recreate:")
            print(f"  aws dynamodb delete-table --table-name {table_name}")
            print(f"  python scripts/create_norm_status_table.py")
            return False

        else:
            print(f"[ERROR] Error creating table: {error_code}")
            print(f"  Message: {error_msg}")
            return False

    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Create norm status table."""
    config = Config.from_env()

    print("\nDynamoDB Norm Status Table Setup")
    print("=" * 70)

    success = create_norm_status_table(config)

    if success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
