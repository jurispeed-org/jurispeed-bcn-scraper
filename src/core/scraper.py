"""
Playwright-based scraper for BCN (handles JavaScript SPA).

Uses headless browser to render dynamic content.
"""

import asyncio
import random
import structlog
import aiohttp
from typing import Optional
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
        # Increase timeout by 15 seconds per attempt
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
        # Retryable status codes
        retryable_codes = {408, 429, 500, 502, 503, 504}

        if status_code and status_code in retryable_codes:
            return True

        # Non-retryable status codes
        permanent_codes = {400, 401, 403, 404, 405, 410}
        if status_code and status_code in permanent_codes:
            return False

        # Timeout errors are retryable
        if isinstance(error, asyncio.TimeoutError):
            return True

        # By default, retry on unknown errors
        return True

    async def start(self):
        """Initialize scraper (no-op for backward compatibility)."""
        logger.info("scraper_ready", mode="xml_only")

    async def _fetch_xml(self, norm_id: int, timeout: Optional[int] = None) -> Optional[str]:
        """
        Fetch XML from BCN's obtxml service using direct HTTP.

        Uses aiohttp instead of Playwright for better performance and reliability.
        XML is static content - no need for browser rendering.

        Args:
            norm_id: BCN norm ID
            timeout: Custom timeout in seconds (overrides config)

        Returns:
            XML content or None if failed
        """
        url = f"http://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={norm_id}"
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
                    # Check status
                    if response.status == 401:
                        logger.warning("xml_unauthorized", norm_id=norm_id)
                        return None

                    if response.status == 404:
                        logger.info("xml_not_found", norm_id=norm_id)
                        return None

                    if response.status != 200:
                        logger.warning("xml_http_error", norm_id=norm_id, status=response.status)
                        return None

                    # Get XML content
                    xml_content = await response.text()

                    # Validate it's actually XML
                    if not xml_content.strip().startswith('<?xml'):
                        logger.warning("not_xml_response", norm_id=norm_id, preview=xml_content[:200])
                        return None

                    if len(xml_content) < 500:
                        logger.warning("xml_too_short", norm_id=norm_id, length=len(xml_content))
                        return None

                    logger.debug("xml_fetch_success", norm_id=norm_id, xml_length=len(xml_content))
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
            # Progressive timeout
            timeout = self._calculate_timeout(attempt)

            try:
                xml = await self._fetch_xml(norm_id, timeout=timeout)

                if xml:
                    # Parse XML
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

            # Retry logic
            if attempt < max_retries - 1 and norm is None:
                # Calculate backoff with full jitter
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

        # Final check
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

        # Success
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

            # Checkpoint callback
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
