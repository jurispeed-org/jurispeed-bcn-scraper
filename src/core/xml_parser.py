"""
Parser for BCN XML format.

Parses the official XML schema from BCN's obtxml service.
Schema: EsquemaIntercambioNorma-v1-0.xsd
"""

import html
import xml.etree.ElementTree as ET
import re
from typing import Dict, List, Optional, Tuple
from datetime import date
import structlog
from core.models import ChileanLegalNorm, NormType

logger = structlog.get_logger()


class BCNXMLParser:
    """
    Parser for BCN's official XML format.

    Extracts norm metadata and article structure from XML.
    Much cleaner than HTML parsing.
    """

    # XML namespace
    NS = {'ns': 'http://www.leychile.cl/esquemas'}

    # tipoParte values treated as articles (Disposición Transitoria are
    # transitory provisions, structurally equivalent to regular articles)
    ARTICLE_TIPO_PARTE = {'Artículo', 'Disposición Transitoria'}

    # BCN's plain-text export lays out the article body and its margin notes
    # (e.g. "CPR Art. 19° N° 24", "D.O. 24.10.1980") side by side in fixed-width
    # columns on the same line. A run of 4+ spaces after real content marks the
    # start of the margin-note column; strip it so it doesn't get embedded mid-word
    # (lookbehind requires a non-space char right before the gap, so it never
    # touches a line's own leading indentation).
    _MARGIN_NOTE_PATTERN = re.compile(r'(?<=\S) {4,}\S.*$', re.MULTILINE)

    # When a margin note wraps onto its own physical line, or BCN pushes it to a
    # line of its own, there is no real content before the gap -- just the note's
    # column padding -- so the pattern above never matches it. Indentation is not
    # a usable signal here: real body lines in the Constitution sit at 8, 10, 12,
    # 16 and even 20 spaces, the same range as these orphan note lines (8-16).
    # Instead require the WHOLE line to consist of note vocabulary, and to carry
    # at least one strong signature. That keeps genuine body text that merely
    # mentions a law ("...sistematizado de la Ley 19175,") because such a line is
    # never note-only.
    # Case-sensitive on purpose: BCN's margin column always uppercases the source
    # ("LEY N° 20.050 Art."), while body text citing a law does not ("de la ley
    # N° 18.700."). Without that distinction those wrapped body lines, which also
    # happen to be made only of note vocabulary, would be dropped as notes.
    _NOTE_STRONG_SIGNATURE = re.compile(
        r'CPR\s*Art\.?|LEY\s*N?[°º]?\s*[\d.]{4,}|D\.\s*O\.|\d{2}\.\d{2}\.\d{4}'
    )
    # A note pushed entirely into the margin column starts far to the right of any
    # body line. Measured over the test corpus, body lines never start past column
    # 20 (BCN wraps the body at ~60 chars), while margin notes start at 60+; 40 sits
    # in the empty band between the two, so it never reaches body text. This catches
    # the wrapped note fragments that are too generic for the vocabulary rules
    # below ("letra a) D.O.", "Nº 3 letra a)").
    _DEEP_INDENT_NOTE_PATTERN = re.compile(r'^ {40,}\S')
    # PR14. The band above turned out not to be empty. Measured over the full population of
    # lines this rule removes (710 lines, 54 documents, .audit/pr14_deep_indent/): BCN's note
    # column is a fixed x-position per document, at indent 66 or 68 in every one of them, while
    # table columns, wrapped column headers and narrow prose columns reach indent 40-53. So
    # column 40 is ~26 columns below the note column and the rule was deleting document
    # content: 290 of the 710 lines (406 words) are table cells, wrapped headers, wrapped prose
    # or an annex heading.
    # A column test alone still cannot be used: norm 256759 puts a real customs-tariff column
    # header ("Estad.", the wrapped third line of "... Unidad / Codigo / Estad.") at indent 66,
    # inside the note column, and a >= 60 rule alone deletes it. The discriminating signal is
    # the note column's bounded VOCABULARY, so the geometric test is combined with the same
    # evidence test PR13 gave the same-line rule. Measured on those 710 lines: zero of the 266
    # content lines below the column match _is_margin_note_tail, and every note verdict it
    # returns sits at indent >= 60.
    # Ordering is load-bearing, for the reason recorded on _MAX_MARGIN_NOTE_TAIL_CHARS: the
    # cheap column test must run before _NOTE_ONLY_LINE_PATTERN can be reached.
    _DEEP_INDENT_MIN_COLUMN = 60
    # BCN also parks a bare editorial marker in that column ("NOTA", "NOTA 1", "VER NOTAS"),
    # which carries no note vocabulary at all. Without this arm PR14 would stop deleting 42
    # further note lines. It is not a note-debris fix; it keeps PR14 from ADDING debris.
    _BARE_NOTE_MARKER_PATTERN = re.compile(r'(?:VER\s+)?NOTAS?(?:\s*\d+)?\.?')
    _NOTE_ONLY_LINE_PATTERN = re.compile(
        r'^[ \t]*(?:'
        r'CPR|LEY|D\.\s*O\.|Art\.?|N[°º]|[úu]nico|inciso|letra|numeral|transitorio'
        r'|bis|de|del|la|el|y|a|N|Nº|[a-z]\)'
        r'|\d{2}\.\d{2}\.\d{4}|[\d.]+'
        r'|[°º()\[\],;.:\-]'
        r'|[ \t]'
        r')+$',
        re.IGNORECASE,
    )
    # A note that wraps may spill onto further lines whose own tail is too generic
    # to recognise on its own ("unico" after "LEY N° 19.519 Art."), so those are
    # only dropped while we are still inside a note block.
    _NOTE_CONTINUATION_LINE_PATTERN = re.compile(
        r'^[ \t]*(?:'
        r'Art\.?|N[°º]|[úu]nico|inciso|letra|numeral|transitorio|bis|de|del|la|el|y|a|N|Nº'
        r'|[a-z]\)|[\d.]+|[°º()\[\],;.:\-]|[ \t]'
        r')+$',
        re.IGNORECASE,
    )

    # A margin note lives in a fixed-width column on the right of the page, so it is SHORT.
    # Measured over the committed XML corpus plus norm 242302: signature-bearing tails run
    # 4-19 characters, note-vocabulary tails 1-18, and not one legitimate note tail exceeds
    # 22. 60 therefore sits far above every observed note while staying far below the body
    # text this rule was deleting (83-word tails in norm 1004655).
    # The bound is also load-bearing for a second reason: _NOTE_ONLY_LINE_PATTERN is a
    # `(?:alt|alt|[ \t])+$` construction, which backtracks catastrophically when it fails on a
    # long string (measured: 1.1s at 2,000 characters, no result at 20,000). Production never
    # hit that because _is_note_only_line() short-circuits before reaching it. Testing a tail
    # for note vocabulary re-opens that path, so the length check must come first.
    _MAX_MARGIN_NOTE_TAIL_CHARS = 60
    # _NOTE_ONLY_LINE_PATTERN tolerates `[\d.]+` and bare punctuation, because a note
    # CONTINUATION legitimately looks like "1° N° 23" once its keyword is on the line above.
    # Applied to a same-line tail with no such context, that tolerance is not evidence: it
    # accepts a numeric table cell ("126049          607534") and a key/value tail
    # (": 7.123.456-8" after "RUN"), both of which are document content. So the vocabulary
    # arm additionally requires one actual note keyword to be present.
    _NOTE_KEYWORD = re.compile(
        r'CPR|LEY|D\.\s*O\.|Art\.?|N[°º]|inciso|letra|numeral|transitorio|[úu]nico|bis',
        re.IGNORECASE,
    )

    def _is_margin_note_tail(self, tail: str) -> bool:
        """
        Does the text after a wide space run actually look like BCN's amendment column?

        PR13. The same-line rule used to be purely geometric: _MARGIN_NOTE_PATTERN deletes a
        4+ space run and everything after it, whatever that text is. Whitespace in BCN's
        export is not exclusively a column separator -- it is also list indentation, table
        column alignment and key/value padding -- so the rule removed substantive document
        text. Measured cases: Article 8 of the Estatuto de Roma reduced to its enumerator
        ("v)"), whole numbered paragraphs of norm 249140 reduced to "1.", substance names in
        the hazardous-waste table of norm 243386, and the values of "Grado" / "RUN" in a
        naval appointment decree.

        The asymmetry this fixes: the WHOLE-line rule (_is_note_only_line) already demanded
        note vocabulary before deleting anything. The same-line rule demanded nothing. This
        applies the evidence test the whole-line rule always had.

        A tail is a note when it carries a strong note signature ("CPR Art.", "LEY N° 20.050",
        "D.O.", a dd.mm.yyyy date), or when it is short enough to fit the margin column and
        consists only of note vocabulary -- which is how a wrapped note fragment such as
        "Art 1° N° 23 letra c)" appears.

        This predicate can only ever KEEP text the previous code removed; it never removes
        anything new. That is the property that bounds this change.
        """
        text = tail.strip()
        if not text:
            return False
        if self._NOTE_STRONG_SIGNATURE.search(text):
            return True
        if len(text) > self._MAX_MARGIN_NOTE_TAIL_CHARS:
            return False
        if not self._NOTE_KEYWORD.search(text):
            return False
        return bool(self._NOTE_ONLY_LINE_PATTERN.match(text))

    def _is_deep_indent_note(self, line: str) -> bool:
        """
        Is a deeply indented line actually BCN's amendment column, or just far-right layout?

        PR14. _DEEP_INDENT_NOTE_PATTERN is geometric and tests nothing about what it deletes;
        this adds the evidence test. A deep-indented line is a note when it starts at or past
        the measured note column AND either reads as a note tail (PR13's predicate, unchanged)
        or is the bare editorial marker BCN puts in the same column.

        Like _is_margin_note_tail, this can only ever KEEP text the previous code removed. It
        is an additional condition on an existing delete branch, so it cannot remove anything
        new, for any input -- that is what bounds the change.
        """
        body = line.strip()
        if not body:
            return False
        if len(line) - len(line.lstrip(' ')) < self._DEEP_INDENT_MIN_COLUMN:
            return False
        return (self._is_margin_note_tail(body)
                or bool(self._BARE_NOTE_MARKER_PATTERN.fullmatch(body)))

    def _is_note_only_line(self, line: str) -> bool:
        if not line.strip() or not self._NOTE_STRONG_SIGNATURE.search(line):
            return False
        # If the same-line rule can already split body from note, the line does
        # carry body text and must not be dropped whole (e.g. a line holding just
        # the closing "." of the previous paragraph plus a margin note).
        if self._MARGIN_NOTE_PATTERN.sub('', line).strip() != line.strip():
            return False
        return bool(self._NOTE_ONLY_LINE_PATTERN.match(line))

    # --- Truncation predicate -----------------------------------------------------
    # Moved here in PR5 from BCNPlaywrightScraper so parser and scraper share ONE
    # implementation instead of two that can drift. The scraper delegates to these;
    # its repair algorithm is unchanged. The rules themselves are untouched.

    # Editorial notes are appended after the body, behind a bare "NOTA" line
    _NOTE_BLOCK_PATTERN = re.compile(r'\n[ \t]*\n[ \t]*NOTAS?[ \t]*\n')
    # A complete Chilean legal article always closes on sentence-terminating
    # punctuation. "º" is excluded on purpose: Art. 19 truncates on a dangling
    # "20º", which would otherwise look like a legitimate ending.
    _SENTENCE_TERMINATORS = '.;:!?)"”'
    # Repealed articles are legitimately a bare marker with no closing period
    _REPEAL_MARKER_PATTERN = re.compile(
        r'^\s*(?:Art\S*\s*[\wº°.\-]*\s*[.\-]*\s*)?'
        r'(?:Derogado|Suprimido|Eliminado|Sin efecto)\s*\.?\s*$',
        re.IGNORECASE,
    )

    def _split_note_block(self, text: str) -> Tuple[str, str]:
        """Splits an article's <Texto> into (body, editorial_note_block)."""
        match = self._NOTE_BLOCK_PATTERN.search(text)
        if not match:
            return text, ""
        return text[:match.start()], text[match.start():]

    def is_part_truncated(self, raw_text: str) -> bool:
        """
        True if a part's text was cut off mid-sentence.

        BCN renders the XML through a fixed-width layout engine that overflows on
        articles carrying dense amendment history, silently cutting <Texto> mid-word
        (e.g. Art. 107 of the Constitution ends at "disposicio").

        Margin notes are stripped first, otherwise the trailing amendment reference in
        the right-hand column would look like the end of the text. An empty body is not
        truncated (there is nothing to cut), and neither is a bare repeal marker, which
        legitimately has no closing period.

        This answers exactly one question -- "does this text end on terminating
        punctuation?" -- about the text it is handed. It has no knowledge of whether a
        repair was attempted, so it cannot report `is_repaired`. See extract_article_
        hierarchy() for what that means for the flag stored per part.
        """
        body, _ = self._split_note_block(html.unescape(raw_text))
        body = self._strip_margin_notes(body).strip()
        if not body or self._REPEAL_MARKER_PATTERN.match(body):
            return False
        return body[-1] not in self._SENTENCE_TERMINATORS

    # --- Margin-note instrumentation ----------------------------------------------
    # PR6. The heuristic below is UNCHANGED; what changed is that it now reports what
    # it removed. There is exactly one implementation of the "this is a margin note"
    # decision (strip_margin_notes_with_stats), and _strip_margin_notes() is a thin
    # wrapper over it, so no second copy of the rules can drift away from the first.

    # The four ways this heuristic removes content, named after the rule that fired.
    # Each name maps 1:1 to a branch of the loop; they are not a reclassification.
    REASON_DEEP_INDENT = 'deep_indent_line'      # _DEEP_INDENT_NOTE_PATTERN
    REASON_NOTE_ONLY = 'note_only_line'          # _is_note_only_line
    REASON_CONTINUATION = 'continuation_line'    # _NOTE_CONTINUATION_LINE_PATTERN
    REASON_SAME_LINE = 'same_line_tail'          # _MARGIN_NOTE_PATTERN

    _MARGIN_NOTE_REASONS = (
        REASON_DEEP_INDENT, REASON_NOTE_ONLY, REASON_CONTINUATION, REASON_SAME_LINE,
    )

    # Cap on captured fragments. Only used by audits and tests that need to show WHICH
    # text was removed; production never asks for them, so no removed content is stored
    # in a document and memory does not grow with document size.
    _MAX_CAPTURED_FRAGMENTS = 20
    _CAPTURED_FRAGMENT_CHARS = 120

    def empty_margin_note_stats(self) -> Dict:
        """Zeroed stats, for parts whose text never reached the heuristic."""
        return {
            'notes_detected': 0,
            'lines_affected': 0,
            'chars_removed': 0,
            'words_removed': 0,
            'by_reason': {reason: 0 for reason in self._MARGIN_NOTE_REASONS},
        }

    def strip_margin_notes_with_stats(
        self, text: str, capture_fragments: bool = False
    ) -> Tuple[str, Dict]:
        """
        Removes BCN's right-hand amendment-history column, and reports what it removed.

        Handles both notes sharing a line with the body and notes occupying whole
        lines of their own (including their wrapped continuations).

        This is the single implementation of the heuristic. The control flow, the order
        of the branches and every pattern are exactly what _strip_margin_notes() ran
        before PR6; the only additions are counters, so the returned text is identical
        for any input.

        Stats:
            notes_detected  contiguous note BLOCKS, not lines: a note that wraps over
                            four lines is one note. Counted on the transition into a
                            note, which is a real boundary in the algorithm and not an
                            invented grouping. There is no separate `notes_removed`:
                            this heuristic has no detect-without-removing path, so the
                            two numbers would be the same by construction and shipping
                            both would just be two names for one fact. Note the cost of
                            counting blocks: two distinct notes on adjacent lines count as
                            one, because a same-line note leaves the block open for the
                            wrapped tail that usually follows. When the question is "how
                            much was removed", read words_removed, not this.
            lines_affected  physical lines that lost content, whole or partial.
            chars_removed   characters dropped. Includes the column gap and the
                            indentation of dropped lines, because BCN pads with
                            whitespace and the gap belongs to the note's layout, not to
                            the body. Consequently this number OVERSTATES lost text.
            words_removed   whitespace-delimited tokens dropped. This is the meaningful
                            "how much text was lost" figure and the one the historical
                            audit used, precisely because chars are inflated by padding.
            by_reason       counts per firing rule, keyed by the REASON_* constants.
            fragments       present only when capture_fragments is True: up to
                            _MAX_CAPTURED_FRAGMENTS truncated samples of removed text,
                            for evidence in audits and tests.
        """
        stats = self.empty_margin_note_stats()
        fragments: List[Dict] = [] if capture_fragments else []

        def record(reason: str, removed: str, line_number: int) -> None:
            stats['notes_detected'] += int(not in_note)
            stats['lines_affected'] += 1
            stats['chars_removed'] += len(removed)
            stats['words_removed'] += len(removed.split())
            stats['by_reason'][reason] += 1
            if capture_fragments and len(fragments) < self._MAX_CAPTURED_FRAGMENTS:
                fragments.append({
                    'reason': reason,
                    'line': line_number,
                    'text': removed.strip()[:self._CAPTURED_FRAGMENT_CHARS],
                })

        kept = []
        in_note = False

        for line_number, line in enumerate(text.split('\n')):
            if (self._DEEP_INDENT_NOTE_PATTERN.match(line)
                    and self._is_deep_indent_note(line)):
                record(self.REASON_DEEP_INDENT, line, line_number)
                in_note = True
                continue

            if self._is_note_only_line(line):
                record(self.REASON_NOTE_ONLY, line, line_number)
                in_note = True
                continue

            if in_note and line.strip() and self._NOTE_CONTINUATION_LINE_PATTERN.match(line):
                record(self.REASON_CONTINUATION, line, line_number)
                continue

            stripped = self._MARGIN_NOTE_PATTERN.sub('', line)
            # PR13: the pattern finding a wide space run is not sufficient evidence that what
            # follows is a note. Unless the tail looks like one, keep the line whole -- the
            # gap is layout (list indentation, table columns, key/value padding), not a
            # column separator.
            if stripped != line and not self._is_margin_note_tail(line[len(stripped):]):
                stripped = line
            if stripped != line:
                record(self.REASON_SAME_LINE, line[len(stripped):], line_number)
            # A same-line note also opens a note block: its wrapped tail lands on
            # the following lines.
            in_note = stripped != line
            # Removing a note leaves the column padding that separated it from the
            # body behind as trailing spaces.
            kept.append(stripped.rstrip())

        if capture_fragments:
            stats['fragments'] = fragments

        return '\n'.join(kept), stats

    def _strip_margin_notes(self, text: str) -> str:
        """
        Removes BCN's right-hand amendment-history column from an article body.

        Kept as the name every call site already uses. The rules live in
        strip_margin_notes_with_stats(); this only discards the stats.
        """
        return self.strip_margin_notes_with_stats(text)[0]

    def _find_article_structures(self, root: ET.Element) -> List[ET.Element]:
        """Find all EstructuraFuncional elements that represent articles (regular or transitory)."""
        return [
            structure for structure in root.findall('.//ns:EstructuraFuncional', self.NS)
            if structure.get('tipoParte') in self.ARTICLE_TIPO_PARTE
        ]

    @staticmethod
    def is_future_version(version_date_str: Optional[str]) -> bool:
        """
        Check if version date is in the future (deferred validity).

        Args:
            version_date_str: ISO format date string (YYYY-MM-DD)

        Returns:
            True if date is in the future, False otherwise
        """
        if not version_date_str:
            return False

        try:
            parsed_date = date.fromisoformat(version_date_str)
            return parsed_date > date.today()
        except (ValueError, TypeError):
            logger.warning("invalid_version_date", version_date=version_date_str)
            return False

    def parse(self, xml_content: str, norm_id: int) -> Optional[ChileanLegalNorm]:
        """
        Parse XML to ChileanLegalNorm.

        Args:
            xml_content: Raw XML string
            norm_id: BCN norm ID

        Returns:
            ChileanLegalNorm object or None if parse fails
        """
        try:
            root = ET.fromstring(xml_content)

            metadata = self._extract_metadata(root, norm_id)
            if not metadata:
                logger.error("metadata_extraction_failed", norm_id=norm_id)
                return None

            full_content = self._extract_full_content(root)

            # Two names for the same string today, deliberately extracted separately so
            # that changing what `full_content` holds cannot change chunker routing (PR9).
            norm = ChileanLegalNorm(
                **metadata,
                full_content=full_content,
                chunking_text=self._extract_chunking_text(root),
            )

            logger.info(
                "xml_parsed_successfully",
                norm_id=norm_id,
                norm_type=norm.norm_type,
                content_length=len(full_content)
            )

            return norm

        except ET.ParseError as e:
            logger.error("xml_parse_error", norm_id=norm_id, error=str(e))
            return None
        except Exception as e:
            logger.error("unexpected_parse_error", norm_id=norm_id, error=str(e))
            return None

    def _extract_metadata(self, root: ET.Element, norm_id: int) -> Optional[Dict]:
        """Extract metadata from XML."""
        try:
            identificador = root.find('ns:Identificador', self.NS)
            if identificador is None:
                return None

            # Dates (both optional - some norms don't have publication date)
            promulgation_date_str = identificador.get('fechaPromulgacion')
            publication_date_str = identificador.get('fechaPublicacion')

            promulgation_date = date.fromisoformat(promulgation_date_str) if promulgation_date_str else None
            publication_date = date.fromisoformat(publication_date_str) if publication_date_str else None

            # If no publication date, use promulgation or version date as fallback
            if not publication_date:
                version_date_str = root.get('fechaVersion')
                publication_date = promulgation_date or (date.fromisoformat(version_date_str) if version_date_str else date.today())

            version_date_str = root.get('fechaVersion')
            last_modified = date.fromisoformat(version_date_str) if version_date_str else None

            type_number_elem = identificador.find('.//ns:TipoNumero', self.NS)
            if type_number_elem is None:
                return None

            type_elem = type_number_elem.find('ns:Tipo', self.NS)
            number_elem = type_number_elem.find('ns:Numero', self.NS)

            if type_elem is None or number_elem is None:
                return None

            type_text = type_elem.text.strip()
            number_text = number_elem.text.strip()

            norm_type = self._map_tipo_to_norm_type(type_text)

            organism_elem = identificador.find('.//ns:Organismo', self.NS)
            issuing_body = organism_elem.text.strip() if organism_elem is not None else "MINISTERIO"

            metadata_elem = root.find('ns:Metadatos', self.NS)
            title_elem = metadata_elem.find('ns:TituloNorma', self.NS) if metadata_elem is not None else None
            title_raw = title_elem.text.strip() if title_elem is not None and title_elem.text else f"{type_text} {number_text}"

            # Clean duplicated titles - avoid "Código 1984: CÓDIGO PENAL"
            title_clean = self._clean_duplicate_title(type_text, number_text, title_raw)

            # Try to extract known código name first for better title formatting
            codigo_name = self._extract_codigo_name_static(type_text, number_text, title_raw)
            if codigo_name:
                # Use clean código name: "Código Penal" instead of "Código PENAL: CÓDIGO PENAL"
                final_title = codigo_name
            else:
                final_title = f"{type_text} {number_text}: {title_clean}"

            # Summary (must be at least 50 chars for Pydantic validation)
            base_summary = final_title

            if len(base_summary) < 50:
                pub_date_str = f"Publicado el {publication_date_str}" if publication_date_str else ""
                base_summary = f"{base_summary}. {pub_date_str}"

                if len(base_summary) < 50:
                    base_summary = f"{base_summary}. Emitido por {issuing_body}"

            summary = base_summary[:500]

            official_url = f"https://bcn.cl/leychile/navegar?idNorma={norm_id}"

            subject_tags = self._extract_subject_tags(root)
            common_name = self._extract_common_name(root)

            return {
                "norm_id": norm_id,
                "norm_type": norm_type,
                "norm_number": number_text,
                "title": final_title,
                "publication_date": publication_date,
                "promulgation_date": promulgation_date,
                "last_modified": last_modified,
                "issuing_body": issuing_body,
                "summary": summary,
                "official_url": official_url,
                "subject_tags": subject_tags,
                "common_name": common_name,
                "version": version_date_str,
            }

        except Exception as e:
            logger.error("metadata_extraction_error", error=str(e))
            return None

    def _extract_subject_tags(self, root: ET.Element) -> List[str]:
        """Extract curated BCN subject tags from <Materias>/<Materia>."""
        tags = []
        for materia_elem in root.findall('.//ns:Materias/ns:Materia', self.NS):
            if materia_elem.text and materia_elem.text.strip():
                tags.append(materia_elem.text.strip())
        return tags

    def _extract_common_name(self, root: ET.Element) -> Optional[str]:
        """Extract popular name from <NombresUsoComun>/<NombreUsoComun>."""
        name_elem = root.find('.//ns:NombresUsoComun/ns:NombreUsoComun', self.NS)
        if name_elem is not None and name_elem.text and name_elem.text.strip():
            return name_elem.text.strip()
        return None

    # Values BCN uses in the norm-level `derogado` attribute. The corpus only ever
    # contains "derogado" / "no derogado" (measured over 1,184 XMLs); "1" is accepted
    # defensively because an earlier implementation expected it.
    REPEALED_ATTR_VALUES = {'derogado', '1'}
    IN_FORCE_ATTR_VALUES = {'no derogado', '0'}

    def extract_norm_vigencia(self, xml_content: str) -> bool:
        """
        Extract norm-level in_force status from the <Norma> `derogado` attribute.

        <Norma> is the ROOT element, not a descendant, so it must be read off the root
        directly. The `.//ns:Norma` lookup used previously could never match, which made
        this method return True unconditionally.

        Note this is deliberately the NORM-level status only. A `FechaDerogacion` inside
        an EstructuraFuncional/Metadatos block marks the repeal of that individual part
        and does not repeal the norm: 58 documents in the audit corpus carry a past
        part-level FechaDerogacion while being correctly `no derogado` as a whole.
        Per-part vigencia is already handled in extract_article_hierarchy().

        Returns:
            True if the norm is in force, False if repealed.
            Defaults to True when the attribute is absent or unrecognized.

        Fallback policy (deliberate): every uncertain case returns True. A wrong False
        would hide a norm that is actually in force from every search, silently and with
        no way for a user to tell the content is missing. A wrong True leaves the norm
        visible and auditable, and shows up as a warning in the logs. The asymmetry is
        intentional; do not "tighten" it into failing closed.
        """
        try:
            root = ET.fromstring(xml_content)

            # <Norma> is the ROOT element. Read the attribute straight off it: there is
            # no descendant lookup on purpose, since that was the original bug.
            local_name = root.tag.split('}')[-1]
            if local_name != 'Norma':
                logger.warning("vigencia_unexpected_root_element", root_tag=root.tag)
                return True

            raw = root.get('derogado')
            if raw is None:
                logger.debug("vigencia_attribute_absent", norma_id=root.get('normaId'))
                return True

            value = raw.strip().lower()

            if value in self.REPEALED_ATTR_VALUES:
                return False
            if value in self.IN_FORCE_ATTR_VALUES:
                return True

            logger.warning(
                "vigencia_attribute_unrecognized",
                value=raw,
                norma_id=root.get('normaId'),
                note="Defaulting to in_force",
            )
            return True

        except Exception as e:
            logger.warning("vigencia_extraction_error", error=str(e))
            return True  # Default to in_force if extraction fails

    def _map_tipo_to_norm_type(self, type_str: str) -> NormType:
        """
        Map XML type to NormType enum.

        BCN's <Tipo> element contains the specific norm type.
        We map it to our expanded NormType enum.
        """
        type_clean = type_str.strip().lower()

        EXACT_MAP = {
            'codigo': NormType.CODIGO,
            'código': NormType.CODIGO,
            'dfl': NormType.DFL,
            'decreto ley': NormType.DL,
            'decreto-ley': NormType.DL,
            'decreto con fuerza de ley': NormType.DFL,
            'decreto supremo': NormType.DECRETO_SUPREMO,
            'orden': NormType.ORDEN,
            'ordenanza': NormType.ORDENANZA,
            'ordenanza municipal': NormType.ORDENANZA_MUNICIPAL,
            'oficio': NormType.OFICIO,
            'resolucion': NormType.RESOLUCION,
            'resolución': NormType.RESOLUCION,
            'circular': NormType.CIRCULAR,
            'instruccion': NormType.INSTRUCCION,
            'instrucción': NormType.INSTRUCCION,
            'reglamento': NormType.REGLAMENTO,
            'acuerdo': NormType.ACUERDO,
            'convenio': NormType.CONVENIO,
            'tratado': NormType.TRATADO,
            'auto acordado': NormType.AUTO_ACORDADO,
            'sentencia': NormType.SENTENCIA,
            'certificado': NormType.CERTIFICADO,
            'dictamen': NormType.DICTAMEN,
            'aviso': NormType.AVISO,
            'bando': NormType.BANDO,
            'notificacion': NormType.NOTIFICACION,
            'notificación': NormType.NOTIFICACION,
            'mensaje': NormType.MENSAJE,
            'otro': NormType.OTRO,
        }

        if type_clean in EXACT_MAP:
            return EXACT_MAP[type_clean]

        # Partial matches (order matters - most specific first)
        if 'decreto ley' in type_clean or 'decreto-ley' in type_clean:
            return NormType.DL
        elif 'dfl' in type_clean or 'fuerza de ley' in type_clean:
            return NormType.DFL
        elif 'decreto supremo' in type_clean:
            return NormType.DECRETO_SUPREMO
        elif 'decreto' in type_clean:
            return NormType.DECRETO
        elif 'codigo' in type_clean or 'código' in type_clean:
            return NormType.CODIGO
        elif 'ordenanza municipal' in type_clean:
            return NormType.ORDENANZA_MUNICIPAL
        elif 'ordenanza' in type_clean:
            return NormType.ORDENANZA
        elif 'orden' in type_clean:
            return NormType.ORDEN
        elif 'oficio' in type_clean:
            return NormType.OFICIO
        elif 'resolucion' in type_clean or 'resolución' in type_clean:
            return NormType.RESOLUCION
        elif 'circular' in type_clean:
            return NormType.CIRCULAR
        elif 'instruccion' in type_clean or 'instrucción' in type_clean:
            return NormType.INSTRUCCION
        elif 'reglamento' in type_clean:
            return NormType.REGLAMENTO
        elif 'acuerdo' in type_clean:
            return NormType.ACUERDO
        elif 'convenio' in type_clean:
            return NormType.CONVENIO
        elif 'tratado' in type_clean:
            return NormType.TRATADO
        elif 'auto acordado' in type_clean:
            return NormType.AUTO_ACORDADO
        elif 'sentencia' in type_clean:
            return NormType.SENTENCIA
        elif 'certificado' in type_clean:
            return NormType.CERTIFICADO
        elif 'dictamen' in type_clean:
            return NormType.DICTAMEN
        elif 'aviso' in type_clean:
            return NormType.AVISO
        elif 'bando' in type_clean:
            return NormType.BANDO
        elif 'notificacion' in type_clean or 'notificación' in type_clean:
            return NormType.NOTIFICACION
        elif 'mensaje' in type_clean:
            return NormType.MENSAJE
        elif 'otro' in type_clean:
            return NormType.OTRO
        elif 'ley' in type_clean:
            return NormType.LEY
        else:
            logger.warning("unknown_norm_type", type_str=type_str)
            return NormType.LEY

    def _clean_duplicate_title(self, type_text: str, number_text: str, title_raw: str) -> str:
        """
        Clean duplicate titles.

        Examples of problems:
        - "Código PENAL: CÓDIGO PENAL" → should be "Código Penal"
        - "Orden 2870: PROTOCOLOS..." → OK (not duplicated)

        Strategy:
        1. If title starts with type_text (ignoring case), remove the duplicate
        2. Normalize case to title case for readability

        Args:
            type_text: Type from XML (e.g., "Código", "Orden")
            number_text: Number from XML (e.g., "1984", "2870")
            title_raw: Raw title from XML

        Returns:
            Cleaned title
        """
        title_lower = title_raw.lower()
        type_lower = type_text.lower()

        if title_lower.startswith(type_lower):
            remaining = title_raw[len(type_text):].strip()
            remaining = remaining.lstrip(':').lstrip('-').strip()

            if remaining == number_text or not remaining:
                return title_raw

            title_clean = remaining
        else:
            title_clean = title_raw

        # Normalize case: convert all-caps to title case for readability
        if title_clean.isupper() and len(title_clean) > 3:
            # "CÓDIGO PENAL" → "Código Penal"
            title_clean = title_clean.title()

        return title_clean

    def _extract_codigo_name_static(self, type_text: str, number_text: str, title_raw: str) -> Optional[str]:
        """
        Extract proper name for códigos from type, number, and title.

        Static version of ChileanLegalNorm._extract_codigo_name() for use in XML parsing.

        Examples:
            "Código", "PENAL", "CÓDIGO PENAL" -> "Código Penal"
            "Código", "1855", "Codigo Civil" -> "Código Civil"

        Returns:
            Clean código name or None if not extractable
        """
        # Well-known códigos mapping
        KNOWN_CODIGOS = {
            'penal': 'Código Penal',
            '1855': 'Código Civil',
            'civil': 'Código Civil',
            'tributario': 'Código Tributario',
            'comercio': 'Código de Comercio',
            'trabajo': 'Código del Trabajo',
            'procedimiento civil': 'Código de Procedimiento Civil',
            'procedimiento penal': 'Código Procesal Penal',
            'mineria': 'Código de Minería',
            'minería': 'Código de Minería',
            'aguas': 'Código de Aguas',
        }

        # Check norm_number first (most reliable)
        norm_num_lower = number_text.lower()
        if norm_num_lower in KNOWN_CODIGOS:
            return KNOWN_CODIGOS[norm_num_lower]

        title_lower = title_raw.lower()
        for key, name in KNOWN_CODIGOS.items():
            if key in title_lower:
                return name

        if ':' in title_raw:
            parts = title_raw.split(':', 1)
            if len(parts) == 2:
                after_colon = parts[1].strip().title()
                # Avoid duplicated words like "Código Código"
                if after_colon.lower().startswith('codigo'):
                    return after_colon

        return None

    def _extract_full_content(self, root: ET.Element) -> str:
        """
        Extract the text stored as `full_content`.

        Contract as of PR11, and now complete:

            <Encabezado> + every <EstructuraFuncional> + every <Anexo> + <Promulgacion>

        joined by blank lines, in that order. That is the reading order PR3 documented and
        the order both extract_article_texts() and extract_article_hierarchy() build their
        dicts in, so this field and those two structures agree.

        The order matters and is not "append the new thing at the end". PR10 appended the
        promulgation last, which was correct only because no annex was present; PR11 inserts
        the annexes BEFORE it, because the promulgation closes the document.

        Known discrepancy, deliberately NOT resolved here: for an annex that the chunker
        SPLITS, `_split_annexes_by_treaty_article()` deletes the parent key and appends the
        sub-keys, so the resulting chunk sequence puts those blocks after the promulgation
        chunk. That is the pre-existing ordering defect recorded in docs/KNOWN_BUGS.md; it
        lives in the chunker, and this method follows the parser's order rather than
        mirroring it.

        History, because the omissions were long-lived: before PR10 this method returned
        exactly _extract_chunking_text(), so the promulgation was absent from 1,176 of the
        1,184 audited documents; before PR11 the annexes were absent from 314 of them, where
        the annex is an estimated median 85% of the field.

        This method and _extract_chunking_text() stay separate because they answer different
        questions: this one is "what do we store", the other is "what does the chunker route
        on" (PR9). This one may grow; the other must not, or routing changes with it. That
        separation is what makes PR11 a storage change: annex text carries "Articulo N"
        lines, and reaching the routing input it would flip `_detect_articles()`.
        """
        # Neither the annexes nor the promulgation are located with a second find() here.
        # Both come from the helpers that already produce the stored parts, so the text
        # appended is byte-identical to the corresponding article_texts entries: same node
        # resolution, same binary removal, same margin-note stripping, same skipping of
        # empty and id-less nodes, same document order.
        annexes = [part['text'] for part in self._extract_annex_parts(root)]
        promulgation = [
            part['text'] for part in self._extract_synthetic_parts(root)
            if part['content_type'] == self.CONTENT_TYPE_PROMULGATION
        ]
        # The header is excluded from the synthetic list on purpose: _extract_chunking_text()
        # already emitted it, and appending it again would duplicate content.
        # Empty sections are filtered so a document with no header and no structures does
        # not gain a leading blank line, matching how _extract_chunking_text() joins.
        return '\n\n'.join(
            section
            for section in [self._extract_chunking_text(root)] + annexes + promulgation
            if section
        )

    def _extract_annex_parts(self, root: ET.Element) -> List[Dict[str, str]]:
        """
        The <Anexo> bodies that become parts, in document order.

        Extracted in PR11 from the loop extract_article_texts() already ran, and called from
        both, so `full_content` cannot drift from `article_texts`. The rules are that loop's
        rules, unchanged: an annex with no `idParte` is skipped (it could not be keyed), an
        annex with no <Texto> or no text left after stripping is skipped, and the key is
        `anexo_{idParte}`.

        Skipping id-less annexes means their text reaches neither the chunks nor
        `full_content`. That is pre-existing behavior, not a PR11 decision; no annex in the
        9 real annex documents on disk lacks an `idParte`, and the corpus-wide count is
        unmeasured.
        """
        parts = []
        for annex in root.findall('.//ns:Anexo', self.NS):
            id_parte = annex.get('idParte')
            if not id_parte:
                continue

            text_elem = annex.find('ns:Texto', self.NS)
            if text_elem is None:
                continue

            text = self._strip_margin_notes(self._extract_text_without_binaries(text_elem))
            if text:
                parts.append({'key': f"anexo_{id_parte}", 'text': text})

        return parts

    def _extract_chunking_text(self, root: ET.Element) -> str:
        """
        Extract the plain text handed to the chunker for routing and fallback chunking.

        Contract, as implemented (not as the name of `full_content` suggests):

        - INCLUDES <Encabezado>/<Texto> and the <Texto> of every <EstructuraFuncional>,
          nested ones included (the search is `.//`), joined by blank lines.
        - EXCLUDES <Anexo> and <Promulgacion> entirely.
        - Margin notes stripped, binary attachments (images) removed.

        The exclusions are a known defect for `full_content` (docs/KNOWN_BUGS.md) but are
        load-bearing here: annex text is full of "Articulo N" lines, and adding it would
        flip `_detect_articles()` and change which fallback branch runs. Pinned by
        tests/test_chunking_text_decoupling.py.
        """
        content_parts = []

        header = root.find('ns:Encabezado/ns:Texto', self.NS)
        if header is not None:
            text = self._strip_margin_notes(self._extract_text_without_binaries(header))
            if text:
                content_parts.append(text)

        for structure in root.findall('.//ns:EstructuraFuncional', self.NS):
            text_elem = structure.find('ns:Texto', self.NS)
            if text_elem is not None:
                text = self._strip_margin_notes(self._extract_text_without_binaries(text_elem))
                if text:
                    content_parts.append(text)

        return '\n\n'.join(content_parts)

    # Conceptual kind of content a part carries. Added in PR3 so non-article content
    # (header, promulgation) can travel through the same intermediate model as articles
    # instead of being dropped. Existing part_ids are untouched; only synthetic parts get
    # synthetic ids.
    CONTENT_TYPE_ARTICLE = 'article'
    CONTENT_TYPE_HEADER = 'header'
    CONTENT_TYPE_PROMULGATION = 'promulgation'
    CONTENT_TYPE_ANNEX = 'annex'
    CONTENT_TYPE_TRANSITORY = 'transitory'

    def _synthetic_id_base(self, root: ET.Element, norm_id: Optional[int] = None) -> str:
        """
        Id used to build synthetic part keys (header_{id}, promulgation_{id}).

        Read from the XML itself so extract_article_hierarchy() and
        extract_article_texts() always agree, even though only the former is given a
        norm_id. A mismatch between them would make the chunker drop the part.
        """
        return root.get('normaId') or (str(norm_id) if norm_id is not None else 'unknown')

    def _extract_synthetic_parts(
        self, root: ET.Element, norm_id: Optional[int] = None
    ) -> List[Dict]:
        """
        Extract <Encabezado> and <Promulgacion> as synthetic parts.

        These carry real normative content -- the header holds the title, number, issuing
        body and preamble; the promulgation holds the signatures and the "tomese razon"
        formula -- but neither has an idParte, so neither ever entered
        hierarchy/article_texts. Measured coverage before PR3: header 23.8%,
        promulgation 0.0%.

        Extracted straight from the XML structure. _extract_special_chunks() is NOT used:
        it reconstructed the preamble by diffing text against article bodies, which is
        both lossy and unable to see the promulgation at all.

        Returns parts in reading order (header first, promulgation last), each as a dict
        the two public extractors can consume without duplicating logic.
        """
        id_base = self._synthetic_id_base(root, norm_id)

        specs = (
            ('Encabezado', f'header_{id_base}', 'Encabezado', self.CONTENT_TYPE_HEADER),
            ('Promulgacion', f'promulgation_{id_base}', 'Promulgación',
             self.CONTENT_TYPE_PROMULGATION),
        )

        parts = []
        for tag, key, label, content_type in specs:
            element = root.find(f'ns:{tag}', self.NS)
            if element is None:
                continue

            text_elem = element.find('ns:Texto', self.NS)
            if text_elem is None:
                continue

            # Same cleaning as articles. PR3 does not change margin-note behavior.
            # PR6 asks the same call what it removed, so the header's and promulgation's
            # stats come from the very invocation that produced their text -- not from a
            # second pass that could see something different.
            text, margin_note_stats = self.strip_margin_notes_with_stats(
                self._extract_text_without_binaries(text_elem)
            )
            if not text:
                continue

            repealed_attr = element.get('derogado', 'no derogado')

            parts.append({
                'key': key,
                'label': label,
                'content_type': content_type,
                'text': text,
                'in_force': repealed_attr == 'no derogado',
                'version_date': element.get('fechaVersion'),
                'margin_notes': margin_note_stats,
            })

        return parts

    def _synthetic_hierarchy_entry(self, part: Dict) -> Dict:
        """
        Hierarchy entry for a synthetic part.

        Deliberately carries the same keys as an article entry so every existing consumer
        (chunker, metadata builders) keeps working without special-casing. article_number
        is None, exactly as annexes already do.
        """
        return {
            'article_number': None,
            'article_label': part['label'],
            'is_nested': False,
            'parent_article': None,
            'hierarchy_level': 0,
            'in_force': part['in_force'],
            'version_date': part['version_date'],
            'is_transitory': False,
            'content_type': part['content_type'],
            # PR5: a synthetic part cannot be truncated under the defined semantics (BCN's
            # layout overflow hits articles with amendment history, and the repair path
            # never considers <Encabezado>/<Promulgacion>). Stated explicitly as False
            # rather than left absent, because every consumer -- and the key-set equality
            # test in tests/test_synthetic_parts.py -- expects article-shaped entries.
            'is_truncated': False,
            # PR6: measured, not stated. A header or promulgation goes through exactly the
            # same margin-note stripping as an article (_extract_synthetic_parts), so it
            # can lose content the same way and the number must be real.
            'margin_notes': part['margin_notes'],
        }

    def extract_article_hierarchy(self, xml_content: str, norm_id: int) -> Dict[str, Dict]:
        """
        Extract article hierarchy from XML.

        Returns dict mapping idParte -> article metadata.

        This is MUCH simpler than HTML version:
        - idParte is direct attribute
        - derogado is direct attribute (vigencia!)
        - hierarchy is native XML tree
        - fechaVersion per article

        IMPORTANT: Detects nested structures:
        - Articles inside N°/romano/TÍTULO are marked as nested
        - parent_article is set to the parent container number
        """
        try:
            root = ET.fromstring(xml_content)
            hierarchy = {}

            parent_map = {c: p for p in root.iter() for c in p}

            # Header goes first so the intermediate model keeps reading order.
            for part in self._extract_synthetic_parts(root, norm_id):
                if part['content_type'] != self.CONTENT_TYPE_HEADER:
                    continue
                hierarchy[part['key']] = self._synthetic_hierarchy_entry(part)

            for structure in self._find_article_structures(root):
                id_parte = structure.get('idParte')
                if not id_parte:
                    continue

                name_elem = structure.find('.//ns:NombreParte', self.NS)
                article_label = name_elem.text.strip() if name_elem is not None and name_elem.text else None

                if article_label:
                    # Normalize suffixes to lowercase (390 BIS -> 390 bis)
                    article_label = re.sub(
                        r'\b(BIS|TER|QUATER|QUINQUIES|SEXIES|SEPTIES|OCTIES|NONIES|DECIES)\b',
                        lambda m: m.group(1).lower(),
                        article_label
                    )

                article_number = self._extract_article_number(article_label) if article_label else None

                if not article_label:
                    # No NombreParte. Some nodes with an article-like tipoParte are actually
                    # section-heading containers that wrap real articles as children (e.g. a
                    # "DISPOSICIONES TRANSITORIAS" divider wrapping the actual transitory
                    # articles) -- those must stay excluded, same as before this fallback.
                    # A genuine article missing only its label is a leaf (no nested article
                    # children); keep those with a fallback label so they aren't silently
                    # dropped from the chunker output.
                    has_nested_articles = any(
                        child.get('tipoParte') in self.ARTICLE_TIPO_PARTE
                        for child in structure.findall('.//ns:EstructuraFuncional', self.NS)
                    )
                    if has_nested_articles:
                        continue

                    # Some transitory provisions have an empty <NombreParte> but carry
                    # their ordinal name in <TituloParte> instead (e.g. "DISPOSICIÓN
                    # TRANSITORIA QUINCUAGÉSIMA PRIMERA TRANSITORIO"). Strip the
                    # boilerplate wrapper so the label matches siblings that do have a
                    # NombreParte (e.g. "QUINCUAGÉSIMA PRIMERA").
                    titulo_elem = structure.find('.//ns:TituloParte', self.NS)
                    titulo_text = titulo_elem.text.strip() if titulo_elem is not None and titulo_elem.text else None
                    if titulo_text:
                        titulo_label = re.sub(
                            r'^DISPOSICI[OÓ]N TRANSITORIA\s+', '', titulo_text, flags=re.IGNORECASE
                        )
                        titulo_label = re.sub(
                            r'\s+TRANSITORIO$', '', titulo_label, flags=re.IGNORECASE
                        ).strip()
                        article_label = titulo_label if titulo_label else None

                    if not article_label:
                        article_label = f"Artículo {article_number}" if article_number else "Contenido"

                # 1. Legacy: has "DEL ART" in name
                is_nested_legacy = 'DEL ART' in article_label.upper()
                parent_article = None

                if is_nested_legacy:
                    match = re.search(r'DEL ART[^\d]*(\d+)', article_label, re.IGNORECASE)
                    if match:
                        parent_article = int(match.group(1))

                # 2. NEW: Check if inside a parent structure (N°, romano, TÍTULO, etc.)
                is_nested_structural, parent_structural = self._detect_nested_structure(
                    structure, parent_map
                )

                # Only mark as nested if we have a valid parent_article
                if parent_structural is not None:
                    parent_article = parent_structural

                # Only set is_nested=True if we actually found a parent article number
                is_nested = parent_article is not None

                hierarchy_level = self._get_hierarchy_level(structure, root)

                repealed_attr = structure.get('derogado', 'no derogado')
                in_force = repealed_attr == 'no derogado'

                version_date_str = structure.get('fechaVersion')

                transitorio_attr = structure.get('transitorio', 'no transitorio')
                is_transitory = transitorio_attr == 'transitorio'

                # PR5 observability. Describes the XML this parser was handed, which in
                # production is already post-repair: True therefore means "still cut off
                # after whatever repair ran", not "BCN served it cut off". A part that was
                # truncated and successfully repaired is indistinguishable here from one
                # that was never truncated -- both end on a period -- so this flag is
                # deliberately NOT is_repaired. See is_part_truncated().
                text_elem = structure.find('ns:Texto', self.NS)
                raw_text = (
                    self._extract_text_without_binaries(text_elem)
                    if text_elem is not None else ''
                )
                is_truncated = self.is_part_truncated(raw_text) if text_elem is not None else False

                # PR6 observability. Measured on the SAME raw text that
                # extract_article_texts() strips to build this part's chunk, so the numbers
                # describe the text that actually shipped. Observation only: the heuristic
                # is untouched and the extracted text is byte-identical.
                margin_note_stats = (
                    self.strip_margin_notes_with_stats(raw_text)[1]
                    if text_elem is not None else self.empty_margin_note_stats()
                )

                hierarchy[id_parte] = {
                    'article_number': article_number,
                    'article_label': article_label,
                    'is_nested': is_nested,
                    'parent_article': parent_article,
                    'hierarchy_level': hierarchy_level,
                    'in_force': in_force,
                    'version_date': version_date_str,
                    'is_transitory': is_transitory,
                    'content_type': (
                        self.CONTENT_TYPE_TRANSITORY if is_transitory
                        else self.CONTENT_TYPE_ARTICLE
                    ),
                    'is_truncated': is_truncated,
                    'margin_notes': margin_note_stats,
                }

            # Extract treaty annexes (Tratados internacionales)
            # Anexos contain full treaty text that would otherwise be missed
            for annex in root.findall('.//ns:Anexo', self.NS):
                id_parte = annex.get('idParte')
                if not id_parte:
                    continue

                title_elem = annex.find('.//ns:Titulo', self.NS)
                annex_title = title_elem.text.strip() if title_elem is not None and title_elem.text else "Anexo"

                repealed_attr = annex.get('derogado', 'no derogado')
                in_force = repealed_attr == 'no derogado'

                version_date_str = annex.get('fechaVersion')

                annex_key = f"anexo_{id_parte}"

                # PR6: measured, unlike is_truncated above. Annexes are outside the
                # truncation semantics, but they ARE stripped -- extract_article_texts()
                # runs _strip_margin_notes() over every annex body -- so an annex can lose
                # table rows or enumerations to the heuristic and that loss must be
                # visible. Measuring it is not implementing annex handling; PR7 owns that.
                annex_text_elem = annex.find('ns:Texto', self.NS)
                annex_margin_notes = (
                    self.strip_margin_notes_with_stats(
                        self._extract_text_without_binaries(annex_text_elem)
                    )[1]
                    if annex_text_elem is not None else self.empty_margin_note_stats()
                )

                hierarchy[annex_key] = {
                    'article_number': None,
                    'article_label': 'Anexo',
                    'annex_title': annex_title,
                    'is_annex': True,
                    'is_nested': False,
                    'parent_article': None,
                    'hierarchy_level': 0,
                    'in_force': in_force,
                    'version_date': version_date_str,
                    'is_transitory': False,
                    'content_type': self.CONTENT_TYPE_ANNEX,
                    # PR5: annexes are outside the truncation semantics -- the repair path
                    # only ever looks at tipoParte Articulo / Disposicion Transitoria -- so
                    # the flag is stated as False rather than measured with a predicate that
                    # was never validated against annex bodies.
                    'is_truncated': False,
                    'margin_notes': annex_margin_notes,
                }

            # Promulgation closes the document, so it goes last.
            for part in self._extract_synthetic_parts(root, norm_id):
                if part['content_type'] != self.CONTENT_TYPE_PROMULGATION:
                    continue
                hierarchy[part['key']] = self._synthetic_hierarchy_entry(part)

            logger.info(
                "hierarchy_extracted",
                norm_id=norm_id,
                total_articles=len(hierarchy),
                annexes=sum(1 for h in hierarchy.values() if h.get('is_annex', False))
            )

            return hierarchy

        except Exception as e:
            logger.error("hierarchy_extraction_failed", norm_id=norm_id, error=str(e))
            return {}

    def _extract_article_number(self, article_label: str) -> Optional[int]:
        """Extract article number from label."""
        # Pattern: "1", "2 (DEL ART 1)", etc.
        match = re.match(r'(\d+)', article_label)
        if match:
            return int(match.group(1))

        return None

    def _get_hierarchy_level(self, element: ET.Element, root: ET.Element) -> int:
        """
        Get hierarchy level by counting parent EstructuraFuncional elements.

        Much simpler than HTML version (no need to count <ul> parents).
        """
        level = 0
        current = element

        # Walk up the tree counting EstructuraFuncional parents
        while True:
            parent = self._find_parent(current, root)
            if parent is None:
                break

            # Check if parent is also EstructuraFuncional
            if parent.tag.endswith('EstructuraFuncional'):
                level += 1

            current = parent

        return level

    def _find_parent(self, element: ET.Element, root: ET.Element) -> Optional[ET.Element]:
        """Find parent element in tree."""
        for parent in root.iter():
            if element in parent:
                return parent
        return None

    def _detect_nested_structure(
        self,
        article_element: ET.Element,
        parent_map: Dict
    ) -> Tuple[bool, Optional[int]]:
        """
        Detect if an article is nested inside a parent structure (N°, romano, TÍTULO, etc.).

        Example XML structure:
        <EstructuraFuncional tipoParte="N°" idParte="10534843">
            <Articulo>2.</Articulo>
            <EstructuraFuncional tipoParte="Artículo" idParte="10534844">
                <Articulo>1º</Articulo>  <!-- This is nested! -->
            </EstructuraFuncional>
        </EstructuraFuncional>

        Args:
            article_element: The article element to check
            parent_map: Dict mapping child -> parent for upward navigation

        Returns:
            (is_nested, parent_number): Tuple with:
                - is_nested: True if article is inside a parent structure
                - parent_number: Number of parent container (e.g., 2 for "N° 2")
        """
        # Parent container types that can contain nested articles
        # Only actual nested structures (articles within articles)
        # TÍTULO, Capítulo, Sección are NORMAL hierarchy, not nested articles
        CONTAINER_TYPES = [
            'N°',               # Numbers within articles (nested structure)
            'romano',           # Roman numerals within articles
            'Doble Articulado', # Ordenanzas with nested article structure
            'Párrafo'           # Some use Párrafo as container of articles
        ]

        # Traverse up to find parent EstructuraFuncional
        current = article_element
        while current is not None:
            parent = parent_map.get(current)
            if parent is None:
                break

            # Check if parent is EstructuraFuncional with a container type
            if parent.tag.endswith('EstructuraFuncional'):
                parent_tipo = parent.get('tipoParte')

                if parent_tipo in CONTAINER_TYPES:
                    # Found a container! Extract its number
                    parent_number = self._extract_container_number(parent)

                    logger.debug(
                        "nested_structure_detected",
                        article_id=article_element.get('idParte'),
                        parent_tipo=parent_tipo,
                        parent_number=parent_number
                    )

                    return (True, parent_number)

            current = parent

        # Not nested
        return (False, None)

    def _extract_container_number(self, container_element: ET.Element) -> Optional[int]:
        """
        Extract number from a container element (N°, romano, etc.).

        Examples:
        - <Articulo>2.</Articulo> → 2
        - <Articulo>N° 2</Articulo> → 2
        - <Articulo>II</Articulo> → 2 (romano)
        """
        # Get the <Articulo> or <NombreParte> element
        name_elem = container_element.find('.//ns:Articulo', self.NS)
        if name_elem is None:
            name_elem = container_element.find('.//ns:NombreParte', self.NS)

        if name_elem is not None and name_elem.text:
            text = name_elem.text.strip()

            # Try to extract number
            # Pattern: "2", "2.", "N° 2", "Nº 2"
            match = re.search(r'(\d+)', text)
            if match:
                return int(match.group(1))

            # TODO: Handle roman numerals (II, III, IV, etc.)
            # For now, return None for roman numerals

        return None

    def _extract_text_without_binaries(self, element: ET.Element) -> str:
        """
        Extract all text from element, skipping binary attachments (images).

        BCN includes inline `<aem:ArchivoBinario>` elements with base64 images.
        These must be skipped to avoid:
        1. Corrupting text extraction
        2. Cutting off text that comes after images

        Uses itertext() to get all text nodes, filtering out binary elements.
        """
        text_parts = []

        # Get element's direct text (before any children)
        if element.text:
            text_parts.append(element.text)

        # Iterate through all children
        for child in element:
            # Skip binary attachments (images)
            # Check tag with or without namespace
            tag = child.tag
            if 'ArchivoBinario' in tag:
                # Skip this element entirely (it's a binary image)
                # But get the tail text (text after this element)
                if child.tail:
                    text_parts.append(child.tail)
                continue

            # For other children, recurse
            child_text = self._extract_text_without_binaries(child)
            if child_text:
                text_parts.append(child_text)

            # Get tail text (text after this child element)
            if child.tail:
                text_parts.append(child.tail)

        # Join and clean up
        full_text = ''.join(text_parts).strip()
        return full_text

    def extract_article_texts(
        self, xml_content: str, norm_id: Optional[int] = None
    ) -> Dict[str, str]:
        """
        Extract article texts from XML.

        Returns dict mapping idParte -> article text, in reading order: header, articles,
        annexes, promulgation. The chunker iterates this dict, so insertion order is what
        determines chunk order.

        Args:
            xml_content: XML string
            norm_id: Only used to build synthetic keys when the XML lacks normaId;
                     optional so existing callers keep working unchanged.

        IMPORTANT: Removes binary attachments (images) that BCN includes inline.
        """
        try:
            root = ET.fromstring(xml_content)
            article_texts = {}

            synthetic_parts = self._extract_synthetic_parts(root, norm_id)

            for part in synthetic_parts:
                if part['content_type'] == self.CONTENT_TYPE_HEADER:
                    article_texts[part['key']] = part['text']

            for structure in self._find_article_structures(root):
                id_parte = structure.get('idParte')
                if not id_parte:
                    continue

                # Get text element
                text_elem = structure.find('ns:Texto', self.NS)
                if text_elem is not None:
                    # Extract all text, skipping binary attachments and margin notes
                    text = self._strip_margin_notes(self._extract_text_without_binaries(text_elem))
                    if text:
                        article_texts[id_parte] = text

            # Extract treaty annex texts
            # Annexes contain full treaty text that would otherwise be missed.
            # PR11: this loop moved verbatim into _extract_annex_parts() so that
            # _extract_full_content() can store the same text instead of re-deriving it.
            # Keys, order and skipping rules are unchanged.
            for part in self._extract_annex_parts(root):
                article_texts[part['key']] = part['text']

            for part in synthetic_parts:
                if part['content_type'] == self.CONTENT_TYPE_PROMULGATION:
                    article_texts[part['key']] = part['text']

            logger.debug(
                "article_texts_extracted",
                total_articles=len(article_texts),
                annexes=sum(1 for k in article_texts.keys() if k.startswith('anexo_')),
                synthetic_parts=len(synthetic_parts),
            )

            return article_texts

        except Exception as e:
            logger.error("article_texts_extraction_failed", error=str(e))
            return {}

    def extract_structural_context(self, xml_content: str, norm_id: int) -> Dict[str, Dict]:
        """
        Extract structural context (Book, Title, Section/Paragraph) for each article.

        Returns dict mapping idParte -> structural context with full names.

        Structure:
        {
            "article_id_parte": {
                "book": "LIBRO SEGUNDO",
                "book_name": "CRIMENES Y SIMPLES DELITOS Y SUS PENAS",
                "title_ordinal": "TITULO OCTAVO",
                "title_name": "CRIMENES Y SIMPLES DELITOS CONTRA LAS PERSONAS",
                "section": "§1 bis",
                "section_name": "Del femicidio"
            }
        }

        This is CRITICAL for hybrid search:
        - BM25 needs the structural names in text
        - Reranker needs them visible (not just metadata)
        - "femicidio", "malversacion", "bigamia" exist ONLY in section names
        """
        try:
            root = ET.fromstring(xml_content)
            context_map = {}

            # Find all articles and walk up to get their structural context
            for article in self._find_article_structures(root):
                id_parte = article.get('idParte')
                if not id_parte:
                    continue

                # Walk up the tree to find structural ancestors
                context = self._find_structural_ancestors(article, root)
                if context:
                    context_map[id_parte] = context

            logger.info(
                "structural_context_extracted",
                norm_id=norm_id,
                total_articles=len(context_map)
            )

            return context_map

        except Exception as e:
            logger.error("structural_context_extraction_failed", norm_id=norm_id, error=str(e))
            return {}

    def _container_display_title(self, container: ET.Element, titulo_parte: str) -> str:
        """
        Prefer the heading BCN prints inside the container's own <Texto> when it is a
        longer form of <TituloParte>.

        <TituloParte> is normalized by BCN and can be identical for two different
        containers (Decreto 49 has two chapters both labelled "DISPOSICIONES
        TRANSITORIAS"), while the printed heading distinguishes them
        ("DISPOSICIONES TRANSITORIAS DEL PRESENTE DECRETO"). Only extensions of
        the metadata label are accepted, so a divergent heading never overrides it.
        """
        texto_elem = container.find('./ns:Texto', self.NS)
        if texto_elem is None or not texto_elem.text:
            return titulo_parte

        # The heading is the first block of the <Texto>, before the NOTA marker
        # BCN appends at the right margin and before the note body itself.
        heading_block = texto_elem.text.split('NOTA')[0]
        heading = ' '.join(heading_block.split()).strip(' :.-')
        if not heading:
            return titulo_parte

        def key(value: str) -> str:
            return ' '.join(value.split()).lower()

        if key(heading) != key(titulo_parte) and key(heading).startswith(key(titulo_parte)):
            return heading

        return titulo_parte

    def _find_structural_ancestors(self, element: ET.Element, root: ET.Element) -> Dict:
        """
        Walk up the XML tree to find Libro, Titulo, Parrafo ancestors.

        Reads <TituloParte> (not <NombreParte>, which is empty for structural elements).
        """
        context = {}
        current = element

        # Walk up the tree
        while True:
            parent = self._find_parent(current, root)
            if parent is None:
                break

            type_val = parent.get('tipoParte')
            # Added "Capítulo" for Constitución and other norms
            if type_val in ['Libro', 'Título', 'Capítulo', 'Párrafo']:
                # Read <TituloParte> from Metadata (this is where BCN stores the full name)
                title_elem = parent.find('.//ns:TituloParte', self.NS)
                if title_elem is not None and title_elem.text:
                    full_text = self._container_display_title(parent, title_elem.text.strip())

                    # Parse the full text to separate ordinal from name
                    # Examples:
                    # "LIBRO SEGUNDO CRIMENES Y SIMPLES DELITOS Y SUS PENAS"
                    # "TITULO OCTAVO CRIMENES Y SIMPLES DELITOS CONTRA LAS PERSONAS"
                    # "Capítulo I BASES DE LA INSTITUCIONALIDAD"
                    # "§1 bis. Del femicidio"

                    if type_val == 'Libro':
                        # Extract "LIBRO SEGUNDO" and "CRIMENES..."
                        parts = full_text.split(maxsplit=2)  # ["LIBRO", "SEGUNDO", "rest"]
                        if len(parts) >= 2:
                            ordinal = f"{parts[0]} {parts[1]}"
                            name = parts[2] if len(parts) > 2 else ""
                            context['book'] = ordinal
                            context['book_name'] = name

                    elif type_val == 'Título':
                        # Extract "TITULO OCTAVO" and "CRIMENES..."
                        parts = full_text.split(maxsplit=2)
                        if len(parts) >= 2:
                            ordinal = f"{parts[0]} {parts[1]}"
                            name = parts[2] if len(parts) > 2 else ""
                            context['title_ordinal'] = ordinal
                            context['title_name'] = name

                    elif type_val == 'Capítulo':
                        # Handle chapters (common in Constitución)
                        # "Capítulo I BASES DE LA INSTITUCIONALIDAD" -> "Capítulo I" + name
                        # Chapters whose label is not "Capítulo <ordinal>" (e.g.
                        # "DISPOSICIONES TRANSITORIAS DEL PRESENTE DECRETO") are kept
                        # whole: splitting them on the second word would drop exactly
                        # the words that tell two same-named chapters apart.
                        match = re.match(
                            r'^(Cap[íi]tulo\s+[\dIVXLC]+(?:\s+(?:bis|ter|quater))?)\s*[:.\-–]?\s*(.*)$',
                            full_text,
                            re.IGNORECASE,
                        )
                        if match:
                            context['title_ordinal'] = match.group(1).strip()
                            context['title_name'] = match.group(2).strip()
                        else:
                            context['title_ordinal'] = full_text
                            context['title_name'] = ""

                    elif type_val == 'Párrafo':
                        # Extract "§1 bis" and "Del femicidio"
                        # Pattern: "§N" or "§N bis/ter" followed by ". Name"
                        match = re.match(r'^(§\s*\d+(?:\s+[a-z]+)?)\.\s*(.*)$', full_text, re.IGNORECASE)
                        if match:
                            context['section'] = match.group(1).strip()
                            context['section_name'] = match.group(2).strip()
                        else:
                            # Fallback: treat entire text as section
                            context['section'] = full_text
                            context['section_name'] = ""

            current = parent

        return context

    def extract_binary_content(self, xml_content: str, norm_id: int) -> Dict[str, dict]:
        """
        Detect articles with binary content (images, tables).

        BCN includes <aem:ArchivoBinario> elements with base64-encoded images/tables
        in articles that have visual content. This method detects which articles
        have binary content so we can:
        1. Add metadata flag
        2. Add textual note to warn users
        3. Mark content as incomplete

        Args:
            xml_content: Raw XML string from BCN
            norm_id: Norm ID for logging

        Returns:
            Dict mapping part_id -> binary info:
            {
                "10534846": {
                    "present": True,
                    "type": "image",
                    "filename": "tabla_tarifas.png",
                    "description": "Contenido binario no indexado"
                }
            }
        """
        try:
            root = ET.fromstring(xml_content)
            binary_map = {}

            # Build a child->parent map first (ElementTree doesn't support upward navigation)
            parent_map = {c: p for p in root.iter() for c in p}

            # Find all ArchivoBinario elements (with or without namespace)
            # BCN uses: <aem:ArchivoBinario nombre="filename.png">base64data</aem:ArchivoBinario>
            for archivo in root.iter():
                if 'ArchivoBinario' in archivo.tag:
                    # Found a binary element, now find its parent article.
                    # BCN carries the metadata as child elements, not attributes:
                    # <aem:Nombre>, <aem:TipoContenido>, <aem:CantidadBytes>.
                    filename = 'unknown'
                    content_type = None
                    byte_count = None
                    for child in archivo:
                        if child.tag.endswith('Nombre') and child.text:
                            filename = child.text.strip()
                        elif child.tag.endswith('TipoContenido') and child.text:
                            content_type = child.text.strip()
                        elif child.tag.endswith('CantidadBytes') and child.text:
                            byte_count = child.text.strip()

                    # Traverse up using parent_map to find element with idParte
                    current = archivo
                    while current is not None:
                        parent = parent_map.get(current)
                        if parent is None:
                            break

                        part_id = parent.get('idParte')
                        if part_id:
                            # Found the article containing this binary
                            # Detect type from filename
                            binary_type = 'image'
                            if filename:
                                ext = filename.lower().split('.')[-1]
                                if ext in ['png', 'jpg', 'jpeg', 'gif', 'bmp']:
                                    binary_type = 'image'
                                elif ext in ['pdf']:
                                    binary_type = 'document'
                                else:
                                    binary_type = 'unknown'

                            # A single article can carry several scanned pages,
                            # so attachments accumulate instead of overwriting.
                            entry = binary_map.setdefault(part_id, {
                                'present': True,
                                'type': binary_type,
                                'filename': filename,
                                'description': 'Contenido binario no indexado (imagen o tabla)',
                                'attachments': []
                            })
                            entry['attachments'].append({
                                'filename': filename,
                                'type': binary_type,
                                'content_type': content_type,
                                'bytes': int(byte_count) if byte_count and byte_count.isdigit() else None,
                            })

                            logger.debug(
                                "binary_content_detected",
                                norm_id=norm_id,
                                part_id=part_id,
                                filename=filename,
                                type=binary_type
                            )
                            break

                        current = parent

            if binary_map:
                logger.info(
                    "binary_content_extraction_complete",
                    norm_id=norm_id,
                    articles_with_binary=len(binary_map)
                )

            return binary_map

        except Exception as e:
            logger.warning(
                "binary_content_extraction_failed",
                norm_id=norm_id,
                error=str(e)
            )
            return {}

    def extract_treaty_annex(self, xml_content: str, norm_id: int) -> Optional[str]:
        """
        Extract treaty annex content from XML.

        Treaties (Tratados internacionales) often come with the treaty text
        as an annex in the XML. The decree itself is typically short (promulgation),
        while the full treaty text is in an <Anexo> or similar element.

        TODO: This is a PLACEHOLDER that needs investigation of real treaty XML structure.
              Download XML of a treaty (e.g., Tratado de Escazú) and examine:
              - <Anexo> elements
              - <Adjunto> elements
              - Other possible containers for treaty text
              Then implement extraction logic.

        Args:
            xml_content: Raw XML content
            norm_id: Norm ID for logging

        Returns:
            Treaty annex text if found, None otherwise
        """
        try:
            root = ET.fromstring(xml_content)

            # TODO: Implement actual extraction once XML structure is known
            # Possible patterns to investigate:
            # - root.find('.//ns:Anexo', self.NS)
            # - root.find('.//ns:Adjunto', self.NS)
            # - Elements with tipoParte="Anexo" or similar

            logger.info(
                "treaty_annex_extraction_not_implemented",
                norm_id=norm_id,
                note="Placeholder - needs XML structure investigation"
            )

            return None

        except Exception as e:
            logger.warning(
                "treaty_annex_extraction_failed",
                norm_id=norm_id,
                error=str(e)
            )
            return None
