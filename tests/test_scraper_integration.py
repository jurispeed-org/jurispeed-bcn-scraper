"""
Test scraping a single specific norm with full details.
"""
import asyncio
import sys
import json
sys.path.insert(0, 'src')

from src.scraper_playwright import BCNPlaywrightScraper
from src.config import ScraperConfig


async def test_single_norm(norm_id: int):
    """Scrape and display one norm with all details."""
    print(f"Scraping norm {norm_id} from BCN...")
    print(f"URL: https://www.bcn.cl/leychile/navegar?idNorma={norm_id}\n")

    config = ScraperConfig(
        rate_limit_seconds=1.0,
        timeout_seconds=30,
        max_retries=2,
        checkpoint_every=100,
    )

    scraper = BCNPlaywrightScraper(config)

    try:
        await scraper.start()
        print("Playwright started, fetching page...\n")

        norm = await scraper.scrape_one(norm_id)

        if not norm:
            print("FAILED: Could not scrape norm")
            return

        print("=" * 70)
        print("SCRAPING SUCCESSFUL")
        print("=" * 70)
        print()

        # Display all fields
        print(f"METADATA")
        print(f"  Norm ID:           {norm.norm_id}")
        print(f"  Type:              {norm.norm_type if isinstance(norm.norm_type, str) else norm.norm_type.value}")
        print(f"  Number:            {norm.norm_number}")
        print(f"  Title:             {norm.title}")
        print()

        print(f"DATES")
        print(f"  Publication Date:  {norm.publication_date}")
        print(f"  Promulgation Date: {norm.promulgation_date or 'N/A'}")
        print(f"  Last Modified:     {norm.last_modified or 'N/A'}")
        print()

        print(f"AUTHORITY")
        print(f"  Issuing Body:      {norm.issuing_body}")
        print(f"  Version:           {norm.version or 'N/A'}")
        print()

        print(f"SUBJECT TAGS ({len(norm.subject_tags)} tags)")
        if norm.subject_tags:
            for i, tag in enumerate(norm.subject_tags, 1):
                print(f"  {i:2}. {tag}")
        else:
            print("  (none)")
        print()

        print(f"URL")
        print(f"  {norm.official_url}")
        print()

        print(f"SUMMARY ({len(norm.summary)} chars)")
        print(f"  {norm.summary}")
        print()

        print(f"FULL CONTENT ({len(norm.full_content)} chars)")
        print(f"  Preview (first 500 chars):")
        print("  " + "-" * 68)
        preview = norm.full_content[:500].replace("\n", "\n  ")
        print(f"  {preview}")
        print("  " + "-" * 68)
        print(f"  ... ({len(norm.full_content) - 500} more characters)")
        print()

        # Save to JSON
        filename = f"scraped_detailed_{norm_id}.json"
        norm_dict = {
            "norm_id": norm.norm_id,
            "norm_type": norm.norm_type if isinstance(norm.norm_type, str) else norm.norm_type.value,
            "norm_number": norm.norm_number,
            "title": norm.title,
            "publication_date": str(norm.publication_date),
            "promulgation_date": str(norm.promulgation_date) if norm.promulgation_date else None,
            "last_modified": str(norm.last_modified) if norm.last_modified else None,
            "issuing_body": norm.issuing_body,
            "version": norm.version,
            "subject_tags": norm.subject_tags,
            "official_url": str(norm.official_url),
            "summary": norm.summary,
            "full_content": norm.full_content,
        }

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(norm_dict, f, indent=2, ensure_ascii=False)

        print(f"[SAVED] File: {filename}")

    finally:
        await scraper.close()
        print("\nDone.")


if __name__ == "__main__":
    import sys
    norm_id = int(sys.argv[1]) if len(sys.argv) > 1 else 19846
    asyncio.run(test_single_norm(norm_id))
