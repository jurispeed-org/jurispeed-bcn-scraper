"""
Test for deferred effectiveness (future fechaVersion) detection.

Validates that articles with future fechaVersion are correctly marked as:
- vigente=False
- vigencia_status="diferida"
"""

import asyncio
import sys
from pathlib import Path

# Add src and scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from utils.config import Config
from run_scraper import ProductionScraper


# Test norm with future effectiveness date
# DL 3.500 has fechaVersion=2027-04-01 (verified in ANALISIS_PUNTOS_CIEGOS_BCN.md)
TEST_NORM_ID = 7147  # DL 3.500


async def test_deferred_effectiveness_detection():
    """
    Test that articles with future fechaVersion are marked as deferred effectiveness.

    Expected behavior:
    - Scraper fetches DL 3.500 (idNorma=7147)
    - XML parser extracts fechaVersion=2027-04-01 (future date)
    - Chunker detects future date and sets:
      - vigente=False
      - vigencia_status="diferida"
    """
    # Load config
    config = Config.from_env()

    # Initialize production scraper
    scraper = ProductionScraper(
        config=config,
        instance_id="test-deferred",
        s3_bucket="test-bucket",
        skip_ids=set()
    )

    try:
        # Start scraper
        await scraper.scraper.start()

        # Scrape norm with future vigencia
        norm = await scraper.scraper.scrape_one(TEST_NORM_ID)

        assert norm is not None, f"Failed to scrape norm {TEST_NORM_ID}"

        # Fetch XML for processing
        xml_content = await scraper.scraper._fetch_xml(TEST_NORM_ID, timeout=30)
        assert xml_content is not None, f"Failed to fetch XML for norm {TEST_NORM_ID}"

        # Process with chunking
        data = await scraper.process_norm_data(norm, xml_content)

        assert data is not None, "process_norm_data returned None"
        assert "chunks" in data, "No chunks in output"
        assert len(data["chunks"]) > 0, "No chunks generated"

        # Check that at least some chunks have deferred effectiveness
        chunks_with_future_effectiveness = [
            c for c in data["chunks"]
            if c["metadata"].get("vigencia_status") == "diferida"
        ]

        assert len(chunks_with_future_effectiveness) > 0, (
            "No chunks with vigencia_status='diferida' found. "
            f"Total chunks: {len(data['chunks'])}"
        )

        # Verify that deferred chunks have vigente=False
        for chunk in chunks_with_future_effectiveness:
            assert chunk["metadata"].get("vigente") is False, (
                f"Chunk with vigencia_status='diferida' has vigente={chunk['metadata'].get('vigente')} "
                f"(expected False). Chunk: {chunk['metadata'].get('article_number')}"
            )

        print(f"\nSUCCESS: Detected {len(chunks_with_future_effectiveness)} chunks with deferred effectiveness")
        print(f"Total chunks: {len(data['chunks'])}")
        print(f"Percentage with future effectiveness: {len(chunks_with_future_effectiveness)/len(data['chunks'])*100:.1f}%")

        # Show examples
        print("\nExample chunks with deferred effectiveness:")
        for chunk in chunks_with_future_effectiveness[:3]:
            print(f"  - Article {chunk['metadata'].get('article_number')}: "
                  f"fechaVersion={chunk['metadata'].get('fecha_version')}, "
                  f"vigente={chunk['metadata'].get('vigente')}, "
                  f"status={chunk['metadata'].get('vigencia_status')}")

    finally:
        await scraper.scraper.close()


async def test_is_future_version_function():
    """Test the is_future_version() utility function directly."""
    from core.xml_parser import BCNXMLParser
    from datetime import date, timedelta

    # Future date
    future_date = (date.today() + timedelta(days=365)).isoformat()
    assert BCNXMLParser.is_future_version(future_date) is True

    # Past date
    past_date = (date.today() - timedelta(days=365)).isoformat()
    assert BCNXMLParser.is_future_version(past_date) is False

    # Today
    today = date.today().isoformat()
    assert BCNXMLParser.is_future_version(today) is False

    # Invalid date
    assert BCNXMLParser.is_future_version("invalid-date") is False

    # None
    assert BCNXMLParser.is_future_version(None) is False

    # Sentinel date (far future)
    assert BCNXMLParser.is_future_version("2222-02-02") is True

    # Known future case from DL 3.500
    assert BCNXMLParser.is_future_version("2027-04-01") is True

    print("\nSUCCESS: is_future_version() works correctly")


if __name__ == "__main__":
    print("Testing deferred effectiveness detection...")
    print("=" * 80)

    # Run utility function test
    asyncio.run(test_is_future_version_function())

    print("\n" + "=" * 80)
    print("Testing full pipeline with DL 3.500...")
    print("=" * 80)

    # Run full pipeline test
    asyncio.run(test_deferred_effectiveness_detection())

    print("\n" + "=" * 80)
    print("ALL TESTS PASSED")
