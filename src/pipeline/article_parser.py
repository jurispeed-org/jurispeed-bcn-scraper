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

        Strategy:
        1. Split by double newline (incisos)
        2. Within each inciso, check for numerales or letras
        3. If found, split further

        Args:
            article_text: Full text of the article

        Returns:
            List of SubPart objects
        """
        if not article_text or not article_text.strip():
            return []

        subparts = []

        # Split by double newline (potential incisos)
        paragraphs = article_text.split('\n\n')

        current_pos = 0
        for para_idx, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                current_pos += 2  # Account for \n\n
                continue

            # Check if this paragraph contains numerales or letras
            has_structure = self._has_numerales_or_letras(paragraph)

            if has_structure:
                # Split by numerales/letras
                sub_subparts = self._split_by_structure(paragraph, current_pos)
                subparts.extend(sub_subparts)
            else:
                # Regular inciso (no internal structure)
                subparts.append(SubPart(
                    type="inciso",
                    number=para_idx + 1,
                    text=paragraph,
                    start_pos=current_pos
                ))

            current_pos += len(paragraph) + 2  # Include \n\n

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
        Split text by numerales or letras.

        Args:
            text: Text to split
            base_pos: Starting position in original article text

        Returns:
            List of SubPart objects
        """
        subparts = []
        lines = text.split('\n')

        current_subpart = None
        current_text_lines = []

        for line in lines:
            # Check if this line starts a new numeral
            numeral_match = self._match_numeral(line)
            if numeral_match:
                # Save previous subpart
                if current_subpart:
                    current_subpart.text = '\n'.join(current_text_lines).strip()
                    subparts.append(current_subpart)

                # Start new numeral
                current_subpart = SubPart(
                    type="numeral",
                    number=numeral_match['number'],
                    start_pos=base_pos
                )
                current_text_lines = [numeral_match['remaining_text']]
                continue

            # Check if this line starts a new letra
            letra_match = self._match_letra(line)
            if letra_match:
                # Save previous subpart
                if current_subpart:
                    current_subpart.text = '\n'.join(current_text_lines).strip()
                    subparts.append(current_subpart)

                # Start new letra
                current_subpart = SubPart(
                    type="letra",
                    letter=letra_match['letter'],
                    start_pos=base_pos
                )
                current_text_lines = [letra_match['remaining_text']]
                continue

            # Regular line, add to current subpart
            if current_subpart:
                current_text_lines.append(line)
            else:
                # No structure found yet, treat as inciso
                if not current_subpart:
                    current_subpart = SubPart(
                        type="inciso",
                        number=1,
                        start_pos=base_pos
                    )
                current_text_lines.append(line)

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


def parse_article_substructure(article_text: str) -> List[SubPart]:
    """
    Convenience function to parse article substructure.

    Args:
        article_text: Full text of the article

    Returns:
        List of SubPart objects (incisos, numerales, letras)
    """
    parser = ArticleSubstructureParser()
    return parser.parse(article_text)
