#!/usr/bin/env python3
"""
END-TO-END test for bug fixes using production code.

Validates fixes from PLAN_CORRECCION_BUGS.md by scraping real norms from BCN
and processing them through ProductionScraper.process_norm_data().

Tests critical bugs: ordinal detection, citations, subdivisions, metadata, norm_types.
"""

import asyncio
import json
import sys
import re
from pathlib import Path
from datetime import datetime

# Add src AND scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from run_scraper import ProductionScraper
from utils.config import Config


# Test norms selected specifically to validate critical bugs
CRITICAL_TEST_NORMS = {
    1984: "Código Penal - numerals with º, citations",
    242302: "Constitución - Art 19 subdivision splitting",
    1165383: "Orden 2870 - non-articulated metadata",
    30667: "Ley 19.300 - letras detection",
}


async def test_production_bug_fixes():
    """
    Test bug fixes using PRODUCTION code path.

    Uses ProductionScraper.process_norm_data() to ensure we test the EXACT
    same code that runs in production EC2 instances.
    """
    print("\n" + "=" * 80)
    print("PRODUCTION BUG FIXES VALIDATION - END-TO-END TEST")
    print("=" * 80)
    print(f"Test time: {datetime.now().isoformat()}")
    print(f"Testing {len(CRITICAL_TEST_NORMS)} critical norms")
    print("=" * 80)

    # Initialize production scraper (EXACT production config)
    config = Config.from_env()
    scraper = ProductionScraper(
        config=config,
        instance_id="test-bug-fixes",
        s3_bucket="test-bucket"  # Won't upload, just process
    )

    results = {
        "total": len(CRITICAL_TEST_NORMS),
        "passed": 0,
        "failed": 0,
        "tests": []
    }

    for norm_id, description in CRITICAL_TEST_NORMS.items():
        print(f"\n{'-' * 80}")
        print(f"Testing norm {norm_id}: {description}")
        print(f"{'-' * 80}")

        test_result = await test_single_norm(norm_id, description, scraper)
        results["tests"].append(test_result)

        if test_result["all_checks_passed"]:
            results["passed"] += 1
            print(f"[OK] PASSED: {norm_id}")
        else:
            results["failed"] += 1
            print(f"[FAIL] FAILED: {norm_id}")
            for check in test_result["checks"]:
                if not check["passed"]:
                    print(f"  - {check['name']}: {check['error']}")

    # Print summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"Total norms tested: {results['total']}")
    print(f"Passed: {results['passed']}")
    print(f"Failed: {results['failed']}")

    if results['failed'] == 0:
        print("\n[SUCCESS] ALL PRODUCTION TESTS PASSED!")
        print("Bug fixes are working correctly in production code.")
    else:
        print(f"\n[WARNING] {results['failed']} norms failed validation")
        print("Some bug fixes may not be working in production code.")
        return False

    # Save detailed results
    output_dir = Path(__file__).parent / "test_bug_fixes_output"
    output_dir.mkdir(exist_ok=True)

    results_file = output_dir / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nDetailed results saved to: {results_file}")

    return results['failed'] == 0


async def test_single_norm(norm_id: int, description: str, scraper: ProductionScraper) -> dict:
    """
    Test one norm using PRODUCTION code path.

    This is the CRITICAL part: we use scraper.process_norm_data() which is
    the EXACT method that runs in production.
    """
    result = {
        "norm_id": norm_id,
        "description": description,
        "all_checks_passed": False,
        "checks": []
    }

    try:
        # Step 1: Scrape using PRODUCTION code
        print(f"  Scraping norm {norm_id}...")
        norm = await scraper.scraper.scrape_one(norm_id)

        if not norm:
            result["checks"].append({
                "name": "Scraping",
                "passed": False,
                "error": "Failed to scrape norm"
            })
            return result

        # Step 2: Fetch XML using PRODUCTION code
        xml_content = await scraper.scraper._fetch_xml(norm_id, timeout=30)

        if not xml_content:
            result["checks"].append({
                "name": "XML Fetch",
                "passed": False,
                "error": "Failed to fetch XML"
            })
            return result

        # Step 3: Process using PRODUCTION code (THIS IS THE CRITICAL STEP)
        print(f"  Processing through production pipeline...")
        data = await scraper.process_norm_data(norm, xml_content)

        # Now validate bug fixes in the OUTPUT
        checks = []

        # Ordinal º detection (Código Penal)
        if norm_id == 1984:
            checks.extend(check_codigo_penal_numerals(data))

        # Clean citations
        checks.extend(check_clean_citations(data, norm_id))

        # Unique citations per subdivision (Constitución Art 19)
        if norm_id == 242302:
            checks.extend(check_art19_subdivisions(data))

        # Non-articulated metadata (Orden 2870)
        if norm_id == 1165383:
            checks.extend(check_non_articulated_metadata(data))

        # Correct norm_type (not all "ley")
        checks.extend(check_norm_type(data, norm_id))

        # No word splits
        checks.extend(check_no_word_splits(data))

        result["checks"] = checks
        result["all_checks_passed"] = all(c["passed"] for c in checks)

        # Print check results
        for check in checks:
            status = "[OK]" if check["passed"] else "[FAIL]"
            print(f"  {status} {check['name']}")
            if not check["passed"]:
                print(f"      Error: {check['error']}")

    except Exception as e:
        result["checks"].append({
            "name": "Processing",
            "passed": False,
            "error": f"Exception: {str(e)}"
        })

    return result


def check_codigo_penal_numerals(data: dict) -> list:
    """Verify numerals with º are detected in Código Penal."""
    checks = []

    # Articles 233, 442, 446, 477 should have subdivisions detected
    critical_articles = ["233", "442", "446", "477"]

    for article_num in critical_articles:
        article_chunks = [
            c for c in data["chunks"]
            if c["metadata"].get("article_label") == article_num
        ]

        if not article_chunks:
            checks.append({
                "name": f"Art {article_num} exists",
                "passed": False,
                "error": f"Article {article_num} not found in chunks"
            })
            continue

        # Subdivisions are no longer stored as metadata (removed field). Detecting
        # them below SUBDIVISION_SPLIT_THRESHOLD does not force a chunk split, so
        # check for recognized numeral markers directly in the chunk content instead.
        numeral_pattern = re.compile(r'\d+[ºo°]\.-?\s|\d+\.[ºo°]\s|\d+\.-\s')
        has_subdivisions = any(
            numeral_pattern.search(c["content"])
            for c in article_chunks
        )

        checks.append({
            "name": f"Art {article_num} numerals detected",
            "passed": has_subdivisions,
            "error": None if has_subdivisions else f"No subdivisions detected in Art {article_num}"
        })

    return checks


def check_clean_citations(data: dict, norm_id: int) -> list:
    """Verify citations are clean (not 'Código N° PENAL')."""
    checks = []

    if norm_id == 1984:  # Código Penal
        # Should be "Código Penal", NOT "Código N° PENAL"
        bad_pattern = "Código N° PENAL"
        good_pattern = "Código Penal"

        for chunk in data["chunks"][:10]:  # Check first 10 chunks
            citation = chunk["metadata"].get("formatted_citation", "")

            if bad_pattern in citation:
                checks.append({
                    "name": "Clean citation",
                    "passed": False,
                    "error": f"Found bad citation: {citation}"
                })
                break
        else:
            # Check that good pattern exists
            has_good = any(
                good_pattern in c["metadata"].get("formatted_citation", "")
                for c in data["chunks"][:10]
            )
            checks.append({
                "name": "Clean citation",
                "passed": has_good,
                "error": None if has_good else "Good citation pattern not found"
            })

    return checks


def check_art19_subdivisions(data: dict) -> list:
    """Verify Art 19 is split and has unique citations."""
    checks = []

    art19_chunks = [
        c for c in data["chunks"]
        if c["metadata"].get("article_label") == "19"
    ]

    # Art 19 should be split into multiple chunks (not 1 giant chunk)
    checks.append({
        "name": "Art 19 split into chunks",
        "passed": len(art19_chunks) >= 5,  # Should have many chunks
        "error": f"Only {len(art19_chunks)} chunks for Art 19, expected 5+"
            if len(art19_chunks) < 5 else None
    })

    # Each chunk should have unique formatted_citation
    if art19_chunks:
        citations = [c["metadata"].get("formatted_citation", "") for c in art19_chunks]
        unique_citations = set(citations)

        checks.append({
            "name": "Unique citations per subdivision",
            "passed": len(unique_citations) == len(citations),
            "error": f"Only {len(unique_citations)} unique citations for {len(citations)} chunks"
                if len(unique_citations) != len(citations) else None
        })

    return checks


def check_non_articulated_metadata(data: dict) -> list:
    """Verify non-articulated norms have complete metadata."""
    checks = []

    required_fields = ["in_force", "force_status", "formatted_citation"]

    if data["chunks"]:
        first_chunk_meta = data["chunks"][0]["metadata"]

        for field in required_fields:
            has_field = field in first_chunk_meta
            checks.append({
                "name": f"Field '{field}' present",
                "passed": has_field,
                "error": f"Missing field: {field}" if not has_field else None
            })

    return checks


def check_norm_type(data: dict, norm_id: int) -> list:
    """Verify norm_type is correct."""
    checks = []

    expected_types = {
        1984: "codigo",
        242302: "decreto",
        1165383: "orden",
        30667: "ley",
    }

    if norm_id in expected_types:
        actual_type = data.get("norm_type", "")
        expected_type = expected_types[norm_id]

        checks.append({
            "name": f"norm_type is '{expected_type}'",
            "passed": actual_type == expected_type,
            "error": f"Expected '{expected_type}', got '{actual_type}'"
                if actual_type != expected_type else None
        })

    return checks


def check_no_word_splits(data: dict) -> list:
    """Verify chunks don't split words (no 'e I.V.P.')."""
    checks = []

    bad_patterns = [
        r'^[a-z]\s',  # Starts with lowercase letter + space
        r'^e\s+I\.V\.P\.',  # Specific bad case from bug report
    ]

    word_split_found = False
    word_split_example = None

    for chunk in data["chunks"]:
        content = chunk.get("content", "")

        for pattern in bad_patterns:
            if re.match(pattern, content):
                word_split_found = True
                word_split_example = content[:50]
                break

        if word_split_found:
            break

    checks.append({
        "name": "No word splits",
        "passed": not word_split_found,
        "error": f"Found word split: '{word_split_example}...'" if word_split_found else None
    })

    return checks


if __name__ == "__main__":
    result = asyncio.run(test_production_bug_fixes())
    sys.exit(0 if result else 1)
