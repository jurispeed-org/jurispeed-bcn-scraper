"""
Test subdivision extraction for an article with numerales.
"""

import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.xml_parser import BCNXMLParser
from pipeline.chunker import ProfessionalChunker


async def test_article_with_numerales():
    """Test article 494 (has numerales)."""

    print("=" * 80)
    print("TEST: Subdivision Extraction - Article 494")
    print("=" * 80)

    xml_path = Path("tests/test_diverse_output/xmls/norm_1984.xml")
    with open(xml_path, 'r', encoding='utf-8') as f:
        xml_content = f.read()

    parser = BCNXMLParser()
    norm = parser.parse(xml_content, 1984)
    hierarchy = parser.extract_article_hierarchy(xml_content, 1984)
    article_texts = parser.extract_article_texts(xml_content)
    structural_context = parser.extract_structural_context(xml_content, 1984)

    # Find article 494
    art_494_id = None
    for part_id, info in hierarchy.items():
        if info['article_number'] == 494:
            art_494_id = part_id
            print(f"Found article 494: part_id={part_id}")
            break

    if not art_494_id:
        print("ERROR: Article 494 not found")
        return False

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

    # Find chunk for article 494
    art_494_chunk = None
    for chunk in chunks:
        if chunk.metadata.get('part_id') == art_494_id:
            art_494_chunk = chunk
            break

    if not art_494_chunk:
        print("ERROR: Chunk for article 494 not found")
        return False

    print(f"\nChunk found:")
    print(f"  Tokens: {art_494_chunk.token_count}")
    print(f"  Part ID: {art_494_chunk.metadata.get('part_id')}")

    # Check subdivisions
    subdivisions = art_494_chunk.metadata.get('subdivisions')
    print(f"\nSubdivisions:")
    if subdivisions:
        print(f"  Count: {len(subdivisions)}")
        print(f"  Data:")
        for i, sub in enumerate(subdivisions):
            print(f"    {i+1}. {json.dumps(sub, ensure_ascii=False, indent=8)}")
        print("  [PASS] Subdivisions extracted")
    else:
        print("  [WARNING] No subdivisions found")

    # Check literal_text
    literal_text = art_494_chunk.metadata.get('literal_text')
    if literal_text:
        print(f"\nLiteral text (first 500 chars):")
        print(literal_text[:500])
        print("  [PASS] literal_text present")
    else:
        print("  [FAIL] literal_text missing")
        return False

    # Verify article is complete (has both numeral 1 and subsequent numerales)
    if subdivisions and len(subdivisions) > 1:
        print(f"\n[PASS] Article has {len(subdivisions)} subdivisions and stayed complete")
    else:
        print("\n[WARNING] Expected multiple subdivisions")

    return True


if __name__ == "__main__":
    success = asyncio.run(test_article_with_numerales())
    sys.exit(0 if success else 1)
