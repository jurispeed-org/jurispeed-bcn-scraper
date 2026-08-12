#!/usr/bin/env python3
"""
Setup AWS resources for BCN scraper.

Creates:
- DynamoDB table for checkpoints
- S3 bucket for legal documents

Usage:
    python setup_aws_resources.py --create-all
    python setup_aws_resources.py --create-dynamodb
    python setup_aws_resources.py --create-s3
"""

import argparse
import sys
import os
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()


def create_dynamodb_table(table_name: str, region: str):
    """
    Create DynamoDB table for checkpoints.

    Args:
        table_name: Table name
        region: AWS region
    """
    dynamodb = boto3.client("dynamodb", region_name=region)

    print(f"📦 Creating DynamoDB table: {table_name}")

    try:
        response = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "instance_id", "KeyType": "HASH"},  # Partition key
            ],
            AttributeDefinitions=[
                {"AttributeName": "instance_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",  # On-demand pricing
            Tags=[
                {"Key": "Project", "Value": "Jurispeed"},
                {"Key": "Component", "Value": "BCN-Scraper"},
                {"Key": "Purpose", "Value": "Checkpoint-Storage"},
            ],
        )

        print(f"✅ Table created successfully")
        print(f"   ARN: {response['TableDescription']['TableArn']}")
        print(f"   Status: {response['TableDescription']['TableStatus']}")
        print(f"   Billing: Pay-per-request (no provisioned capacity)")
        print()

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "ResourceInUseException":
            print(f"ℹ️  Table already exists: {table_name}")
        else:
            print(f"❌ Error creating table: {e.response['Error']['Message']}")
            sys.exit(1)


def create_s3_bucket(bucket_name: str, region: str):
    """
    Create S3 bucket for legal documents.

    Args:
        bucket_name: Bucket name
        region: AWS region
    """
    s3 = boto3.client("s3", region_name=region)

    print(f"🪣 Creating S3 bucket: {bucket_name}")

    try:
        # Create bucket (location constraint not needed for us-east-1)
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )

        print(f"✅ Bucket created successfully")
        print(f"   Region: {region}")
        print()

        # Enable versioning (recommended for production)
        print(f"🔄 Enabling versioning...")
        s3.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )
        print(f"✅ Versioning enabled")
        print()

        # Add lifecycle policy (transition to Glacier after 90 days)
        print(f"📅 Adding lifecycle policy...")
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket_name,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "Id": "archive-old-docs",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Transitions": [
                            {
                                "Days": 90,
                                "StorageClass": "GLACIER_IR",  # Instant Retrieval
                            }
                        ],
                    }
                ]
            },
        )
        print(f"✅ Lifecycle policy added (Glacier after 90 days)")
        print()

        # Add tags
        s3.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={
                "TagSet": [
                    {"Key": "Project", "Value": "Jurispeed"},
                    {"Key": "Component", "Value": "BCN-Scraper"},
                    {"Key": "Purpose", "Value": "Legal-Documents-Storage"},
                ]
            },
        )
        print(f"✅ Tags added")
        print()

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "BucketAlreadyOwnedByYou":
            print(f"ℹ️  Bucket already exists: {bucket_name}")
        elif error_code == "BucketAlreadyExists":
            print(f"❌ Bucket name taken by another AWS account: {bucket_name}")
            print(f"   Try a different name (must be globally unique)")
            sys.exit(1)
        else:
            print(f"❌ Error creating bucket: {e.response['Error']['Message']}")
            sys.exit(1)


def verify_resources(table_name: str, bucket_name: str, region: str):
    """
    Verify resources exist and are accessible.

    Args:
        table_name: DynamoDB table name
        bucket_name: S3 bucket name
        region: AWS region
    """
    print("🔍 Verifying resources...")
    print()

    # Check DynamoDB
    dynamodb = boto3.client("dynamodb", region_name=region)
    try:
        response = dynamodb.describe_table(TableName=table_name)
        status = response["Table"]["TableStatus"]
        print(f"✅ DynamoDB table: {table_name}")
        print(f"   Status: {status}")
        print(f"   Item count: {response['Table']['ItemCount']}")
        print()
    except ClientError:
        print(f"❌ DynamoDB table not found: {table_name}")
        print()

    # Check S3
    s3 = boto3.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"✅ S3 bucket: {bucket_name}")

        # Get object count
        try:
            response = s3.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
            if "Contents" in response:
                # Get approximate count
                paginator = s3.get_paginator("list_objects_v2")
                count = 0
                for page in paginator.paginate(Bucket=bucket_name):
                    count += len(page.get("Contents", []))
                    if count > 1000:  # Stop after 1K for performance
                        print(f"   Objects: 1,000+ (stopped counting)")
                        break
                else:
                    print(f"   Objects: {count:,}")
            else:
                print(f"   Objects: 0 (empty)")
        except Exception:
            print(f"   Objects: Unknown")

        print()

    except ClientError:
        print(f"❌ S3 bucket not found: {bucket_name}")
        print()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Setup AWS resources for BCN scraper")

    parser.add_argument(
        "--create-all",
        action="store_true",
        help="Create all resources (DynamoDB + S3)",
    )
    parser.add_argument(
        "--create-dynamodb",
        action="store_true",
        help="Create DynamoDB table only",
    )
    parser.add_argument(
        "--create-s3",
        action="store_true",
        help="Create S3 bucket only",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify existing resources",
    )

    args = parser.parse_args()

    # Load config from env
    table_name = os.getenv("CHECKPOINT_TABLE_NAME", "jurispeed-scraper-checkpoints")
    bucket_name = os.getenv("S3_BUCKET_NAME", "jurispeed-bcn-legal-docs")
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

    print("🚀 AWS Resource Setup")
    print("=" * 60)
    print(f"Table: {table_name}")
    print(f"Bucket: {bucket_name}")
    print(f"Region: {region}")
    print("=" * 60)
    print()

    # Verify credentials
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        print(f"✅ AWS credentials valid")
        print(f"   Account: {identity['Account']}")
        print(f"   User: {identity['Arn']}")
        print()
    except Exception as e:
        print(f"❌ AWS credentials invalid: {e}")
        sys.exit(1)

    # Create resources
    if args.create_all or args.create_dynamodb:
        create_dynamodb_table(table_name, region)

    if args.create_all or args.create_s3:
        create_s3_bucket(bucket_name, region)

    if args.verify or args.create_all:
        verify_resources(table_name, bucket_name, region)

    if not any([args.create_all, args.create_dynamodb, args.create_s3, args.verify]):
        parser.print_help()
        sys.exit(1)

    print("✅ Done!")


if __name__ == "__main__":
    main()
