"""
Version of the XML -> chunks transformation.

This single constant is stamped into every stored document as `parser_version`, so a
document in S3 can always be traced back to the code that produced it. Without it,
reprocessing a corpus of 411K documents leaves no way to tell which ones already carry
a given fix.

Bump PARSER_VERSION whenever the pipeline can produce different output for the same
input XML. That covers the parser (src/core/xml_parser.py), the chunker
(src/pipeline/chunker.py) and the assembly step (ProductionScraper.process_norm_data).
Do NOT bump it for changes that cannot alter output (logging, comments, tests).

History:
    1.0.0 - Baseline: XML-only pipeline with truncation repair, binary tag repair and
            structural chunking, as validated against norm 242302 (Constitucion).
    1.1.0 - PR2: extract_norm_vigencia() reads the `derogado` attribute off the <Norma>
            root and understands BCN's "derogado"/"no derogado" values. Norm-level
            in_force can now be False; 4 of 1,178 audited documents change.
    1.2.0 - PR3: <Encabezado> and <Promulgacion> enter hierarchy/article_texts as
            synthetic parts (header_{id}, promulgation_{id}) and are chunked. Every
            chunk gains `content_type`. Existing part_ids, chunk bodies and
            total_articles are unchanged; documents gain up to 2 chunks.
    1.3.0 - PR4: the XML chunking route is selected from the real XML structure
            (hierarchy + article_texts + at least one non-synthetic part) instead of
            counting "Articulo N" matches in the text. _detect_articles() is unchanged
            and still governs the fallback tree. 378 of 1,184 audited documents move
            from the semantic fallback to the XML route, gaining structural metadata.
            _add_overlap() now refuses structured chunks, which also stops a
            pre-existing corruption on the non-articulated branch.
    1.4.0 - PR5: truncation is observable. The predicate the fetch-time repair path
            already used moved to BCNXMLParser.is_part_truncated() (single source of
            truth, rules unchanged), every hierarchy entry gains `is_truncated`, and the
            chunker copies it into chunk metadata. Extracted text, part_ids, chunk
            bodies, token counts and chunk order are unchanged: metadata only.
            `is_repaired` is deliberately absent -- it is not determinable from the
            post-repair XML. See docs/KNOWN_BUGS.md.
    1.5.0 - PR6: margin-note stripping is observable. _strip_margin_notes() became a
            wrapper over strip_margin_notes_with_stats(), which runs the identical
            algorithm and reports notes_detected / lines_affected / chars_removed /
            words_removed / by_reason. Every hierarchy entry and every chunk gains
            `margin_notes`. The heuristic, its patterns and the extracted text are
            unchanged: metadata only.
    1.6.0 - PR7: annex blocks stop overwriting each other. In
            _split_annexes_by_treaty_article() the synthetic key `{part_id}_art{N}` was not
            unique: an annex preamble fell back to index "1" and was replaced by the real
            article 1, and an annex holding two treaties collided on its repeated numbering.
            Both silently dropped text. Keys are now unique and a preamble is no longer
            labelled as an article. Only annexes with one of those two shapes change;
            documents without annexes (including the golden 242302) are byte-identical.
            `_extract_full_content()` still omits <Anexo> and <Promulgacion>: see
            docs/KNOWN_BUGS.md.
          ( PR9 is deliberately absent from this list: it split the chunker's routing input
            (`chunking_text`) from the stored `full_content` and could not alter output for
            any input. Verified byte-identical, golden included, so it was not bumped. )
    1.7.0 - PR10: <Promulgacion> enters `full_content`. _extract_full_content() now appends
            the promulgation text obtained from PR3's _extract_synthetic_parts() -- same
            binary removal, same margin-note stripping, same `\n\n` separator, appended last
            in reading order. STORED FIELD ONLY: the chunker receives `chunking_text`, which
            is unchanged, so chunks, token_count, part_id, hierarchy, article_texts,
            is_truncated, margin_notes and routing are all byte-identical (measured on
            242302, 215179, 252869; the golden diff is exactly full_content_chars +
            full_content_sha256 plus this bump). 1,176 of 1,184 audited documents gain text,
            a median 2.4% of the field, mean +326 chars. <Anexo> is still omitted: see
            docs/KNOWN_BUGS.md.
    1.8.0 - PR11: <Anexo> enters `full_content`, completing the field's contract to
            Encabezado + EstructuraFuncional + Anexo + Promulgacion. The annex loop
            extract_article_texts() already ran moved into _extract_annex_parts() and both
            call it, so the stored text is byte-identical to the `anexo_{idParte}` entries of
            article_texts and cannot drift from them. The annexes are inserted BEFORE the
            promulgation, which closes the document, matching the reading order PR3
            documented and both extraction functions build.
            STORED FIELD ONLY, same as PR10: chunks, chunk content, token_count, part_id,
            article_number, article_label, formatted_citation, content_type, hierarchy,
            article_texts, is_truncated, margin_notes and the route are unchanged (measured
            on 242302, 215179, 252869 and the 9 real annex documents on disk).
            Scale: 314 of 1,178 parsed audit documents gain text, an estimated median 84.6%
            of the resulting field, growth factor median 6.5x and max 202x (norm 198321,
            measured live: 7.448 -> 1.348.797 chars). 242302 does NOT change, so the golden
            is silent about this PR by construction -- 252869 is the evidence.
    1.9.0 - PR12: a split annex keeps its POSITION. _split_annexes_by_treaty_article() used
            to delete `anexo_{idParte}` and assign the sub-keys, and a new dict key lands at
            the end, so the sub-parts jumped behind promulgation_{id} and _chunk_by_xml()
            emitted the closing formula in the middle of the treaty (chunk 2 of 12 on norm
            252869). The split now runs in two passes and rebuilds both dicts in the original
            key order, expanding each split annex where it already was.
            POSITION ONLY: the multiset of (part_id, content_sha256, token_count,
            content_type, article_label, formatted_citation), the full chunk metadata, the
            text per part_id and total_chunks are IDENTICAL before and after, measured
            through process_norm_data() on 252869, 1008095, 133150, 252870, 214367, 1216451,
            198321, 242302 and 215179. Only chunk_index changes, and only where an annex
            actually splits: 62 of 85 annex documents on disk, an estimated ~229 of the 314
            in the audit. Boundaries, block text, keys, the PR7 de-duplication and the order
            of the sub-parts among themselves are untouched -- including the known
            "Articulo 17 before Articulo 1" false positive, deliberately left alone.
            242302 does NOT change again, so the golden moves only by this version string.
"""

PARSER_VERSION = "1.9.0"
