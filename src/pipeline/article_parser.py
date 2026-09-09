"""
Parser for article substructure (incisos, letras, numerales).

Parses the internal structure of a legal article to identify:
- Incisos (paragraphs)
- Numerales (1., 2., 3. or N°1, N°2, etc.)
- Letras (a), b), c) or letra a), letra b), etc.)
"""

import re
from typing import List, Dict, Optional
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()


@dataclass
class SubPart:
    """A sub-part of an article (inciso, numeral, or letra)."""
    type: str  # "inciso", "numeral", "letra"
    number: Optional[int] = None  # For incisos and numerales
    letter: Optional[str] = None  # For letras
    text: str = ""
    start_pos: int = 0  # Character position in original text


class ArticleSubstructureParser:
    """
    Parses the internal structure of legal articles.

    Identifies:
    - Incisos: paragraphs separated by double newline
    - Numerales: 1., 2., 3. or N°1, N°2, etc.
    - Letras: a), b), c) or letra a), letra b), etc.
    """

    def __init__(self):
        # Patterns for numerales (expanded to detect more formats)
        # ORDER MATTERS: more specific patterns first
        self.numeral_patterns = [
            r'^\s*(\d+)º\.-\s+',  # "1º.- texto" (ORDINAL before dot-hyphen, e.g. Constitucion Art 19)
            r'^\s*(\d+)°\.-\s+',  # "1°.- texto" (degree symbol before dot-hyphen)
            r'^\s*(\d+)\.-\s+',  # "1.- texto" (with hyphen - common in Chilean law)
            r'^\s*(\d+)\.º\s+',  # "1.º texto" (MASCULINE ORDINAL - BCN uses this)
            r'^\s*(\d+)\.°\s+',  # "1.° texto" (degree symbol - alternate)
            r'^\s*(\d+)\.ª\s+',  # "1.ª texto" (feminine ordinal)
            r'^\s*(\d+)\.\s+',   # "1. texto" (simple dot)
            r'^\s*(\d+)\)\s+',   # "1) texto" (parenthesis)
            r'^\s*N°\s*(\d+)[\.:\-]?\s+',  # "N°1:" or "N° 1."
            r'^\s*Nº\s*(\d+)[\.:\-]?\s+',  # "Nº1:"
        ]

        # Inline numeral patterns (for cases like "...y 3.- texto")
        # These detect numerals NOT at line start
        self.inline_numeral_patterns = [
            r'\s+y\s+(\d+)º\.-\s+',  # "y 3º.- texto" (ORDINAL before dot-hyphen)
            r'\s+y\s+(\d+)°\.-\s+',  # "y 3°.- texto" (degree symbol before dot-hyphen)
            r',\s*y\s+(\d+)º\.-\s+', # ", y 3º.- texto" (with comma, ORDINAL before dot-hyphen)
            r'\s+y\s+(\d+)\.-\s+',   # "y 3.- texto" (inline after "y")
            r'\s+y\s+(\d+)\.º\s+',   # "y 3.º texto" (MASCULINE ORDINAL)
            r'\s+y\s+(\d+)\.°\s+',   # "y 3.° texto" (degree symbol)
            r'\s+y\s+(\d+)\.ª\s+',   # "y 3.ª texto" (feminine ordinal)
            r',\s*y\s+(\d+)\.-\s+',  # ", y 3.- texto" (with comma)
            r',\s*y\s+(\d+)\.º\s+',  # ", y 3.º texto" (with comma, ORDINAL)
        ]

        # Patterns for letras
        self.letter_patterns = [
            r'^\s*([a-z])\)\s+',  # "a) texto"
            r'^\s*letra\s+([a-z])\)',  # "letra a) texto"
        ]

        # BCN plain-text formatting uses two visual columns: body text on the
        # left (indented ~5 spaces for numerales/letras/incisos) and marginal
        # notes (law citations, "D.O." dates) column-aligned far to the right.
        # A marker preceded by an unusually large run of whitespace is a
        # marginal note that happens to start with "N° X" / "letra x)", not a
        # real subdivision -- e.g. "                    ...   letra b) D.O.
        # 26.08.2005" referencing an amendment law, not a real list item.
        self.MARGIN_NOTE_INDENT_THRESHOLD = 20

    def parse(self, article_text: str) -> List[SubPart]:
        """
        Parse article text into sub-parts.

        ONLY parses explicit structure markers:
        - Numerales: 1., 2., 3. or N°1, N°2, etc.
        - Letras: a), b), c) or letra a), letra b), etc.

        NO parses incisos (double newlines are ambiguous).

        Strategy:
        1. Check if text contains numerales or letras
        2. If yes: split ONLY by those explicit markers
        3. If no: return empty list (article stays complete)

        Args:
            article_text: Full text of the article

        Returns:
            List of SubPart objects (only numerales/letras, never incisos)
        """
        if not article_text or not article_text.strip():
            return []

        # Check if article has explicit structure (numerales or letras)
        has_structure = self._has_numerals_or_letters(article_text)

        if not has_structure:
            # No explicit structure: return empty list
            # Chunker will keep the article complete
            return []

        # Has structure: split by numerales/letras
        subparts = self._split_by_structure(article_text, 0)

        logger.debug(
            "article_substructure_parsed",
            total_subparts=len(subparts),
            types={sp.type for sp in subparts}
        )

        return subparts

    def _has_numerals_or_letters(self, text: str) -> bool:
        """Check if text contains numeral or letra markers (both line-start and inline)."""
        lines = text.split('\n')

        for line in lines:
            # Line-start numerales/letras: reuse the same matchers used for
            # splitting, so marginal-note false positives are excluded here too.
            if self._match_numeral(line) or self._match_letter(line):
                return True

            # Check inline numerales (e.g., "y 3.-" cases)
            for pattern in self.inline_numeral_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    return True

        return False

    def _split_by_structure(self, text: str, base_pos: int) -> List[SubPart]:
        """
        Split text by numerales or letras ONLY.

        Lines before the first numeral/letra are prepended to the first subpart.
        NO incisos are generated.

        Args:
            text: Text to split
            base_pos: Starting position in original article text

        Returns:
            List of SubPart objects (only numerales and letras)
        """
        subparts = []
        lines = text.split('\n')

        # Preprocess: expand lines with inline numerals into multiple lines
        # This handles cases like "1.- texto y 2.- texto y 3.- texto"
        expanded_lines = []
        for line in lines:
            expanded = self._expand_inline_numerals(line)
            expanded_lines.extend(expanded)

        lines = expanded_lines

        current_subpart = None
        current_text_lines = []
        preamble_lines = []  # Lines before first numeral/letra

        for line in lines:
            # Check if this line starts a new numeral
            numeral_match = self._match_numeral(line)
            if numeral_match:
                if current_subpart:
                    current_subpart.text = '\n'.join(current_text_lines).strip()
                    subparts.append(current_subpart)
                    current_text_lines = []

                current_subpart = SubPart(
                    type="numeral",
                    number=numeral_match['number'],
                    start_pos=base_pos
                )

                # If this is the first subpart, prepend preamble
                if preamble_lines:
                    current_text_lines = preamble_lines + [numeral_match['remaining_text']]
                    preamble_lines = []
                else:
                    current_text_lines = [numeral_match['remaining_text']]
                continue

            letter_match = self._match_letter(line)
            if letter_match:
                if current_subpart:
                    current_subpart.text = '\n'.join(current_text_lines).strip()
                    subparts.append(current_subpart)
                    current_text_lines = []

                current_subpart = SubPart(
                    type="letra",
                    letter=letter_match['letter'],
                    start_pos=base_pos
                )

                # If this is the first subpart, prepend preamble
                if preamble_lines:
                    current_text_lines = preamble_lines + [letter_match['remaining_text']]
                    preamble_lines = []
                else:
                    current_text_lines = [letter_match['remaining_text']]
                continue

            if current_subpart:
                current_text_lines.append(line)
            else:
                preamble_lines.append(line)

        if current_subpart:
            current_subpart.text = '\n'.join(current_text_lines).strip()
            subparts.append(current_subpart)

        return subparts

    def _is_marginal_note(self, line: str, match) -> bool:
        """
        Check if a matched marker is a false positive rather than a real
        subdivision marker in the body text. Two known false-positive shapes:

        1. Marginal notes: BCN's plain-text formatting uses a right-hand
           column (law citations, "D.O." dates) that is column-aligned far to
           the right of the body text -- a marker preceded by an unusually
           large run of whitespace belongs to that column, not the body.
        2. Word-wrapped continuations: a real marker always starts a new
           paragraph, indented by BCN's standard ~5-space paragraph indent.
           Zero leading whitespace means the line is a wrapped continuation
           of the previous line's sentence (e.g. "...senalado en la\nletra
           a), debera aplicarse..."), not a new subdivision.
        """
        leading_whitespace = len(line) - len(line.lstrip(' '))

        if leading_whitespace == 0:
            return True

        if leading_whitespace >= self.MARGIN_NOTE_INDENT_THRESHOLD:
            return True

        remaining_text = line[match.end():].strip()
        if remaining_text.startswith('D.O.') or remaining_text.startswith('D O'):
            return True

        return False

    def _match_numeral(self, line: str) -> Optional[Dict]:
        """
        Check if line starts with a numeral marker.

        Returns:
            Dict with 'number', 'mark' (literal), and 'remaining_text', or None
        """
        for pattern in self.numeral_patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                if self._is_marginal_note(line, match):
                    continue

                number = int(match.group(1))
                remaining_text = line[match.end():].strip()
                # Preserve literal mark from text
                mark_literal = match.group(0).strip()  # "1.º " -> "1.º"

                return {
                    'number': number,
                    'mark': mark_literal,
                    'remaining_text': remaining_text
                }

        return None

    def _match_inline_numeral(self, line: str) -> Optional[Dict]:
        """
        Check if line contains an inline numeral marker (e.g., "y 3.-").

        Returns:
            Dict with 'number', 'remaining_text', and 'prefix_text', or None
        """
        for pattern in self.inline_numeral_patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                number = int(match.group(1))
                # Text before the inline numeral
                prefix_text = line[:match.start()].strip()
                # Text after the inline numeral marker
                remaining_text = line[match.end():].strip()
                return {
                    'number': number,
                    'remaining_text': remaining_text,
                    'prefix_text': prefix_text
                }

        return None

    def _expand_inline_numerals(self, line: str) -> List[str]:
        """
        Expand a line containing inline numerals into multiple lines.

        For example:
            "1.- texto y 2.- texto y 3.- texto"
        becomes:
            ["1.- texto", "2.- texto", "3.- texto"]

        This preprocessing step allows the main splitting logic to handle
        inline numerals the same way as line-start numerals.

        Args:
            line: Line to expand

        Returns:
            List of lines (original line if no inline numerals, split lines if found)
        """
        # Check if line has any inline numerals
        has_inline = False
        for pattern in self.inline_numeral_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                has_inline = True
                break

        if not has_inline:
            return [line]

        # Split by inline numerals
        result = []
        remaining = line

        while remaining:
            # Try to match an inline numeral
            inline_match = self._match_inline_numeral(remaining)

            if inline_match:
                # Found inline numeral
                # Add prefix (if non-empty) as separate line
                if inline_match['prefix_text']:
                    result.append(inline_match['prefix_text'])

                # Continue with the numeral part + remaining text
                # Format as "N.- remaining_text" so main logic can match it
                numeral_line = f"{inline_match['number']}.- {inline_match['remaining_text']}"
                remaining = numeral_line

                # Check if this numeral_line has MORE inline numerals
                has_more_inline = False
                for pattern in self.inline_numeral_patterns:
                    if re.search(pattern, numeral_line, re.IGNORECASE):
                        has_more_inline = True
                        break

                if not has_more_inline:
                    result.append(numeral_line)
                    break
            else:
                result.append(remaining)
                break

        return result if result else [line]

    def _match_letter(self, line: str) -> Optional[Dict]:
        """
        Check if line starts with a letra marker.

        Returns:
            Dict with 'letter', 'mark' (literal), and 'remaining_text', or None
        """
        for pattern in self.letter_patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                if self._is_marginal_note(line, match):
                    continue

                letter = match.group(1)
                remaining_text = line[match.end():].strip()
                # Preserve literal mark from text
                mark_literal = match.group(0).strip()  # "a) " -> "a)"
                return {
                    'letter': letter,
                    'mark': mark_literal,
                    'remaining_text': remaining_text
                }

        return None

    def _is_article_header(self, text: str) -> bool:
        """
        Check if text is just an article header (e.g., "ARTÍCULO 1.", "ART. 2.").

        Article headers are typically:
        - Very short (< 50 characters)
        - Contain only "ARTÍCULO" or "ART" + number + optional punctuation
        - No substantial content

        Args:
            text: Text to check

        Returns:
            True if this is an article header only
        """
        text_clean = text.strip()

        # Must be short
        if len(text_clean) > 50:
            return False

        # Pattern for article headers
        # Matches: "ARTÍCULO 1.", "ART. 2.", "ARTICULO 3°.-", etc.
        header_patterns = [
            r'^ARTÍ?CULO\s+\d+[°\.º\-]*$',
            r'^ART\.?\s+\d+[°\.º\-]*$',
        ]

        for pattern in header_patterns:
            if re.match(pattern, text_clean, re.IGNORECASE):
                return True

        return False


def parse_article_substructure(article_text: str) -> List[SubPart]:
    """
    DEPRECATED: Use extract_subdivisions_metadata() instead.

    Convenience function to parse article substructure.

    Args:
        article_text: Full text of the article

    Returns:
        List of SubPart objects (numerales, letras only - NO incisos)
    """
    parser = ArticleSubstructureParser()
    return parser.parse(article_text)


def extract_subdivisions_metadata(article_text: str) -> List[Dict]:
    """
    Extract subdivisions (numerales, letras) as metadata with positions.

    Does NOT split the article - just identifies where subdivisions are.
    Returns position info for highlighting/navigation.

    Args:
        article_text: Full text of the article

    Returns:
        List of dicts with subdivision metadata:
        [
            {
                "type": "numeral",
                "mark": "1.-",
                "start": 291,
                "end": 404
            },
            ...
        ]
    """
    if not article_text or not article_text.strip():
        return []

    subdivisions = []
    parser = ArticleSubstructureParser()

    # Check if article has explicit structure
    has_structure = parser._has_numerals_or_letters(article_text)
    if not has_structure:
        return []

    lines = article_text.split('\n')
    current_pos = 0
    current_numeral_mark = None  # tracks the most recent numeral, so nested letras keep it in context

    for line in lines:
        line_len = len(line) + 1  # +1 for newline

        # Check for numeral
        numeral_match = parser._match_numeral(line)
        if numeral_match:
            # Use literal mark from text, not reconstructed
            current_numeral_mark = numeral_match['mark']
            subdivisions.append({
                "type": "numeral",
                "mark": numeral_match['mark'],  # "1.º" as it appears in XML
                "number": numeral_match['number'],
                "start": current_pos
            })

        # Check for letra
        letter_match = parser._match_letter(line)
        if letter_match:
            # Use literal mark from text, not reconstructed
            letra_subdivision = {
                "type": "letra",
                "mark": letter_match['mark'],  # "a)" as it appears in XML
                "letter": letter_match['letter'],
                "start": current_pos
            }
            # A letra nested under a numeral (e.g. "1°.- ... a) ...") must keep the
            # numeral in its structural context, otherwise the numeral is silently
            # dropped from the chunk's header/citation.
            if current_numeral_mark:
                letra_subdivision["parent_numeral"] = current_numeral_mark
            subdivisions.append(letra_subdivision)

        current_pos += line_len

    # Calculate end positions (next subdivision start or paragraph break)
    for i, subdivision in enumerate(subdivisions):
        if i < len(subdivisions) - 1:
            # Not the last subdivision: ends where next one starts
            subdivision["end"] = subdivisions[i + 1]["start"] - 1
        else:
            # Last subdivision: find actual end (not article end)
            # Look for paragraph break after this subdivision's start
            start_pos = subdivision["start"]

            # Find the next paragraph break (double newline or newline + indent)
            # This signals transition from subdivision to article-level content
            end_pos = _find_subdivision_end(article_text, start_pos)

            subdivision["end"] = end_pos

    return subdivisions


def _find_subdivision_end(text: str, start_pos: int) -> int:
    """
    Find the actual end of a subdivision's text.

    Looks for paragraph breaks that signal the transition from
    subdivision content to article-level content.

    Args:
        text: Full article text
        start_pos: Start position of this subdivision

    Returns:
        End position of the subdivision (exclusive)
    """
    # Look for paragraph breaks after the subdivision starts
    # Pattern 1: Double newline (explicit paragraph break)
    # Pattern 2: Newline + significant indent (>= 5 spaces)

    search_start = start_pos + 50  # Skip the subdivision header itself

    # Search for double newline
    double_newline = text.find('\n\n', search_start)

    # Search for newline + indent pattern
    # This pattern matches: \n followed by 5+ spaces, followed by uppercase letter or "En caso"
    # (common patterns for article-level incisos)
    indent_pattern = r'\n {5,}[A-ZÁ]'  # Newline + 5+ spaces + uppercase letter
    match = re.search(indent_pattern, text[search_start:])
    indent_break = match.start() + search_start if match else -1

    # Take the nearest break (if any exist)
    breaks = [b for b in [double_newline, indent_break] if b > search_start]

    if breaks:
        return min(breaks)
    else:
        # No paragraph break found: subdivision extends to end of article
        return len(text)
