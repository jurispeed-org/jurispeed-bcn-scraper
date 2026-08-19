#!/usr/bin/env python3
"""
Launch 8 EC2 instances for massive BCN scraping.

This script:
1. Uses existing instance i-091b91dee4898ebc0 as template
2. Creates 7 new instances (total 8)
3. Each instance gets ScraperInstance tag: "1", "2", ..., "8"
4. Uses same IAM role, security group, key pair
5. Installs updated scraper code with placeholder filter

Prerequisites:
- AWS CLI configured with credentials
- Existing IAM role: jurispeed-scraper-ec2-role
- Existing Security Group: sg-012d740b6dbc49f78
- Key pair: jurispeed-debug-key
- Git repo pushed with latest changes

Usage:
    python deployment/launch_8_instances.py
"""

import boto3
import base64
import sys
from pathlib import Path

# Configuration from existing instance
REGION = "us-west-2"
INSTANCE_TYPE = "t3.small"
IAM_INSTANCE_PROFILE = "jurispeed-scraper-ec2-role"
SECURITY_GROUP_ID = "sg-012d740b6dbc49f78"
KEY_NAME = "jurispeed-debug-key"
AMI_ID = "ami-05134c8ef96964280"  # Ubuntu 22.04 LTS us-west-2

# Scraper configuration
REPO_URL = "https://github.com/jurispeed-org/jurispeed-bcn-scraper.git"
BRANCH = "main"
PROJECT_DIR = "/opt/jurispeed-scraper"

# Read .env file
env_file = Path(__file__).parent.parent / ".env"
if not env_file.exists():
    print(f"❌ Error: .env file not found at {env_file}")
    sys.exit(1)

with open(env_file) as f:
    env_content = f.read()

# User data script
USER_DATA = f"""#!/bin/bash
set -e

echo "============================================"
echo "BCN Scraper EC2 Setup - Instance ${{INSTANCE_NUM}}"
echo "============================================"

# Update system
echo "📦 Updating system packages..."
apt-get update -y
apt-get upgrade -y

# Install Python 3.12
echo "🐍 Installing Python 3.12..."
apt-get install -y python3.12 python3.12-venv python3-pip git

# Install Playwright dependencies
echo "🎭 Installing Playwright system dependencies..."
apt-get install -y \\
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \\
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \\
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \\
    libgbm1 libpango-1.0-0 libcairo2 libasound2

# Create project directory
echo "📁 Creating project directory..."
mkdir -p {PROJECT_DIR}
cd {PROJECT_DIR}

# Clone repository
echo "📥 Cloning repository..."
git clone -b {BRANCH} {REPO_URL} .

# Create virtual environment
echo "🔧 Creating virtual environment..."
python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
echo "📦 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browsers
echo "🎭 Installing Playwright browsers..."
playwright install chromium

# Create .env file
echo "⚙️  Creating .env file..."
cat > .env << 'ENVEOF'
{env_content}
ENVEOF

# Create systemd service
echo "🔧 Creating systemd service..."
cat > /etc/systemd/system/jurispeed-scraper.service << 'SERVICEEOF'
[Unit]
Description=Jurispeed BCN Scraper
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={PROJECT_DIR}/scripts
Environment="PATH={PROJECT_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart={PROJECT_DIR}/venv/bin/python run_multi_instance.py --auto-detect
Restart=always
RestartSec=60
StandardOutput=append:/var/log/jurispeed-scraper.log
StandardError=append:/var/log/jurispeed-scraper.log

# Resource limits
MemoryMax=2G
CPUQuota=80%

[Install]
WantedBy=multi-user.target
SERVICEEOF

# Enable and start service
echo "🚀 Starting scraper service..."
systemctl daemon-reload
systemctl enable jurispeed-scraper.service
systemctl start jurispeed-scraper.service

# Check status
sleep 5
systemctl status jurispeed-scraper.service || true

echo ""
echo "============================================"
echo "✅ Setup complete! Instance ${{INSTANCE_NUM}}"
echo "============================================"
"""


def launch_instances(num_instances: int = 7):
    """
    Launch new EC2 instances.

    Note: Existing instance i-091b91dee4898ebc0 will be re-tagged as instance #1

    Args:
        num_instances: Number of NEW instances to launch (default 7)
    """
    ec2 = boto3.client("ec2", region_name=REGION)

    print(f"\n🚀 Launching {num_instances} new EC2 instances...")
    print(f"   Region: {REGION}")
    print(f"   Instance type: {INSTANCE_TYPE}")
    print(f"   IAM role: {IAM_INSTANCE_PROFILE}")
    print(f"   Security group: {SECURITY_GROUP_ID}")
    print(f"   Key pair: {KEY_NAME}")
    print()

    launched_instances = []

    for i in range(2, num_instances + 2):  # Start from 2 (1 is existing instance)
        print(f"[{i}/{num_instances+1}] Launching instance #{i}...")

        # Prepare user data with instance number
        user_data_with_num = USER_DATA.replace("${INSTANCE_NUM}", str(i))
        user_data_encoded = base64.b64encode(user_data_with_num.encode()).decode()

        # Launch instance
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
                        {"Key": "Name", "Value": f"jurispeed-scraper-{i}"},
                        {"Key": "Project", "Value": "Jurispeed"},
                        {"Key": "Component", "Value": "BCN-Scraper"},
                        {"Key": "ScraperInstance", "Value": str(i)},
                    ],
                }
            ],
        )

        instance_id = response["Instances"][0]["InstanceId"]
        launched_instances.append((i, instance_id))
        print(f"   ✅ Instance #{i}: {instance_id}")

    print("\n" + "="*60)
    print("✅ All instances launched!")
    print("="*60)
    print("\nInstance mapping:")
    print("  #1: i-091b91dee4898ebc0 (existing - needs re-tag)")
    for num, instance_id in launched_instances:
        print(f"  #{num}: {instance_id}")

    print("\n⚠️  NEXT STEPS:")
    print("1. Re-tag existing instance to ScraperInstance=1:")
    print("   aws ec2 create-tags --resources i-091b91dee4898ebc0 --tags Key=ScraperInstance,Value=1 --region us-west-2")
    print("\n2. Update code on existing instance:")
    print("   ssh -i jurispeed-debug-key.pem ubuntu@35.167.31.99")
    print("   cd /opt/jurispeed-scraper && git pull")
    print("   systemctl restart jurispeed-scraper")
    print("\n3. Wait ~5 minutes for new instances to initialize")
    print("\n4. Check logs:")
    print("   ssh -i jurispeed-debug-key.pem ubuntu@<IP>")
    print("   tail -f /var/log/jurispeed-scraper.log")
    print("   journalctl -u jurispeed-scraper -f")

    return launched_instances


def retag_existing_instance():
    """Re-tag existing instance as #1."""
    ec2 = boto3.client("ec2", region_name=REGION)

    print("\n📝 Re-tagging existing instance...")
    ec2.create_tags(
        Resources=["i-091b91dee4898ebc0"],
        Tags=[
            {"Key": "ScraperInstance", "Value": "1"},
            {"Key": "Name", "Value": "jurispeed-scraper-1"},
        ]
    )
    print("   ✅ Instance i-091b91dee4898ebc0 tagged as #1")


def main():
    """Main entry point."""
    print("\n" + "="*60)
    print("🚀 JURISPEED BCN SCRAPER - LAUNCH 8 INSTANCES")
    print("="*60)

    # Confirm
    print("\n⚠️  This will:")
    print("  - Launch 7 NEW t3.small instances")
    print("  - Re-tag existing instance i-091b91dee4898ebc0 as #1")
    print("  - Cost: ~$50 for 25 days")
    print()

    response = input("Continue? (yes/no): ")
    if response.lower() != "yes":
        print("❌ Aborted")
        sys.exit(0)

    # Check repo URL
    if "YOUR_USERNAME" in REPO_URL:
        print("\n❌ Error: Update REPO_URL in this script first!")
        print(f"   Current: {REPO_URL}")
        sys.exit(1)

    # Re-tag existing instance
    retag_existing_instance()

    # Launch new instances
    launched_instances = launch_instances(num_instances=7)

    print("\n✅ Deployment complete!")


if __name__ == "__main__":
    main()
