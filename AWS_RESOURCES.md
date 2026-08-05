# 📦 AWS Resources - BCN Scraper

**Date Created:** August 5, 2026  
**Region:** us-west-2  
**Account:** 241789448945

---

## ✅ Resources Created

### 1. DynamoDB Table: `jurispeed-scraper-checkpoints`

**Purpose:** Store scraping progress for resume functionality

**Configuration:**
- **Table Name:** `jurispeed-scraper-checkpoints`
- **Region:** us-west-2
- **Primary Key:** `instance_id` (String)
- **Billing Mode:** Pay-per-request (on-demand)
- **Status:** ✅ ACTIVE
- **ARN:** `arn:aws:dynamodb:us-west-2:241789448945:table/jurispeed-scraper-checkpoints`

**Schema:**
```json
{
  "instance_id": "ec2-instance-1",           // Partition key
  "last_id_processed": 5000,                 // Last norm ID scraped
  "total_processed": 5000,
  "success_count": 4800,
  "failed_count": 100,
  "skipped_count": 100,
  "retry_count": 150,
  "timestamp": "2026-08-05T12:00:00.000Z",
  "status": "running",                       // running | completed | failed
  "metadata": {}                             // Optional extra data
}
```

**Usage Patterns:**
- **Write:** Every 1,000 documents (checkpoint frequency)
- **Read:** Once at scraper start (resume from last checkpoint)
- **Cost:** ~$0.001 per checkpoint = ~$0.30 for 300K docs

**Access:**
```bash
# Query checkpoint
aws dynamodb get-item \
  --table-name jurispeed-scraper-checkpoints \
  --key '{"instance_id": {"S": "ec2-instance-1"}}' \
  --region us-west-2

# List all checkpoints
aws dynamodb scan \
  --table-name jurispeed-scraper-checkpoints \
  --region us-west-2
```

---

### 2. S3 Bucket: `jurispeed-bcn-legal-docs`

**Purpose:** Store scraped legal documents (raw JSON)

**Configuration:**
- **Bucket Name:** `jurispeed-bcn-legal-docs`
- **Region:** us-west-2
- **Versioning:** ✅ Enabled
- **Encryption:** Default (SSE-S3)
- **Public Access:** ❌ Blocked
- **Status:** ✅ ACTIVE

**Path Structure:**
```
s3://jurispeed-bcn-legal-docs/
└── normativabcn/                    # Knowledge base ID
    └── originals/                   # Document type
        ├── bcn-1.json               # Doc ID format: bcn-{norm_id}
        ├── bcn-2.json
        ├── bcn-19846.json
        └── ... (300,000 files total)
```

**Document Format (Example: bcn-19846.json):**
```json
{
  "norm_id": 19846,
  "norm_type": "decreto",
  "norm_number": "37",
  "title": "Decreto 37: AUTORIZA CIRCULACION DE VEHICULO...",
  "publication_date": "1996-02-08",
  "promulgation_date": "1996-01-30",
  "last_modified": null,
  "issuing_body": "MINISTERIO DEL INTERIOR",
  "version": null,
  "subject_tags": ["Circulacion", "Vehiculos", "Provincia"],
  "official_url": "https://bcn.cl/leychile/navegar?idNorma=19846",
  "summary": "AUTORIZA CIRCULACION DE VEHICULO EN TERMINOS QUE SEÑALA",
  "full_content": "Artículo 1°. ...",
  "scraped_at": "2026-08-05T14:30:00Z"
}
```

**Storage Estimates:**
- **Files:** 300,000 documents
- **Average size:** 15KB per file
- **Total size:** ~4.5GB
- **Monthly cost:** ~$0.10 (Standard storage)

**Access:**
```bash
# List all documents
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/

# Count total files
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/ \
  --recursive --summarize | grep "Total Objects"

# Download specific document
aws s3 cp s3://jurispeed-bcn-legal-docs/normativabcn/originals/bcn-19846.json .

# Upload test document
aws s3 cp test.json s3://jurispeed-bcn-legal-docs/normativabcn/originals/bcn-test.json
```

---

## 🔐 IAM Permissions Required

### For Scraper (EC2/Local)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3WriteAccess",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::jurispeed-bcn-legal-docs/*"
    },
    {
      "Sid": "DynamoDBCheckpoints",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": "arn:aws:dynamodb:us-west-2:241789448945:table/jurispeed-scraper-checkpoints"
    }
  ]
}
```

### For Indexer (Pipeline)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ReadAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::jurispeed-bcn-legal-docs",
        "arn:aws:s3:::jurispeed-bcn-legal-docs/*"
      ]
    },
    {
      "Sid": "BedrockEmbeddings",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": "arn:aws:bedrock:us-west-2::foundation-model/cohere.embed-multilingual-v3"
    }
  ]
}
```

---

## 💰 Cost Breakdown

### Monthly Costs (Steady State - After Scraping)

| Resource | Usage | Unit Cost | Monthly Cost |
|----------|-------|-----------|--------------|
| **DynamoDB** | 0 reads (scraping done) | $0 | $0.00 |
| **S3 Storage** | 4.5GB Standard | $0.023/GB | $0.10 |
| **S3 Requests** | 0 (no active scraping) | $0 | $0.00 |
| **Total** | | | **$0.10/month** |

### One-Time Scraping Costs (3-4 days)

| Resource | Usage | Unit Cost | Total Cost |
|----------|-------|-----------|------------|
| **EC2** | 5× t3.small × 72h | $0.0208/h | $7.49 |
| **S3 PUT** | 300K writes | $0.005/1K | $1.50 |
| **DynamoDB Write** | 300 checkpoints | $1.25/M | $0.00 |
| **Data Transfer** | 15GB out to S3 | $0.09/GB | $1.35 |
| **Total** | | | **~$10.34** |

**Note:** Scraping is a one-time cost. After completion, only S3 storage remains (~$0.10/month).

---

## 🔍 Monitoring & Verification

### Check Resource Status

```bash
# DynamoDB table status
aws dynamodb describe-table \
  --table-name jurispeed-scraper-checkpoints \
  --region us-west-2 \
  --query 'Table.{Name:TableName,Status:TableStatus,Items:ItemCount}'

# S3 bucket exists
aws s3api head-bucket --bucket jurispeed-bcn-legal-docs

# Count S3 objects
aws s3api list-objects-v2 \
  --bucket jurispeed-bcn-legal-docs \
  --prefix normativabcn/originals/ \
  --query 'length(Contents)'
```

### View Recent Checkpoints

```bash
# Get latest checkpoint for instance 1
aws dynamodb get-item \
  --table-name jurispeed-scraper-checkpoints \
  --key '{"instance_id": {"S": "ec2-instance-1"}}' \
  --region us-west-2 \
  --output json | jq '.Item'

# Scan all checkpoints (see progress of all instances)
aws dynamodb scan \
  --table-name jurispeed-scraper-checkpoints \
  --region us-west-2 \
  --output json | jq '.Items[] | {instance: .instance_id.S, last_id: .last_id_processed.N, status: .status.S}'
```

### View Recent S3 Uploads

```bash
# List last 10 uploaded documents
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/ \
  --recursive | tail -10

# Get document sample
aws s3 cp s3://jurispeed-bcn-legal-docs/normativabcn/originals/bcn-1.json - | jq .
```

---

## 🛡️ Backup & Disaster Recovery

### DynamoDB Backups

**Automatic:** Not configured (not needed - checkpoints are ephemeral)

**Manual backup if needed:**
```bash
aws dynamodb create-backup \
  --table-name jurispeed-scraper-checkpoints \
  --backup-name scraper-checkpoints-backup-$(date +%Y%m%d) \
  --region us-west-2
```

### S3 Versioning

**Status:** ✅ Enabled

**Restore previous version:**
```bash
# List versions
aws s3api list-object-versions \
  --bucket jurispeed-bcn-legal-docs \
  --prefix normativabcn/originals/bcn-19846.json

# Restore specific version
aws s3api get-object \
  --bucket jurispeed-bcn-legal-docs \
  --key normativabcn/originals/bcn-19846.json \
  --version-id <VERSION_ID> \
  restored-bcn-19846.json
```

---

## 🗑️ Cleanup (If Needed)

### Delete Resources

```bash
# WARNING: This will delete all scraped data!

# 1. Empty S3 bucket first
aws s3 rm s3://jurispeed-bcn-legal-docs/normativabcn/ --recursive

# 2. Delete S3 bucket
aws s3 rb s3://jurispeed-bcn-legal-docs --force

# 3. Delete DynamoDB table
aws dynamodb delete-table \
  --table-name jurispeed-scraper-checkpoints \
  --region us-west-2
```

---

## 📞 Support

**AWS Account:** 241789448945  
**IAM User:** jurispeed-backend-app  
**Project:** Jurispeed BCN Scraper  
**Contact:** Jurispeed Team

---

**Last Updated:** August 5, 2026
