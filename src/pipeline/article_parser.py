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
        # Patterns for numerales
        self.numeral_patterns = [
            r'^\s*(\d+)\.\s+',  # "1. texto"
            r'^\s*(\d+)\)\s+',  # "1) texto"
            r'^\s*N°\s*(\d+)[\.:\-]?\s+',  # "N°1:" or "N° 1."
            r'^\s*Nº\s*(\d+)[\.:\-]?\s+',  # "Nº1:"
        ]

        # Patterns for letras
        self.letra_patterns = [
            r'^\s*([a-z])\)\s+',  # "a) texto"
            r'^\s*letra\s+([a-z])\)',  # "letra a) texto"
        ]

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
        has_structure = self._has_numerales_or_letras(article_text)

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

    def _has_numerales_or_letras(self, text: str) -> bool:
        """Check if text contains numeral or letra markers."""
        lines = text.split('\n')

        for line in lines:
            # Check numerales
            for pattern in self.numeral_patterns:
                if re.match(pattern, line, re.IGNORECASE):
                    return True

            # Check letras
            for pattern in self.letra_patterns:
                if re.match(pattern, line, re.IGNORECASE):
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

        current_subpart = None
        current_text_lines = []
        preamble_lines = []  # Lines before first numeral/letra

        for line in lines:
            # Check if this line starts a new numeral
            numeral_match = self._match_numeral(line)
            if numeral_match:
                # Save previous subpart
                if current_subpart:
                    current_subpart.text = '\n'.join(current_text_lines).strip()
                    subparts.append(current_subpart)
                    current_text_lines = []

                # Start new numeral
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

            # Check if this line starts a new letra
            letra_match = self._match_letra(line)
            if letra_match:
                # Save previous subpart
                if current_subpart:
                    current_subpart.text = '\n'.join(current_text_lines).strip()
                    subparts.append(current_subpart)
                    current_text_lines = []

                # Start new letra
                current_subpart = SubPart(
                    type="letra",
                    letter=letra_match['letter'],
                    start_pos=base_pos
                )

                # If this is the first subpart, prepend preamble
                if preamble_lines:
                    current_text_lines = preamble_lines + [letra_match['remaining_text']]
                    preamble_lines = []
                else:
                    current_text_lines = [letra_match['remaining_text']]
                continue

            # Regular line
            if current_subpart:
                # Already in a subpart: add line to it
                current_text_lines.append(line)
            else:
                # Before first numeral/letra: accumulate as preamble
                preamble_lines.append(line)

        # Save last subpart
        if current_subpart:
            current_subpart.text = '\n'.join(current_text_lines).strip()
            subparts.append(current_subpart)

        return subparts

    def _match_numeral(self, line: str) -> Optional[Dict]:
        """
        Check if line starts with a numeral marker.

        Returns:
            Dict with 'number' and 'remaining_text', or None
        """
        for pattern in self.numeral_patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                number = int(match.group(1))
                remaining_text = line[match.end():].strip()
                return {
                    'number': number,
                    'remaining_text': remaining_text
                }

        return None

    def _match_letra(self, line: str) -> Optional[Dict]:
        """
        Check if line starts with a letra marker.

        Returns:
            Dict with 'letter' and 'remaining_text', or None
        """
        for pattern in self.letra_patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                letter = match.group(1)
                remaining_text = line[match.end():].strip()
                return {
                    'letter': letter,
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
    has_structure = parser._has_numerales_or_letras(article_text)
    if not has_structure:
        return []

    lines = article_text.split('\n')
    current_pos = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline

        # Check for numeral
        numeral_match = parser._match_numeral(line)
        if numeral_match:
            # Found start of a subdivision
            # We'll need to find where it ends (next subdivision or end of text)
            subdivisions.append({
                "type": "numeral",
                "mark": f"{numeral_match['number']}.-",
                "number": numeral_match['number'],
                "start": current_pos
            })

        # Check for letra
        letra_match = parser._match_letra(line)
        if letra_match:
            subdivisions.append({
                "type": "letra",
                "mark": f"{letra_match['letter']})",
                "letter": letra_match['letter'],
                "start": current_pos
            })

        current_pos += line_len

    # Calculate end positions (next subdivision start or text end)
    for i, subdivision in enumerate(subdivisions):
        if i < len(subdivisions) - 1:
            subdivision["end"] = subdivisions[i + 1]["start"] - 1
        else:
            subdivision["end"] = len(article_text)

    return subdivisions
