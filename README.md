# 📜 Jurispeed BCN Legal Scraper

Production-ready scraper for Chilean legal norms from BCN (Biblioteca del Congreso Nacional).

## 🎯 Features

- ✅ **Playwright-based scraping** - Handles JavaScript SPA rendering
- ✅ **Checkpoint system** - Resume from last processed ID (DynamoDB)
- ✅ **S3 storage** - Immediate upload of scraped documents
- ✅ **Multi-instance support** - Divide 300K docs across 5 EC2 instances
- ✅ **Retry logic** - 3 attempts with exponential backoff
- ✅ **Rate limiting** - 2.5s delay to avoid bans
- ✅ **CloudWatch Logs** - Production monitoring
- ✅ **Structured logging** - JSON format for parsing

### Indexing (Professional Pipeline)
- ✅ **Semantic chunking** (by meaning, not arbitrary bytes)
- ✅ **Token-aware** (512 tokens optimal for Cohere v4)
- ✅ **Context preservation** (50 token overlap)
- ✅ **Article detection** (legal text specific)
- ✅ **Direct Bedrock integration** (Cohere Embed v4, 512 dims)
- ✅ **Bulk OpenSearch indexing** (batch operations)
- ✅ **S3 document storage** (originals backup)

### Quality
- ✅ **Zero legacy code dependencies**
- ✅ **Structured logging** (JSON format for CloudWatch)
- ✅ **Production-ready** (type hints, error handling)
- ✅ **Following RAG best practices**

## Architecture

```
jurispeed-bcn-scraper/
├── src/
│   ├── __init__.py
│   ├── models.py              # Pydantic models (validation)
│   ├── parser.py              # HTML parser
│   ├── scraper.py             # Async scraper (httpx + tenacity)
│   ├── chunker.py             # ⭐ Professional semantic chunker
│   ├── embedder.py            # ⭐ Bedrock Cohere v4 client
│   ├── opensearch_client.py   # ⭐ Direct OpenSearch indexing
│   ├── s3_client.py           # ⭐ S3 document storage
│   ├── indexer.py             # ⭐ Pipeline orchestrator
│   ├── checkpoint.py          # DynamoDB checkpoint manager
│   ├── config.py              # Configuration management
│   └── cli.py                 # CLI interface
├── tests/
│   ├── test_parser.py
│   ├── test_chunker.py
│   └── test_scraper.py
├── pyproject.toml
├── .env.example
└── README.md
```

**⭐ = Professional pipeline (zero legacy dependencies)**

## 📊 Scraping Stats

- **Total documents:** 300,000 legal norms
- **Time estimate:** 3-4 days with 5 EC2 instances
- **Speed:** ~21,600 docs/day per instance
- **Storage:** ~15GB in S3 (JSON format)
- **Cost:** ~$18 total (EC2 + S3)

## 🚀 Quick Start

### 1. Install Dependencies

```bash
# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your AWS credentials
```

Required variables:
- `AWS_ACCESS_KEY_ID` - AWS access key
- `AWS_SECRET_ACCESS_KEY` - AWS secret key
- `S3_BUCKET_NAME` - S3 bucket for documents
- `CHECKPOINT_TABLE_NAME` - DynamoDB table for checkpoints

### 3. Create AWS Resources

**Resources needed:**
- ✅ DynamoDB table: `jurispeed-scraper-checkpoints` (CREATED ✅)
- ✅ S3 bucket: `jurispeed-bcn-legal-docs` (CREATED ✅)

**Status:** Both resources are ready in region `us-west-2`

**To verify:**
```bash
python setup_aws_resources.py --verify
```

**Resource details:**
```
DynamoDB Table: jurispeed-scraper-checkpoints
├── Region: us-west-2
├── Billing: Pay-per-request (no provisioned capacity)
├── Primary Key: instance_id (String)
└── Status: ACTIVE ✅

S3 Bucket: jurispeed-bcn-legal-docs
├── Region: us-west-2
├── Versioning: Enabled
├── Path structure: {knowledge_id}/originals/{doc_id}.json
└── Status: ACTIVE ✅
```

## Usage

### Dry Run (scrape without uploading)

```bash
python -m src.cli \
  --instance-id test-local \
  --range-start 1 \
  --range-end 100 \
  --dry-run
```

### Production Run

```bash
python -m src.cli \
  --instance-id scraper-1 \
  --range-start 1 \
  --range-end 60000
```

### Resume from Checkpoint

```bash
python -m src.cli \
  --instance-id scraper-1 \
  --range-start 1 \
  --range-end 60000 \
  --resume
```

## Multi-Instance Deployment (EC2)

Deploy 5 instances in parallel:

```bash
# Instance 1
python -m src.cli --instance-id scraper-1 --range-start 1 --range-end 60000

# Instance 2
python -m src.cli --instance-id scraper-2 --range-start 60001 --range-end 120000

# Instance 3
python -m src.cli --instance-id scraper-3 --range-start 120001 --range-end 180000

# Instance 4
python -m src.cli --instance-id scraper-4 --range-start 180001 --range-end 240000

# Instance 5
python -m src.cli --instance-id scraper-5 --range-start 240001 --range-end 300000
```

**Expected throughput:** ~900 docs/hour per instance = 4,500 docs/hour total

## Configuration

Edit `.env` file:

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_ACCESS_KEY_ID` | AWS credentials | - |
| `CHECKPOINT_TABLE_NAME` | DynamoDB table for checkpoints | `jurispeed-scraper-checkpoints` |
| `LEXINTEL_API_URL` | Lexintel backend URL | - |
| `KNOWLEDGE_ID` | Target knowledge base | `normativabcn` |
| `RATE_LIMIT_SECONDS` | Delay between requests | `2.5` |
| `CHECKPOINT_EVERY` | Save checkpoint every N docs | `1000` |

## Data Model

### Extracted Fields (10 + 2 critical)

```python
{
  "norm_id": 12345,                          # BCN ID
  "norm_type": "ley",                        # ley, codigo, dfl, decreto, reglamento
  "norm_number": "19846",
  "title": "LEY NUM. 19.846 SUBSIDIO HABITACIONAL",
  "publication_date": "2003-01-04",
  "promulgation_date": "2002-12-27",
  "last_modified": "2024-05-15",
  "issuing_body": "MINISTERIO DE VIVIENDA Y URBANISMO",
  "version": "2025-09-29",
  "subject_tags": ["vivienda", "subsidios"],
  "official_url": "https://bcn.cl/leychile/navegar?idNorma=12345",
  
  # ⭐ Critical fields for RAG
  "summary": "Establece normas sobre subsidio habitacional...",
  "full_content": "Artículo 1°. El presente decreto..."
}
```

## 🧪 Testing

### Run Tests

```bash
# Run parser tests
pytest tests/test_parser.py -v

# Run integration test (scrapes 3 real docs)
pytest tests/test_scraper_integration.py -v
```

### Verify AWS Resources

```bash
# Check DynamoDB table and S3 bucket
python setup_aws_resources.py --verify

# Expected output:
# ✅ DynamoDB table: jurispeed-scraper-checkpoints (Status: ACTIVE)
# ✅ S3 bucket: jurispeed-bcn-legal-docs (accessible)
```

### Test Scraper Locally (100 docs)

```bash
# Test with small range
python run_scraper.py \
  --start 1 \
  --end 100 \
  --instance-id local-test \
  --s3-bucket jurispeed-bcn-legal-docs

# Check S3 after completion
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/ | wc -l
# Should show ~100 files
```

## Monitoring

Logs are JSON-formatted for CloudWatch:

```json
{
  "event": "norm_parsed",
  "norm_id": 12345,
  "norm_type": "ley",
  "timestamp": "2026-08-04T12:00:00Z"
}
```

Query in CloudWatch Insights:
```
fields @timestamp, norm_id, norm_type, event
| filter event = "norm_parsed"
| stats count() by norm_type
```

## Troubleshooting

### Script stops mid-run

**Solution:** Use `--resume` flag to continue from last checkpoint.

### Too many 404s

**Cause:** BCN ID range has gaps (not all IDs exist).
**Expected:** ~10-15% 404 rate is normal.

### Rate limiting / IP ban

**Cause:** Scraping too fast.
**Solution:** Increase `RATE_LIMIT_SECONDS` in `.env`.

## Architecture Decisions

### Why async + rate limiting?

- Async allows efficient I/O without blocking
- Rate limiting prevents IP bans
- Balance: fast enough (~900 docs/h) but respectful

### Why Pydantic?

- Automatic validation (catches bad data early)
- Type safety (mypy can verify)
- Easy serialization (`.dict()`, `.json()`)
- Self-documenting (JSON Schema generation)

### Why checkpoint every 1000 docs?

- Balance between safety and DynamoDB costs
- ~$0.001 per checkpoint write
- 300K docs = 300 checkpoints = ~$0.30 total

## Cost Estimation (7 days, 5 EC2 instances)

| Resource | Cost |
|----------|------|
| 5× EC2 t4g.small | ~$141 |
| DynamoDB checkpoints | ~$0.30 |
| S3 storage (50GB) | ~$1.15 |
| Data transfer | ~$20 |
| **Total** | **~$162** |

## 📦 AWS Resources Created

### ✅ Resources Status (as of 2026-08-05)

| Resource | Name | Region | Status | Details |
|----------|------|--------|--------|---------|
| **DynamoDB** | `jurispeed-scraper-checkpoints` | us-west-2 | ✅ ACTIVE | Pay-per-request billing |
| **S3 Bucket** | `jurispeed-bcn-legal-docs` | us-west-2 | ✅ ACTIVE | Versioning enabled |

### DynamoDB Table Schema

```
Table: jurispeed-scraper-checkpoints
├── Primary Key: instance_id (String)
├── Attributes:
│   ├── instance_id: "ec2-instance-1"
│   ├── last_id_processed: 5000
│   ├── total_processed: 5000
│   ├── success_count: 4800
│   ├── failed_count: 100
│   ├── skipped_count: 100
│   ├── retry_count: 150
│   ├── timestamp: "2026-08-05T12:00:00Z"
│   └── status: "running" | "completed" | "failed"
├── Billing: Pay-per-request (no capacity planning)
└── Estimated cost: ~$0.30 for 300K checkpoints
```

### S3 Bucket Structure

```
s3://jurispeed-bcn-legal-docs/
└── normativabcn/
    └── originals/
        ├── bcn-1.json
        ├── bcn-2.json
        ├── bcn-19846.json
        └── ... (300,000 total)
```

**Each JSON file contains:**
- All 12 extracted fields (norm_id, type, title, dates, etc.)
- Full legal content
- Metadata (instance_id, source)
- Average size: ~15KB per file

### Verification Commands

```bash
# Check DynamoDB table
aws dynamodb describe-table \
  --table-name jurispeed-scraper-checkpoints \
  --region us-west-2

# Check S3 bucket
aws s3 ls s3://jurispeed-bcn-legal-docs/

# Count documents in S3
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/ \
  --recursive --summarize | grep "Total Objects"
```

---

## 📝 Next Steps

After scraping completes (3-4 days):

1. ✅ **Verify S3 storage** (~300K files in bucket)
2. ⏳ **Run indexing pipeline** (Feature 1.2 - coming next)
3. ⏳ **Update MCP server** with new search tools
4. ⏳ **Test search** in Claude Desktop

---

## 📄 License

Internal Jurispeed project - Not for public distribution.

## 👤 Author

Jurispeed Team - 2026
