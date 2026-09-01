# Jurispeed BCN Legal Scraper

Production scraper for Chilean legal norms from BCN (Biblioteca del Congreso Nacional). Scrapes ~411K legal documents using XML-only pipeline with semantic chunking and AWS storage.

## Overview

This scraper fetches legal documents from Chile's Congressional Library (BCN), processes them into semantically meaningful chunks, and stores them in S3 for downstream indexing. Built for production deployment across 8 EC2 instances in AWS.

**Key Features:**
- XML-only pipeline (no HTML fallback - quality over coverage)
- Semantic chunking with article-aware parsing
- Exponential backoff with retry logic
- S3 storage with immediate upload
- DynamoDB checkpoint system for resume capability

## How It Works

### 1. Scraping Pipeline

Processes legal documents from BCN in 5 stages:

1. **Fetch XML** - Downloads document from BCN's official XML endpoint (`obtxml` service)
2. **Parse Metadata** - Extracts norm type, number, dates, issuing body, and article hierarchy
3. **Parse Content** - Identifies article structure, numerals, letters, and vigencia status (in-force/repealed)
4. **Chunk Semantically** - Splits articles into 512-token chunks while preserving legal structure and context
5. **Upload to S3** - Stores complete document with metadata and chunks as JSON

**Why XML-only?** BCN's XML is schema-defined with reliable metadata (vigencia, article IDs). This gives us 98%+ vigencia coverage for free, whereas HTML scraping would require separate requests and parsing.

### 2. Indexing Pipeline

Once scraping completes, the indexing pipeline processes the stored documents:

1. **Read from S3** - Loads chunked JSON documents
2. **Generate Embeddings** - Creates vector embeddings using AWS Bedrock (Cohere Embed v4, 512 dimensions)
3. **Batch Intelligently** - Groups chunks into optimized batches (respects Bedrock limits)
4. **Index to OpenSearch** - Stores chunks with embeddings for semantic search

Both pipelines are included in this repository.

## Directory Structure

```
jurispeed-bcn-scraper/
├── src/
│   ├── core/
│   │   ├── models.py           # Pydantic data models
│   │   ├── scraper.py          # XML fetching with aiohttp
│   │   └── xml_parser.py       # BCN XML parser
│   ├── pipeline/
│   │   ├── chunker.py          # Semantic chunker (512 token target)
│   │   ├── article_parser.py   # Article substructure parser
│   │   ├── checkpoint.py       # DynamoDB checkpoint manager
│   │   └── norm_tracker.py     # Norm status tracking
│   ├── storage/
│   │   └── s3_client.py        # S3 document storage
│   └── utils/
│       └── config.py           # Configuration management
├── scripts/
│   ├── run_scraper.py          # Production scraper entry point
│   ├── retry_failed_norms.py   # Retry failed documents
│   └── create_norm_status_table.py
├── tests/
│   ├── test_scrape_to_chunks_e2e.py         # End-to-end test (33 norms)
│   ├── test_parsing_and_chunking_validation.py
│   ├── test_xml_pipeline.py
│   ├── test_xml_fetching.py
│   ├── test_smart_batching.py               # Bedrock batching
│   ├── test_scraping_throughput.py          # Throughput benchmark
│   └── test_deferred_effectiveness.py
├── CLAUDE.md                   # Project instructions for Claude Code
├── requirements.txt
├── .env.example
└── README.md
```

## Setup

### 1. Prerequisites

- Python 3.11+
- AWS credentials with access to DynamoDB and S3
- Virtual environment (recommended)

### 2. Installation

```bash
# Create and activate virtual environment
python -m venv venv
source venv/Scripts/activate  # Windows Git Bash
source venv/bin/activate       # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration

```bash
cp .env.example .env
# Edit .env with your AWS credentials
```

Required variables:
```env
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
AWS_REGION=us-west-2
S3_BUCKET_NAME=jurispeed-bcn-legal-docs
```

## Running Tests

```bash
# Activate virtual environment
source venv/Scripts/activate  # Windows
source venv/bin/activate       # Linux/Mac

# Run end-to-end test (33 diverse norms)
pytest tests/test_scrape_to_chunks_e2e.py -v

# Run XML pipeline validation
pytest tests/test_xml_pipeline.py -v

# Run all tests
pytest tests/ -v
```

## AWS Resources

### Required Resources (us-west-2)

**DynamoDB Tables:**
- `jurispeed-scraper-checkpoints` - Progress tracking per instance
- `jurispeed-norm-status` - Individual norm status for retry logic (GSI: status-index)

**S3 Bucket:**
- `jurispeed-bcn-legal-docs`
- Path: `normativabcn/originals/bcn-{norm_id}.json`

**IAM Role:**
- `jurispeed-scraper-ec2-role` - EC2 instance role with S3 + DynamoDB access

### Verify Resources

```bash
# Check DynamoDB tables
aws dynamodb describe-table --table-name jurispeed-scraper-checkpoints --region us-west-2
aws dynamodb describe-table --table-name jurispeed-norm-status --region us-west-2

# Check S3 bucket
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/ --region us-west-2

# Count scraped documents
aws s3 ls s3://jurispeed-bcn-legal-docs/normativabcn/originals/ --region us-west-2 | wc -l
```

## Output Format

Each scraped document is stored as JSON in S3 with 16 fields:

```json
{
  "norm_id": 242302,
  "norm_type": "decreto",
  "norm_number": "100",
  "formal_citation": "Decreto 100",
  "title": "Constitución Política de la República de Chile",
  "publication_date": "2005-09-22",
  "last_modified": "2024-08-29",
  "official_url": "https://bcn.cl/leychile/navegar?idNorma=242302",
  "scraped_at": "2026-08-24T12:00:00",
  "source": "xml",
  "full_content": "...",
  "total_chunks": 156,
  "total_articles": 129,
  "vigentes": 127,
  "chunks": [
    {
      "chunk_index": 0,
      "article_number": 1,
      "formal_citation": "Decreto 100, artículo 1",
      "vigente": true,
      "token_count": 487,
      "content": "...",
      "metadata": { ... }
    }
  ]
}
```

---

**License:** Internal Jurispeed project  
**Author:** Jurispeed Team - 2026
