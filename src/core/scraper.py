"""
Playwright-based scraper for BCN (handles JavaScript SPA).

Uses headless browser to render dynamic content.
"""

import asyncio
import html
import random
import re
import structlog
import aiohttp
from typing import Dict, Optional, Set
from core.models import ChileanLegalNorm, ScraperStats
from core.xml_parser import BCNXMLParser
from utils.config import ScraperConfig

logger = structlog.get_logger()


class BCNPlaywrightScraper:
    """
    BCN scraper using aiohttp for XML-only fetching.

    Features:
    - Direct HTTP requests (no browser overhead)
    - XML-only parsing (quality over coverage)
    - Exponential backoff with jitter
    - Progressive timeouts
    - Retry logic
    - Rate limiting
    """

    # BCN renders the XML through a fixed-width layout engine that overflows on
    # articles carrying dense amendment history, silently cutting <Texto>
    # mid-word (e.g. Art. 107 of the Constitution ends at "disposició").
    # Which articles overflow depends on whether notaPIE=1 injects note markers,
    # so the two variants of the same norm are truncated in DIFFERENT articles:
    # with notes, Arts. 19/107 and transitoria 41 are cut; without notes, the
    # transitory Art. 144 and transitoria QUINTA are cut instead. Neither
    # variant is authoritative, so we keep the note-bearing one as the base
    # (its notes carry real legal cross-references) and repair the casualties
    # from the note-free variant.
    _STRUCTURE_PATTERN = re.compile(
        r'<EstructuraFuncional(?P<attrs>[^>]*)>\s*<Texto>(?P<text>.*?)</Texto>',
        re.DOTALL,
    )
    _ATTR_PATTERN = re.compile(r'(\w+)="([^"]*)"')
    _REPAIRABLE_PART_TYPES = {"Artículo", "Disposición Transitoria"}
    # The truncation rules themselves (note block, sentence terminators, repeal markers)
    # live in BCNXMLParser as of PR5, so this repair path and the per-part flag the
    # parser stores cannot disagree. See BCNXMLParser.is_part_truncated().

    def __init__(self, config: ScraperConfig):
        self.config = config
        self.xml_parser = BCNXMLParser()
        self.stats = ScraperStats()
        self.last_error = None  # Track last error for failure tracking

        logger.info(
            "scraper_initialized",
            mode="xml_only_aiohttp",
            rate_limit=config.rate_limit_seconds,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    def _calculate_backoff(self, attempt: int) -> float:
        """
        Calculate backoff time with Full Jitter (AWS best practice).

        Full Jitter: random.uniform(0, min(max_backoff, base * 2^attempt))

        Args:
            attempt: Current retry attempt (0-indexed)

        Returns:
            Wait time in seconds
        """
        exponential = self.config.retry_backoff_base * (2 ** attempt)
        max_wait = min(self.config.retry_backoff_max, exponential)

        # Full jitter: randomize completely
        return random.uniform(0, max_wait)

    def _calculate_timeout(self, attempt: int) -> int:
        """
        Calculate progressive timeout (increases with retries).

        Args:
            attempt: Current retry attempt (0-indexed)

        Returns:
            Timeout in seconds
        """
        timeout = self.config.timeout_seconds + (attempt * 15)
        return min(timeout, self.config.max_timeout_seconds)

    def _should_retry_error(self, status_code: Optional[int], error: Optional[Exception]) -> bool:
        """
        Determine if error is retryable.

        Retry:
        - HTTP 408 (Request Timeout)
        - HTTP 429 (Too Many Requests)
        - HTTP 500, 502, 503, 504 (Server Errors)
        - Network timeouts
        - Connection errors

        Don't retry:
        - HTTP 400, 401, 403, 404 (Client Errors)
        - HTTP 405, 410 (Permanent)

        Args:
            status_code: HTTP status code if available
            error: Exception if available

        Returns:
            True if should retry
        """
        retryable_codes = {408, 429, 500, 502, 503, 504}
        if status_code and status_code in retryable_codes:
            return True

        permanent_codes = {400, 401, 403, 404, 405, 410}
        if status_code and status_code in permanent_codes:
            return False

        if isinstance(error, asyncio.TimeoutError):
            return True

        # By default, retry on unknown errors
        return True

    async def start(self):
        """Initialize scraper (no-op for backward compatibility)."""
        logger.info("scraper_ready", mode="xml_only")

    def _split_note_block(self, text: str) -> tuple:
        """Splits an article's <Texto> into (body, editorial_note_block)."""
        return self.xml_parser._split_note_block(text)

    def _is_truncated(self, raw_text: str) -> bool:
        """
        True if an article's text was cut off mid-sentence.

        PR5 moved the rules to BCNXMLParser.is_part_truncated() so the repair path here
        and the per-part instrumentation in the parser cannot drift apart. Behavior is
        unchanged; this stays as the name the repair helpers (and the audit scripts in
        .audit/) already call.
        """
        return self.xml_parser.is_part_truncated(raw_text)

    def _iter_repairable_articles(self, xml_content: str):
        """Yields (part_id, match) for article-like parts that can be repaired."""
        for match in self._STRUCTURE_PATTERN.finditer(xml_content):
            # BCN escapes non-ASCII in attributes too (tipoParte="Art&#237;culo")
            attrs = {
                name: html.unescape(value)
                for name, value in self._ATTR_PATTERN.findall(match.group("attrs"))
            }
            part_id = attrs.get("idParte")
            if part_id and attrs.get("tipoParte") in self._REPAIRABLE_PART_TYPES:
                yield part_id, match

    def _find_truncated_articles(self, xml_content: str) -> Set[str]:
        return {
            part_id
            for part_id, match in self._iter_repairable_articles(xml_content)
            if self._is_truncated(match.group("text"))
        }

    def _article_bodies(self, xml_content: str) -> Dict[str, str]:
        return {
            part_id: match.group("text")
            for part_id, match in self._iter_repairable_articles(xml_content)
        }

    def _merge_article_bodies(
        self, base_xml: str, donor_bodies: Dict[str, str], truncated: Set[str], norm_id: int
    ) -> str:
        """
        Replaces each truncated article body in base_xml with the donor's version,
        keeping the base's editorial note block (the donor has no notes).

        Only substitutes when the donor is actually complete and longer, so a
        donor that is truncated at the same place leaves the base untouched.
        """
        pieces = []
        cursor = 0
        repaired, unrecoverable = [], []

        for part_id, match in self._iter_repairable_articles(base_xml):
            if part_id not in truncated:
                continue

            donor_text = donor_bodies.get(part_id)
            if donor_text is None:
                unrecoverable.append(part_id)
                continue

            donor_body, _ = self._split_note_block(donor_text)
            base_body, base_note = self._split_note_block(match.group("text"))

            if self._is_truncated(donor_body) or len(donor_body) <= len(base_body):
                unrecoverable.append(part_id)
                continue

            start, end = match.span("text")
            pieces.append(base_xml[cursor:start])
            pieces.append(donor_body + base_note)
            cursor = end
            repaired.append(part_id)

        if not repaired:
            logger.warning(
                "xml_truncation_unrecoverable",
                norm_id=norm_id,
                part_ids=sorted(unrecoverable),
                reason="donor variant truncated at the same point (BCN source defect)",
            )
            return base_xml

        pieces.append(base_xml[cursor:])
        logger.info(
            "xml_truncation_repaired",
            norm_id=norm_id,
            repaired=sorted(repaired),
            unrecoverable=sorted(unrecoverable),
        )
        return "".join(pieces)

    async def _repair_truncated_articles(
        self, norm_id: int, xml_content: str, timeout: Optional[int]
    ) -> str:
        """
        Detects articles truncated by BCN's renderer and patches them from the
        note-free variant of the same norm.

        Costs one extra request, and only for norms that actually have a
        truncation, so most documents are unaffected.
        """
        truncated = self._find_truncated_articles(xml_content)
        if not truncated:
            return xml_content

        logger.info("xml_truncation_detected", norm_id=norm_id, part_ids=sorted(truncated))

        donor_xml = await self._fetch_xml(norm_id, timeout=timeout, include_notes=False)
        if not donor_xml:
            logger.warning("xml_donor_fetch_failed", norm_id=norm_id, part_ids=sorted(truncated))
            return xml_content

        return self._merge_article_bodies(
            xml_content, self._article_bodies(donor_xml), truncated, norm_id
        )

    async def _fetch_xml(
        self, norm_id: int, timeout: Optional[int] = None, include_notes: bool = True
    ) -> Optional[str]:
        """
        Fetch XML from BCN's obtxml service using direct HTTP.

        Uses aiohttp instead of Playwright for better performance and reliability.
        XML is static content - no need for browser rendering.

        Args:
            norm_id: BCN norm ID
            timeout: Custom timeout in seconds (overrides config)
            include_notes: Request editorial notes (notaPIE=1) and repair any
                article BCN truncates as a side effect. Set False to fetch the
                note-free variant used as the repair donor (avoids recursion).

        Returns:
            XML content or None if failed
        """
        url = f"http://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={norm_id}"
        if include_notes:
            url += "&notaPIE=1"
        effective_timeout = timeout or self.config.timeout_seconds

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Referer': 'https://www.bcn.cl/',
            'Accept': 'application/xml,text/xml,*/*',
            'Accept-Language': 'es-CL,es;q=0.9',
        }

        try:
            logger.debug("fetching_xml_http", norm_id=norm_id, url=url, timeout=effective_timeout)

            # Use aiohttp for direct HTTP GET (much faster than Playwright)
            timeout_config = aiohttp.ClientTimeout(total=effective_timeout)

            async with aiohttp.ClientSession(timeout=timeout_config) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 401:
                        logger.warning("xml_unauthorized", norm_id=norm_id)
                        return None

                    if response.status == 404:
                        logger.info("xml_not_found", norm_id=norm_id)
                        return None

                    if response.status != 200:
                        logger.warning("xml_http_error", norm_id=norm_id, status=response.status)
                        return None

                    xml_content = await response.text()

                    if not xml_content.strip().startswith('<?xml'):
                        logger.warning("not_xml_response", norm_id=norm_id, preview=xml_content[:200])
                        return None

                    if len(xml_content) < 500:
                        logger.warning("xml_too_short", norm_id=norm_id, length=len(xml_content))
                        return None

                    logger.debug("xml_fetch_success", norm_id=norm_id, xml_length=len(xml_content))

                    xml_content = self._repair_notes_in_binary_tags(norm_id, xml_content)

                    if include_notes:
                        xml_content = await self._repair_truncated_articles(
                            norm_id, xml_content, timeout
                        )

                    return xml_content

        except asyncio.TimeoutError:
            logger.warning("xml_timeout", norm_id=norm_id, timeout=effective_timeout)
            return None

        except aiohttp.ClientError as e:
            logger.warning("xml_client_error", norm_id=norm_id, error=str(e), error_type=type(e).__name__)
            return None

        except Exception as e:
            logger.error("xml_fetch_unexpected_error", norm_id=norm_id, error=str(e), error_type=type(e).__name__)
            return None

    def _repair_notes_in_binary_tags(self, norm_id: int, xml_content: str) -> str:
        """
        Remove margin-note text that BCN injects inside <aem:ArchivoBinario> tags.

        BCN renders the XML through a fixed-width layout that writes margin notes
        ("NOTA", "NOTA 1") in a right-hand column. When a norm carries embedded
        scanned attachments, that column lands in the middle of the attachment's
        opening tag:

            <aem:ArchivoBinario xmlns:aem="..."            NOTA 1
            SchemaVersion="1.0"><aem:Nombre>dto232-p19-p21.jpeg</aem:Nombre>

        The stray token makes the whole document not well-formed, so the norm
        fails to parse even though every article is intact - and since the
        attachments are base64 images (up to 96% of the payload) the loss is
        entirely avoidable. Only the note tokens inside those opening tags are
        removed, so the attachments themselves survive for
        `extract_binary_content()` to map onto their idParte.

        Args:
            norm_id: BCN norm ID (for logging)
            xml_content: Raw XML as fetched

        Returns:
            XML with the injected note tokens removed from binary tags
        """
        repaired_count = 0

        def strip_notes(match) -> str:
            nonlocal repaired_count
            tag = match.group(0)
            if "NOTA" not in tag:
                return tag
            repaired_count += 1
            return re.sub(r"\s*NOTA(\s+\d+)?\s*", " ", tag)

        xml_content = re.sub(
            r"<aem:ArchivoBinario\b[^>]*>", strip_notes, xml_content
        )

        if repaired_count:
            logger.info(
                "binary_tag_notes_repaired",
                norm_id=norm_id,
                tags_repaired=repaired_count,
            )

        return xml_content

    async def scrape_one(self, norm_id: int) -> Optional[ChileanLegalNorm]:
        """
        Scrape one norm from XML only (no HTML fallback).

        Uses exponential backoff with full jitter and progressive timeouts.
        HTML fallback removed to ensure data quality - better no data than bad data.

        Args:
            norm_id: BCN norm ID

        Returns:
            Validated ChileanLegalNorm from XML or None if failed
        """
        # Rate limiting
        await asyncio.sleep(self.config.rate_limit_seconds)

        max_retries = self.config.max_retries
        norm = None
        last_error = None

        # XML ONLY - No HTML fallback
        logger.debug("attempting_xml_only", norm_id=norm_id, max_attempts=max_retries)

        for attempt in range(max_retries):
            timeout = self._calculate_timeout(attempt)

            try:
                xml = await self._fetch_xml(norm_id, timeout=timeout)

                if xml:
                    norm = self.xml_parser.parse(xml, norm_id)
                    if norm:
                        logger.info(
                            "xml_success",
                            norm_id=norm_id,
                            attempts=attempt + 1,
                            source="xml"
                        )
                        self.last_error = None  # Clear error on success
                        break
                    else:
                        logger.warning("xml_parse_failed", norm_id=norm_id)
                        last_error = "XML parse failed (validation error)"
                else:
                    last_error = "XML fetch returned None"

            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)}"
                logger.warning(
                    "xml_fetch_exception",
                    norm_id=norm_id,
                    attempt=attempt + 1,
                    error=last_error
                )

            if attempt < max_retries - 1 and norm is None:
                wait_time = self._calculate_backoff(attempt)

                logger.info(
                    "retrying_xml",
                    norm_id=norm_id,
                    attempt=attempt + 1,
                    max_attempts=max_retries,
                    wait_seconds=f"{wait_time:.2f}",
                    next_timeout=self._calculate_timeout(attempt + 1)
                )

                await asyncio.sleep(wait_time)

        if norm is None:
            logger.error(
                "scrape_failed_xml_only",
                norm_id=norm_id,
                total_attempts=max_retries,
                last_error=last_error,
                note="No HTML fallback - data quality over coverage"
            )
            self.stats.failed_count += 1
            self.last_error = last_error or "Unknown error"
            self.stats.total_processed += 1
            return None

        # Filter BCN placeholder pages
        if (len(norm.full_content) < 500 and
            "Biblioteca del Congreso Nacional" in norm.title):
            logger.info(
                "skipping_bcn_placeholder",
                norm_id=norm_id,
                content_length=len(norm.full_content),
            )
            self.stats.skipped_count += 1
            self.stats.total_processed += 1
            return None

        self.stats.success_count += 1
        self.stats.total_processed += 1

        return norm

    async def scrape_range(
        self,
        start: int,
        end: int,
        on_checkpoint: Optional[callable] = None,
        checkpoint_every: Optional[int] = None,
    ) -> list:
        """
        Scrape a range of norm IDs.

        Args:
            start: Starting norm ID
            end: Ending norm ID (inclusive)
            on_checkpoint: Callback function(norm_id, stats)
            checkpoint_every: Checkpoint frequency

        Returns:
            List of successfully scraped norms
        """
        results = []
        checkpoint_freq = checkpoint_every or self.config.checkpoint_every

        logger.info(
            "scrape_range_start",
            range_start=start,
            range_end=end,
            total_range=end - start + 1,
        )

        for norm_id in range(start, end + 1):
            norm = await self.scrape_one(norm_id)

            if norm:
                results.append(norm)

            if (norm_id - start + 1) % checkpoint_freq == 0:
                progress_pct = ((norm_id - start + 1) / (end - start + 1)) * 100

                logger.info(
                    "checkpoint",
                    norm_id=norm_id,
                    progress=f"{norm_id - start + 1}/{end - start + 1}",
                    progress_pct=f"{progress_pct:.1f}%",
                    stats=self.stats.to_dict(),
                )

                if on_checkpoint:
                    await on_checkpoint(norm_id, self.stats)

        logger.info(
            "scrape_range_complete",
            range_start=start,
            range_end=end,
            final_stats=self.stats.to_dict(),
        )

        return results

    async def close(self):
        """Cleanup resources (no-op for backward compatibility)."""
        logger.info("scraper_closed", final_stats=self.stats.to_dict())

    def get_stats(self) -> ScraperStats:
        """Get current statistics."""
        return self.stats
