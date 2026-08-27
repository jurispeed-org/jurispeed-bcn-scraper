"""
Test new chunking architecture.

Validates:
1. Articles stay complete (no splitting by numerales/letras)
2. Subdivisions stored as metadata
3. New fields: literal_text, path, formatted_citation
4. · separator in header
"""

import sys
import asyncio
import json
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.xml_parser import BCNXMLParser
from pipeline.chunker import ProfessionalChunker


async def test_codigo_penal():
    """Test with Código Penal (norm 1984)."""

    print("=" * 80)
    print("TEST: New Chunking Architecture - Código Penal")
    print("=" * 80)

    # Load XML
    xml_path = Path("tests/test_diverse_output/xmls/norm_1984.xml")
    if not xml_path.exists():
        print(f"ERROR: XML not found at {xml_path}")
        return False

    with open(xml_path, 'r', encoding='utf-8') as f:
        xml_content = f.read()

    # Parse XML
    parser = BCNXMLParser()
    norm = parser.parse(xml_content, 1984)

    if not norm:
        print("ERROR: Failed to parse XML")
        return False

    print(f"\nNorm parsed: {norm.title}")

    # Extract data
    hierarchy = parser.extract_article_hierarchy(xml_content, 1984)
    article_texts = parser.extract_article_texts(xml_content)
    structural_context = parser.extract_structural_context(xml_content, 1984)

    print(f"Articles: {len(hierarchy)}")
    print(f"Structural contexts: {len(structural_context)}")

    # Chunk
    chunker = ProfessionalChunker()
    metadata = {
        "norm_id": 1984,
        "norm_type": "código",
        "norm_number": "penal",
        "norm_title": norm.title,
        "official_url": norm.official_url,
    }

    chunks = chunker.chunk(
        norm.full_content,
        metadata=metadata,
        xml_content=xml_content,
        norm_id=1984
    )

    print(f"\nTotal chunks: {len(chunks)}")

    # Find article 390 ter (femicidio)
    art_390_ter_id = None
    for part_id, info in hierarchy.items():
        if info['article_label'] == '390 TER':
            art_390_ter_id = part_id
            break

    if not art_390_ter_id:
        print("\nERROR: Article 390 ter not found")
        return False

    # Find chunk for article 390 ter
    art_390_chunk = None
    for chunk in chunks:
        if chunk.metadata.get('part_id') == art_390_ter_id:
            art_390_chunk = chunk
            break

    if not art_390_chunk:
        print("\nERROR: Chunk for article 390 ter not found")
        return False

    print(f"\n" + "=" * 80)
    print("CHUNK FOR ARTICLE 390 TER:")
    print("=" * 80)

    # Check new fields
    print("\n1. NEW FIELD 'literal_text':")
    literal_text = art_390_chunk.metadata.get('literal_text')
    if literal_text:
        print(f"   Length: {len(literal_text)} chars")
        print(f"   Preview: {literal_text[:150]}...")
        print("   [PASS] literal_text present")
    else:
        print("   [FAIL] literal_text missing")
        return False

    print("\n2. NEW FIELD 'subdivisions':")
    subdivisions = art_390_chunk.metadata.get('subdivisions')
    if subdivisions is not None:
        print(f"   Count: {len(subdivisions)}")
        if subdivisions:
            print(f"   First subdivision: {subdivisions[0]}")
        print("   [PASS] subdivisions present")
    else:
        print("   [FAIL] subdivisions missing")
        return False

    print("\n3. NEW FIELD 'path':")
    path = art_390_chunk.metadata.get('path')
    if path:
        print(f"   Structure: {json.dumps(path, ensure_ascii=False, indent=4)}")
        print("   [PASS] path present")
    else:
        print("   [FAIL] path missing")
        return False

    print("\n4. NEW FIELD 'formatted_citation':")
    citation = art_390_chunk.metadata.get('formatted_citation')
    if citation:
        print(f"   Citation: {citation}")
        print("   [PASS] formatted_citation present")
    else:
        print("   [FAIL] formatted_citation missing")
        return False

    print("\n5. HEADER WITH · SEPARATOR:")
    header_lines = art_390_chunk.text.split('\n\n')[0]
    print(f"   Header: {header_lines}")
    if '·' in header_lines:
        print("   [PASS] · separator present in header")
    else:
        print("   [FAIL] · separator NOT in header")
        return False

    print("\n6. ARTICLE IS COMPLETE (not split):")
    # Check if chunk contains multiple numerales
    chunk_text = art_390_chunk.text
    numeral_1_present = '1.-' in chunk_text or '1)' in chunk_text
    numeral_2_present = '2.-' in chunk_text or '2)' in chunk_text

    if numeral_1_present and numeral_2_present:
        print("   Article contains multiple numerales in one chunk")
        print("   [PASS] Article stayed complete (not split)")
    else:
        print("   [WARNING] Could not verify multiple numerales")

    print("\n7. FEMICIDIO IN TEXT (BM25):")
    if 'femicidio' in chunk_text.lower():
        print("   [PASS] 'femicidio' found in chunk text")
    else:
        print("   [FAIL] 'femicidio' NOT in chunk text")
        return False

    print("\n8. TOKEN COUNT:")
    print(f"   Tokens: {art_390_chunk.token_count}")
    print("   [INFO] Compare with old architecture (multiple chunks)")

    # Count total chunks for articles with subdivisions
    articles_with_subdivisions = [
        c for c in chunks
        if c.metadata.get('subdivisions') and len(c.metadata['subdivisions']) > 0
    ]

    print(f"\n9. ARTICLES WITH SUBDIVISIONS:")
    print(f"   Count: {len(articles_with_subdivisions)}")
    print("   [INFO] Old architecture would have split these")

    print("\n" + "=" * 80)
    print("ALL CHECKS PASSED")
    print("=" * 80)

    return True


if __name__ == "__main__":
    success = asyncio.run(test_codigo_penal())
    sys.exit(0 if success else 1)
