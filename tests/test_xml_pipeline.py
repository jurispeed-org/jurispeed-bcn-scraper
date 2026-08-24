#!/usr/bin/env python3
r"""
Test XML-based pipeline (scraping + parsing + chunking).

Verifies:
1. XML fetching from obtxml endpoint
2. XML parsing with BCNXMLParser
3. Chunking with XML data (idParte, vigencia, hierarchy)
4. Metadata generation with formal citations

Usage:
    cd jurispeed-bcn-scraper
    source venv/bin/activate  # or venv\Scripts\activate on Windows
    python tests/test_xml_pipeline.py
"""

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.scraper import BCNPlaywrightScraper
from core.xml_parser import BCNXMLParser
from pipeline.chunker import ProfessionalChunker
from utils.config import Config


async def main():
    """Test XML pipeline."""
    print("\n" + "=" * 70)
    print("TESTING XML-BASED PIPELINE")
    print("=" * 70)

    # Test norm
    norm_id = 6368  # Ley 824 (Código Tributario)

    # Initialize components
    config = Config.from_env()
    scraper = BCNPlaywrightScraper(config.scraper)
    xml_parser = BCNXMLParser()
    chunker = ProfessionalChunker(
        target_chunk_size=512,
        article_max_size=8192,
        overlap_tokens=200
    )

    try:
        # Step 1: Fetch XML
        print(f"\n[STEP 1] Fetching XML for norm {norm_id}...")
        await scraper.start()

        xml_content = await scraper._fetch_xml(norm_id)

        if not xml_content:
            print("[ERROR] XML fetch failed!")
            return 1

        print(f"[OK] XML fetched: {len(xml_content):,} chars")

        # Save raw XML
        xml_path = Path(__file__).parent.parent / f"test_ley_824_fetched.xml"
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
        print(f"[OK] Raw XML saved: {xml_path}")

        # Step 2: Parse XML
        print(f"\n[STEP 2] Parsing XML...")
        norm = xml_parser.parse(xml_content, norm_id)

        if not norm:
            print("[ERROR] XML parse failed!")
            return 1

        print(f"[OK] Norm parsed successfully")
        print(f"  - Type: {norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type}")
        print(f"  - Number: {norm.norm_number}")
        print(f"  - Formal citation: {norm.formal_citation}")
        print(f"  - Title: {norm.title[:80]}...")
        print(f"  - Content: {len(norm.full_content):,} chars")
        print(f"  - Last modified: {norm.last_modified}")

        # Step 3: Extract article hierarchy and texts
        print(f"\n[STEP 3] Extracting article hierarchy and texts...")

        hierarchy = xml_parser.extract_article_hierarchy(xml_content, norm_id)
        article_texts = xml_parser.extract_article_texts(xml_content)

        print(f"[OK] Extraction complete!")
        print(f"  - Articles in hierarchy: {len(hierarchy)}")
        print(f"  - Articles with text: {len(article_texts)}")

        # Count vigentes
        vigentes = sum(1 for info in hierarchy.values() if info.get('vigente'))
        derogados = len(hierarchy) - vigentes
        print(f"  - Vigentes: {vigentes}")
        print(f"  - Derogados: {derogados}")

        # Step 4: Chunk with XML data
        print(f"\n[STEP 4] Chunking with XML data...")

        metadata = {
            "norm_id": norm.norm_id,
            "norm_type": norm.norm_type.value if hasattr(norm.norm_type, 'value') else norm.norm_type,
            "norm_number": norm.norm_number,
            "norm_title": norm.title,
            "official_url": str(norm.official_url)
        }

        # Use XML-based chunking
        chunks = chunker.chunk(
            norm.full_content,
            metadata=metadata,
            xml_content=xml_content,
            norm_id=norm_id
        )

        print(f"[OK] Chunking complete!")
        print(f"  - Total chunks: {len(chunks)}")
        print(f"  - Avg tokens: {sum(c.token_count for c in chunks) / len(chunks):.1f}")

        # Step 5: Generate output
        print(f"\n[STEP 5] Generating output...")

        output = {
            "norm_id": norm.norm_id,
            "norm_type": metadata["norm_type"],
            "norm_number": norm.norm_number,
            "formal_citation": norm.formal_citation,
            "title": norm.title,
            "publication_date": str(norm.publication_date),
            "last_modified": str(norm.last_modified) if norm.last_modified else None,
            "official_url": str(norm.official_url),
            "summary": norm.summary,
            "subject_tags": norm.subject_tags,
            "scraped_at": datetime.now().isoformat(),
            "source": "xml",  # ✅ Mark as XML-sourced
            "total_chunks": len(chunks),
            "chunks": []
        }

        # Process each chunk
        for chunk in chunks:
            article_number = chunk.metadata.get("article_number")

            # Generate formal citation
            if article_number is not None:
                is_nested = chunk.metadata.get("is_nested", False)
                parent_article = chunk.metadata.get("parent_article")
                formal_citation = norm.get_article_citation(
                    article_number,
                    is_nested=is_nested,
                    parent_article=parent_article
                )
            else:
                formal_citation = norm.formal_citation

            # Generate URL with idParte
            part_id = chunk.metadata.get("part_id")
            article_url = norm.get_article_url(part_id) if part_id else str(norm.official_url)

            chunk_data = {
                "chunk_index": chunk.chunk_index,
                "chunk_total": chunk.chunk_total,
                "article_number": article_number,
                "formal_citation": formal_citation,
                "article_url": article_url,
                "token_count": chunk.token_count,
                "char_count": len(chunk.text),
                "content": chunk.text,
                "metadata": chunk.metadata,
                # XML-specific fields
                "vigente": chunk.metadata.get("vigente"),  # ✅ From XML!
                "fecha_version": chunk.metadata.get("fecha_version"),
                # Hierarchy
                "parent_article": chunk.metadata.get("parent_article"),
                "is_nested": chunk.metadata.get("is_nested", False),
                "hierarchy_level": chunk.metadata.get("hierarchy_level"),
                "article_label": chunk.metadata.get("article_label"),
                # Substructure
                "substructure_type": chunk.metadata.get("substructure_type"),
                "numeral": chunk.metadata.get("numeral"),
                "letra": chunk.metadata.get("letra"),
                "inciso": chunk.metadata.get("inciso")
            }

            output["chunks"].append(chunk_data)

        # Save output
        output_path = Path(__file__).parent.parent / f"test_xml_chunks_{norm_id}.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        print(f"[OK] Output saved: {output_path}")

        # Step 6: Analysis
        print(f"\n[STEP 6] Analysis")
        print("-" * 70)

        # Vigencia distribution
        chunks_vigentes = [c for c in chunks if c.metadata.get("vigente") is True]
        chunks_derogados = [c for c in chunks if c.metadata.get("vigente") is False]
        print(f"Vigentes: {len(chunks_vigentes)}/{len(chunks)} ({len(chunks_vigentes)*100/len(chunks):.1f}%)")
        print(f"Derogados: {len(chunks_derogados)}/{len(chunks)} ({len(chunks_derogados)*100/len(chunks):.1f}%)")

        # Article distribution
        articles_with_number = [c for c in chunks if c.metadata.get("article_number") is not None]
        print(f"\nChunks with article_number: {len(articles_with_number)}/{len(chunks)} ({len(articles_with_number)*100/len(chunks):.1f}%)")

        # Hierarchy distribution
        nested_chunks = [c for c in chunks if c.metadata.get("is_nested", False)]
        root_chunks = [c for c in chunks if c.metadata.get("article_number") is not None and not c.metadata.get("is_nested", False)]
        if articles_with_number:
            print(f"Nested articles (DEL ART X): {len(nested_chunks)}/{len(articles_with_number)} ({len(nested_chunks)*100/len(articles_with_number):.1f}%)")
            print(f"Root-level articles: {len(root_chunks)}/{len(articles_with_number)} ({len(root_chunks)*100/len(articles_with_number):.1f}%)")

        # Substructure distribution
        chunks_with_substructure = [c for c in chunks if c.metadata.get("substructure_type")]
        if chunks_with_substructure:
            print(f"\nSubstructure chunks: {len(chunks_with_substructure)}/{len(chunks)} ({len(chunks_with_substructure)*100/len(chunks):.1f}%)")
            numerales = [c for c in chunks_with_substructure if c.metadata.get("substructure_type") == "numeral"]
            letras = [c for c in chunks_with_substructure if c.metadata.get("substructure_type") == "letra"]
            incisos = [c for c in chunks_with_substructure if c.metadata.get("substructure_type") == "inciso"]
            print(f"  - Numerales: {len(numerales)}")
            print(f"  - Letras: {len(letras)}")
            print(f"  - Incisos: {len(incisos)}")

        # Sample chunks
        print("\n" + "-" * 70)
        print("SAMPLE CHUNKS")
        print("-" * 70)

        for i in [0, 1, 2, len(chunks)//2, -2, -1]:
            if i >= len(chunks) or i < -len(chunks):
                continue

            chunk = chunks[i]
            article_num = chunk.metadata.get("article_number")

            # Generate formal citation
            if article_num is not None:
                is_nested = chunk.metadata.get("is_nested", False)
                parent_article = chunk.metadata.get("parent_article")
                citation = norm.get_article_citation(
                    article_num,
                    is_nested=is_nested,
                    parent_article=parent_article
                )
            else:
                citation = norm.formal_citation

            print(f"\nChunk {chunk.chunk_index + 1}/{chunk.chunk_total}:")
            print(f"  Formal citation: {citation}")
            print(f"  Article number: {article_num if article_num else 'N/A'}")
            print(f"  Vigente: {chunk.metadata.get('vigente')}")  # ✅ XML vigencia!

            if chunk.metadata.get("fecha_version"):
                print(f"  Fecha version: {chunk.metadata.get('fecha_version')}")

            # Show hierarchy info
            if chunk.metadata.get("is_nested"):
                print(f"  Hierarchy: Nested under ARTICULO {chunk.metadata.get('parent_article')} (level {chunk.metadata.get('hierarchy_level')})")
                print(f"  Label: {chunk.metadata.get('article_label', 'N/A')}")
            elif chunk.metadata.get("article_number"):
                print(f"  Hierarchy: Root level")

            # Show substructure info
            if chunk.metadata.get("substructure_type"):
                subtype = chunk.metadata.get("substructure_type")
                if subtype == "numeral":
                    print(f"  Substructure: Numeral N°{chunk.metadata.get('numeral')}")
                elif subtype == "letra":
                    print(f"  Substructure: Letra {chunk.metadata.get('letra')})")
                elif subtype == "inciso":
                    print(f"  Substructure: Inciso {chunk.metadata.get('inciso')}")

            print(f"  Tokens: {chunk.token_count}")
            print(f"  Preview: {chunk.text[:150]}...")

        print("\n" + "=" * 70)
        print("TEST COMPLETE - XML PIPELINE SUCCESSFUL")
        print("=" * 70)
        print(f"\nKey advantages over HTML:")
        print(f"  [+] Vigencia FREE (no additional scraping needed)")
        print(f"  [+] idParte direct from XML attributes")
        print(f"  [+] Less fragile (official schema)")
        print(f"  [+] Cleaner parsing (no HTML noise)")
        print(f"\nOutputs saved:")
        print(f"  - XML: {xml_path}")
        print(f"  - JSON: {output_path}")

        return 0

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        await scraper.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
