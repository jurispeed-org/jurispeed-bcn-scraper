#!/usr/bin/env python3
"""
Automated EC2 launcher for BCN Scraper.

This script:
1. Creates IAM role (if doesn't exist)
2. Generates User Data script with secrets
3. Launches EC2 instance(s)
4. Waits for them to be running
5. Shows SSH commands and monitoring info

Usage:
    # Launch 1 test instance (range 1-1000)
    python launch_ec2.py --test

    # Launch all 5 production instances (full 300K)
    python launch_ec2.py --production

    # Launch specific instance number
    python launch_ec2.py --instance 1
"""

import argparse
import boto3
import time
import sys
import os
from dotenv import load_dotenv

load_dotenv()

# Configuration
REGION = os.getenv("AWS_DEFAULT_REGION", "us-west-2")
AMI_ID = "ami-05134c8ef96964280"  # Ubuntu 22.04 LTS us-west-2
INSTANCE_TYPE = "t3.small"
KEY_NAME = None  # Will prompt or auto-detect
SECURITY_GROUP = None  # Will use default VPC security group
IAM_ROLE_NAME = "jurispeed-scraper-ec2-role"

# Repository configuration
REPO_URL = ""  # Will be prompted
REPO_BRANCH = "main"

# Ranges for multi-instance
RANGES = {
    1: (1, 60000),
    2: (60001, 120000),
    3: (120001, 180000),
    4: (180001, 240000),
    5: (240001, 300000),
}

TEST_RANGE = (1, 1000)  # Small range for testing


def get_or_create_iam_role():
    """Create IAM role if doesn't exist."""
    iam = boto3.client("iam", region_name=REGION)

    try:
        # Check if role exists
        iam.get_role(RoleName=IAM_ROLE_NAME)
        print(f"✅ IAM Role already exists: {IAM_ROLE_NAME}")
        return IAM_ROLE_NAME

    except iam.exceptions.NoSuchEntityException:
        print(f"📝 Creating IAM Role: {IAM_ROLE_NAME}")

        # Create role
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }

        iam.create_role(
            RoleName=IAM_ROLE_NAME,
            AssumeRolePolicyDocument=str(trust_policy),
            Description="Role for Jurispeed BCN Scraper EC2 instances"
        )

        # Attach policies
        policies = [
            "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
        ]

        for policy_arn in policies:
            iam.attach_role_policy(
                RoleName=IAM_ROLE_NAME,
                PolicyArn=policy_arn
            )

        # Create inline policy for S3 and DynamoDB
        inline_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "S3Access",
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
                    "Resource": [
                        "arn:aws:s3:::jurispeed-bcn-legal-docs",
                        "arn:aws:s3:::jurispeed-bcn-legal-docs/*"
                    ]
                },
                {
                    "Sid": "DynamoDBAccess",
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem"
                    ],
                    "Resource": f"arn:aws:dynamodb:{REGION}:*:table/jurispeed-scraper-checkpoints"
                },
                {
                    "Sid": "EC2DescribeTags",
                    "Effect": "Allow",
                    "Action": ["ec2:DescribeTags"],
                    "Resource": "*"
                }
            ]
        }

        iam.put_role_policy(
            RoleName=IAM_ROLE_NAME,
            PolicyName="ScraperAccess",
            PolicyDocument=str(inline_policy)
        )

        # Create instance profile
        try:
            iam.create_instance_profile(
                InstanceProfileName=IAM_ROLE_NAME
            )
        except iam.exceptions.EntityAlreadyExistsException:
            pass

        iam.add_role_to_instance_profile(
            InstanceProfileName=IAM_ROLE_NAME,
            RoleName=IAM_ROLE_NAME
        )

        print(f"✅ IAM Role created: {IAM_ROLE_NAME}")
        print(f"⏳ Waiting 10s for IAM propagation...")
        time.sleep(10)

        return IAM_ROLE_NAME


def generate_user_data(instance_num: int, test_mode: bool = False):
    """Generate user data script with configuration."""

    if test_mode:
        start, end = TEST_RANGE
        instance_id = "test-ec2"
    else:
        start, end = RANGES[instance_num]
        instance_id = f"ec2-instance-{instance_num}"

    # Load credentials from .env
    s3_bucket = os.getenv("S3_BUCKET_NAME", "jurispeed-bcn-legal-docs")
    checkpoint_table = os.getenv("CHECKPOINT_TABLE_NAME", "jurispeed-scraper-checkpoints")
    lexintel_url = os.getenv("LEXINTEL_API_URL", "")
    lexintel_user = os.getenv("LEXINTEL_USERNAME", "")
    lexintel_client = os.getenv("LEXINTEL_CLIENTNAME", "")
    lexintel_pass = os.getenv("LEXINTEL_PASSWORD", "")

    user_data = f"""#!/bin/bash
set -e

# Logging
exec > >(tee /var/log/user-data.log)
exec 2>&1

echo "============================================"
echo "BCN Scraper EC2 Setup"
echo "Instance: {instance_id}"
echo "Range: {start:,} - {end:,}"
echo "============================================"

# Update system
apt-get update -y
apt-get upgrade -y

# Install Python 3.11
apt-get install -y python3.11 python3.11-venv python3-pip git

# Install Playwright dependencies
apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \\
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \\
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \\
    libcairo2 libasound2

# Create directories
mkdir -p /opt/jurispeed-scraper
mkdir -p /var/log/jurispeed-scraper
cd /opt/jurispeed-scraper

# Clone repository
git clone {REPO_URL} .

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium

# Create .env file
cat > .env << 'EOF'
AWS_DEFAULT_REGION={REGION}
S3_BUCKET_NAME={s3_bucket}
CHECKPOINT_TABLE_NAME={checkpoint_table}
KNOWLEDGE_ID=normativabcn
OPENSEARCH_INDEX=normativassiiv1
RATE_LIMIT_SECONDS=2.5
MAX_RETRIES=3
TIMEOUT_SECONDS=30
CHECKPOINT_EVERY=1000
LOG_LEVEL=INFO
LOG_FORMAT=json
LEXINTEL_API_URL={lexintel_url}
LEXINTEL_USERNAME={lexintel_user}
LEXINTEL_CLIENTNAME={lexintel_client}
LEXINTEL_PASSWORD={lexintel_pass}
EOF

# Create systemd service
cat > /etc/systemd/system/jurispeed-scraper.service << 'EOF'
[Unit]
Description=Jurispeed BCN Scraper
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/jurispeed-scraper
Environment="PATH=/opt/jurispeed-scraper/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/jurispeed-scraper/venv/bin/python run_scraper.py --start {start} --end {end} --instance-id {instance_id} --resume --s3-bucket {s3_bucket}
Restart=always
RestartSec=60
StandardOutput=append:/var/log/jurispeed-scraper/scraper.log
StandardError=append:/var/log/jurispeed-scraper/scraper.log

[Install]
WantedBy=multi-user.target
EOF

# Start service
systemctl daemon-reload
systemctl enable jurispeed-scraper.service
systemctl start jurispeed-scraper.service

echo "✅ Setup complete!"
systemctl status jurispeed-scraper.service
"""

    return user_data


def launch_instance(instance_num: int, test_mode: bool = False):
    """Launch EC2 instance."""
    ec2 = boto3.client("ec2", region_name=REGION)

    # Generate user data
    user_data = generate_user_data(instance_num, test_mode)

    # Instance name
    if test_mode:
        name = "jurispeed-scraper-test"
        tag_value = "test"
    else:
        name = f"jurispeed-scraper-{instance_num}"
        tag_value = str(instance_num)

    print(f"\n🚀 Launching EC2 instance: {name}")
    print(f"   Type: {INSTANCE_TYPE}")
    print(f"   Region: {REGION}")
    if test_mode:
        print(f"   Range: {TEST_RANGE[0]:,} - {TEST_RANGE[1]:,} (TEST MODE)")
    else:
        start, end = RANGES[instance_num]
        print(f"   Range: {start:,} - {end:,}")

    # Launch parameters
    launch_params = {
        "ImageId": AMI_ID,
        "InstanceType": INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": user_data,
        "IamInstanceProfile": {"Name": IAM_ROLE_NAME},
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": name},
                    {"Key": "Project", "Value": "Jurispeed"},
                    {"Key": "Component", "Value": "BCN-Scraper"},
                    {"Key": "ScraperInstance", "Value": tag_value},
                ]
            }
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": 20,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True
                }
            }
        ]
    }

    # Add key pair if exists
    if KEY_NAME:
        launch_params["KeyName"] = KEY_NAME

    # Launch
    response = ec2.run_instances(**launch_params)
    instance_id = response["Instances"][0]["InstanceId"]

    print(f"✅ Instance launched: {instance_id}")
    print(f"⏳ Waiting for instance to be running...")

    # Wait for running state
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    # Get instance details
    instances = ec2.describe_instances(InstanceIds=[instance_id])
    instance = instances["Reservations"][0]["Instances"][0]
    public_ip = instance.get("PublicIpAddress", "N/A")

    print(f"\n✅ Instance is running!")
    print(f"   Instance ID: {instance_id}")
    print(f"   Public IP: {public_ip}")
    print(f"\n📊 Monitoring:")
    print(f"   SSH: ssh -i YOUR_KEY.pem ubuntu@{public_ip}")
    print(f"   Logs: ssh ubuntu@{public_ip} 'tail -f /var/log/jurispeed-scraper/scraper.log'")
    print(f"   Status: ssh ubuntu@{public_ip} 'systemctl status jurispeed-scraper'")

    return instance_id, public_ip


def main():
    parser = argparse.ArgumentParser(description="Launch EC2 instances for BCN Scraper")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="Launch 1 test instance (range 1-1,000)")
    group.add_argument("--production", action="store_true", help="Launch all 5 instances (full 300K)")
    group.add_argument("--instance", type=int, choices=[1,2,3,4,5], help="Launch specific instance number")

    parser.add_argument("--repo", type=str, help="GitHub repo URL (will prompt if not provided)")
    parser.add_argument("--key", type=str, help="EC2 Key Pair name (optional)")

    args = parser.parse_args()

    # Get repo URL
    global REPO_URL, KEY_NAME
    if args.repo:
        REPO_URL = args.repo
    else:
        REPO_URL = input("Enter GitHub repo URL: ").strip()

    if not REPO_URL:
        print("❌ Error: Repository URL required")
        sys.exit(1)

    KEY_NAME = args.key

    print("\n" + "="*60)
    print("🚀 Jurispeed BCN Scraper - EC2 Launcher")
    print("="*60)

    # Create IAM role
    get_or_create_iam_role()

    # Launch instances
    if args.test:
        launch_instance(1, test_mode=True)

    elif args.production:
        print("\n⚠️  WARNING: This will launch 5 EC2 instances!")
        print(f"   Cost: ~$10 for 3 days of scraping")
        confirm = input("\nType 'yes' to continue: ")

        if confirm.lower() != "yes":
            print("❌ Aborted")
            sys.exit(0)

        for i in range(1, 6):
            launch_instance(i, test_mode=False)
            time.sleep(5)  # Stagger launches

    else:
        launch_instance(args.instance, test_mode=False)

    print("\n" + "="*60)
    print("✅ Launch complete!")
    print("\n💡 Next steps:")
    print("   1. Wait ~5 minutes for User Data script to complete")
    print("   2. SSH to instance and check logs")
    print("   3. Monitor CloudWatch Logs: aws logs tail /jurispeed/bcn-scraper --follow")
    print("   4. Check S3: aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/")
    print("="*60)


if __name__ == "__main__":
    main()
