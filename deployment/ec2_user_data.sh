#!/bin/bash
#
# EC2 User Data Script for BCN Scraper
#
# This script:
# 1. Installs dependencies
# 2. Clones the scraper repo
# 3. Configures environment
# 4. Installs systemd service for auto-restart
# 5. Starts scraping automatically
#
# Usage:
#   - Copy this script to EC2 User Data when launching instance
#   - Make sure instance has IAM role with:
#     * S3 full access to jurispeed-bcn-legal-docs bucket
#     * DynamoDB read/write to jurispeed-scraper-checkpoints table
#     * CloudWatch Logs write access
#   - Tag instance with "ScraperInstance" = "1" (or 2, 3, 4, 5)
#

set -e  # Exit on error

# Variables (configure these)
REPO_URL="https://github.com/YOUR_USERNAME/jurispeed-bcn-scraper.git"  # Replace with your repo
BRANCH="main"
PROJECT_DIR="/opt/jurispeed-scraper"
VENV_DIR="$PROJECT_DIR/venv"
LOG_DIR="/var/log/jurispeed-scraper"

# Environment variables (replace with actual values or use AWS Secrets Manager)
export AWS_DEFAULT_REGION="us-east-1"
export S3_BUCKET_NAME="jurispeed-bcn-legal-docs"
export CHECKPOINT_TABLE_NAME="jurispeed-scraper-checkpoints"
export KNOWLEDGE_ID="normativabcn"
export OPENSEARCH_INDEX="normativassiiv1"
export RATE_LIMIT_SECONDS="2.5"
export MAX_RETRIES="3"
export TIMEOUT_SECONDS="30"
export CHECKPOINT_EVERY="1000"
export LOG_LEVEL="INFO"
export LOG_FORMAT="json"

# Lexintel API credentials (use AWS Secrets Manager in production)
export LEXINTEL_API_URL="https://development.eba-hbpjieaz.us-west-2.elasticbeanstalk.com"
export LEXINTEL_USERNAME="YOUR_USERNAME"  # Replace
export LEXINTEL_CLIENTNAME="YOUR_CLIENT"  # Replace
export LEXINTEL_PASSWORD="YOUR_PASSWORD"  # Replace

echo "============================================"
echo "BCN Scraper EC2 Setup"
echo "============================================"

# Update system
echo "📦 Updating system packages..."
apt-get update -y
apt-get upgrade -y

# Install Python 3.11
echo "🐍 Installing Python 3.11..."
apt-get install -y python3.11 python3.11-venv python3-pip

# Install Playwright dependencies
echo "🎭 Installing Playwright system dependencies..."
apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2

# Install Git
apt-get install -y git

# Install CloudWatch Logs agent
echo "📊 Installing CloudWatch Logs agent..."
wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb -O /tmp/cloudwatch.deb
dpkg -i /tmp/cloudwatch.deb
rm /tmp/cloudwatch.deb

# Create project directory
echo "📁 Creating project directory..."
mkdir -p $PROJECT_DIR
mkdir -p $LOG_DIR
cd $PROJECT_DIR

# Clone repository
echo "📥 Cloning repository..."
if [ ! -d ".git" ]; then
    git clone -b $BRANCH $REPO_URL .
else
    git pull origin $BRANCH
fi

# Create virtual environment
echo "🔧 Creating virtual environment..."
python3.11 -m venv $VENV_DIR
source $VENV_DIR/bin/activate

# Install dependencies
echo "📦 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browsers
echo "🎭 Installing Playwright browsers..."
playwright install chromium

# Create .env file
echo "⚙️  Creating .env file..."
cat > $PROJECT_DIR/.env << EOF
AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION
S3_BUCKET_NAME=$S3_BUCKET_NAME
CHECKPOINT_TABLE_NAME=$CHECKPOINT_TABLE_NAME
KNOWLEDGE_ID=$KNOWLEDGE_ID
OPENSEARCH_INDEX=$OPENSEARCH_INDEX
RATE_LIMIT_SECONDS=$RATE_LIMIT_SECONDS
MAX_RETRIES=$MAX_RETRIES
TIMEOUT_SECONDS=$TIMEOUT_SECONDS
CHECKPOINT_EVERY=$CHECKPOINT_EVERY
LOG_LEVEL=$LOG_LEVEL
LOG_FORMAT=$LOG_FORMAT
LEXINTEL_API_URL=$LEXINTEL_API_URL
LEXINTEL_USERNAME=$LEXINTEL_USERNAME
LEXINTEL_CLIENTNAME=$LEXINTEL_CLIENTNAME
LEXINTEL_PASSWORD=$LEXINTEL_PASSWORD
EOF

# Configure CloudWatch Logs
echo "📊 Configuring CloudWatch Logs..."
cat > /opt/aws/amazon-cloudwatch-agent/etc/cloudwatch-config.json << 'EOF'
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/jurispeed-scraper/scraper.log",
            "log_group_name": "/jurispeed/bcn-scraper",
            "log_stream_name": "{instance_id}",
            "timezone": "UTC"
          }
        ]
      }
    }
  }
}
EOF

# Start CloudWatch agent
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/cloudwatch-config.json

# Create systemd service
echo "🔧 Creating systemd service..."
cat > /etc/systemd/system/jurispeed-scraper.service << EOF
[Unit]
Description=Jurispeed BCN Scraper
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$VENV_DIR/bin/python run_multi_instance.py --auto-detect
Restart=always
RestartSec=60
StandardOutput=append:$LOG_DIR/scraper.log
StandardError=append:$LOG_DIR/scraper.log

# Resource limits
MemoryMax=2G
CPUQuota=80%

[Install]
WantedBy=multi-user.target
EOF

# Enable and start service
echo "🚀 Starting scraper service..."
systemctl daemon-reload
systemctl enable jurispeed-scraper.service
systemctl start jurispeed-scraper.service

# Check status
sleep 5
systemctl status jurispeed-scraper.service

echo ""
echo "============================================"
echo "✅ Setup complete!"
echo "============================================"
echo ""
echo "Service status:"
systemctl is-active jurispeed-scraper.service && echo "  ✅ Running" || echo "  ❌ Not running"
echo ""
echo "Logs:"
echo "  tail -f $LOG_DIR/scraper.log"
echo "  journalctl -u jurispeed-scraper -f"
echo ""
echo "CloudWatch Logs:"
echo "  Group: /jurispeed/bcn-scraper"
echo "  Stream: $(ec2-metadata --instance-id | cut -d ' ' -f 2)"
echo ""
