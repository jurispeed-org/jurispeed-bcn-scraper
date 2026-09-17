#!/usr/bin/env python3
"""
Launch a single EC2 instance to scrape the mapped decretos batch (1,185 IDs).

Unlike the 8-instance range-based deployment, this batch is an explicit,
non-contiguous list of norm IDs (deployment/decretos_ids.txt), so the
instance runs `run_scraper.py --ids-file` instead of a --start/--end range.

A fresh instance is launched (rather than reusing one of the 8 stopped
range-based ones) to avoid carrying over old service definitions or partial
state from the previous HTML/Playwright-era setup.

Prerequisites:
- AWS CLI configured with credentials
- Existing IAM role: jurispeed-scraper-ec2-role
- Existing Security Group: sg-012d740b6dbc49f78
- Key pair: jurispeed-debug-key
- Repo public and pushed with latest changes (includes decretos_ids.txt)

Usage:
    python deployment/launch_decretos_instance.py
"""

import boto3
import base64
import sys
from pathlib import Path

REGION = "us-west-2"
# t3.medium (4 GB) rather than t3.small (2 GB): norms 1200724 and 1172679 each contain a
# ~121,000-token article whose chunking is slow and memory-hungry, and an OOM kill would
# restart the batch instead of finishing it. Keep MemoryMax below the instance RAM.
INSTANCE_TYPE = "t3.medium"
IAM_INSTANCE_PROFILE = "jurispeed-scraper-ec2-role"
SECURITY_GROUP_ID = "sg-012d740b6dbc49f78"
KEY_NAME = "jurispeed-debug-key"
AMI_ID = "ami-0eb3161272dc9c6eb"  # Ubuntu 22.04 LTS us-west-2

REPO_URL = "https://github.com/jurispeed-org/jurispeed-bcn-scraper.git"
BRANCH = "main"
PROJECT_DIR = "/opt/jurispeed-scraper"
# --instance-id passed to run_scraper.py, and the primary key of the DynamoDB checkpoint
# row. It must NOT be reused: the `decretos-1` row is `completed` with
# last_id_processed=999, and in --ids-file mode that number is a list INDEX, so --resume
# under that id would silently start at norm_ids[1000:] and scrape 185 of the 1,185 IDs.
# A fresh id has no row, so get_resume_point() returns None and the full list is processed;
# it also leaves the 1.9.0 batch's checkpoint intact as evidence.
INSTANCE_ID_TAG = "decretos-2"

env_file = Path(__file__).parent.parent / ".env"
if not env_file.exists():
    print(f"[ERROR] .env file not found at {env_file}")
    sys.exit(1)

with open(env_file, encoding="utf-8") as f:
    env_content = f.read()

USER_DATA = f"""#!/bin/bash
set -e

echo "============================================"
echo "BCN Scraper EC2 Setup - decretos batch"
echo "============================================"

apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

mkdir -p {PROJECT_DIR}
cd {PROJECT_DIR}

echo "Cloning repository..."
git clone -b {BRANCH} {REPO_URL} .

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Creating .env file..."
cat > .env << 'ENVEOF'
{env_content}
ENVEOF

echo "Creating systemd service..."
cat > /etc/systemd/system/jurispeed-scraper.service << 'SERVICEEOF'
[Unit]
Description=Jurispeed BCN Scraper - decretos batch
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={PROJECT_DIR}/scripts
Environment="PATH={PROJECT_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart={PROJECT_DIR}/venv/bin/python run_scraper.py --ids-file ../deployment/decretos_ids.txt --instance-id {INSTANCE_ID_TAG} --resume
Restart=on-failure
RestartSec=60
StandardOutput=append:/var/log/jurispeed-scraper.log
StandardError=append:/var/log/jurispeed-scraper.log

MemoryMax=3G
CPUQuota=100%

[Install]
WantedBy=multi-user.target
SERVICEEOF

echo "Starting scraper service..."
systemctl daemon-reload
systemctl enable jurispeed-scraper.service
systemctl start jurispeed-scraper.service

sleep 5
systemctl status jurispeed-scraper.service || true

echo "============================================"
echo "Setup complete"
echo "============================================"
"""


def launch_instance():
    ec2 = boto3.client("ec2", region_name=REGION)

    print("\nLaunching 1 EC2 instance for decretos batch (1,185 IDs)...")
    print(f"   Region: {REGION}")
    print(f"   Instance type: {INSTANCE_TYPE}")
    print(f"   IAM role: {IAM_INSTANCE_PROFILE}")
    print(f"   Security group: {SECURITY_GROUP_ID}")
    print(f"   Key pair: {KEY_NAME}")

    user_data_encoded = base64.b64encode(USER_DATA.encode()).decode()

    response = ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType=INSTANCE_TYPE,
        KeyName=KEY_NAME,
        SecurityGroupIds=[SECURITY_GROUP_ID],
        IamInstanceProfile={"Name": IAM_INSTANCE_PROFILE},
        UserData=user_data_encoded,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": "jurispeed-scraper-decretos"},
                    {"Key": "Project", "Value": "Jurispeed"},
                    {"Key": "Component", "Value": "BCN-Scraper"},
                    {"Key": "ScraperBatch", "Value": "decretos"},
                ],
            }
        ],
    )

    instance_id = response["Instances"][0]["InstanceId"]
    print(f"\n[SUCCESS] Instance launched: {instance_id}")
    print("\nNext steps:")
    print("  1. Wait ~2 min for boot + user-data to run")
    print(f"  2. Get its public IP:")
    print(f"     aws ec2 describe-instances --instance-ids {instance_id} "
          f"--query 'Reservations[0].Instances[0].PublicIpAddress' --output text --region {REGION}")
    print("  3. Tail logs:")
    print("     ssh -i jurispeed-debug-key.pem ubuntu@<IP> \"tail -f /var/log/jurispeed-scraper.log\"")

    return instance_id


if __name__ == "__main__":
    launch_instance()
