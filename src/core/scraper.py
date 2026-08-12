"""
Async scraper for BCN legal norms.

Features:
- httpx for async HTTP
- tenacity for smart retry
- Rate limiting
- Structured logging
"""

import asyncio
import structlog
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
from typing import Optional, List
from .models import ChileanLegalNorm, ScraperStats
from .parser import BCNHtmlParser
from ..utils.config import ScraperConfig

logger = structlog.get_logger()


class BCNScraper:
    """
    Professional BCN scraper with:
    - Rate limiting (respectful scraping)
    - Retry logic (exponential backoff)
    - Async for performance
    - Realistic headers
    """

    def __init__(self, config: ScraperConfig):
        self.config = config
        self.parser = BCNHtmlParser()
        self.stats = ScraperStats()

        # Async HTTP client with professional configuration
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            limits=httpx.Limits(max_connections=1),  # Rate limiting
            headers={
                "User-Agent": "JurispeedBot/1.0 (Legal Research; +https://jurispeed.ai/bot)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
            follow_redirects=True,
        )

        logger.info(
            "scraper_initialized",
            rate_limit=config.rate_limit_seconds,
            max_retries=config.max_retries,
            timeout=config.timeout_seconds,
        )

    @retry(
        stop=stop_after_attempt(3),  # Will use config.max_retries in production
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(logger, "WARNING"),
    )
    async def _fetch_html(self, norm_id: int) -> Optional[str]:
        """
        Fetch HTML with smart retry (only network errors).

        404 = norm doesn't exist (don't retry)
        Timeout/Network = transient (retry with backoff)

        Returns:
            HTML string or None if failed/not found
        """
        url = f"https://bcn.cl/leychile/navegar?idNorma={norm_id}"

        logger.debug("fetching", norm_id=norm_id, url=url)

        try:
            response = await self.client.get(url)

            # 404 = norm doesn't exist (NOT an error, don't retry)
            if response.status_code == 404:
                logger.info("norm_not_found", norm_id=norm_id, status=404)
                self.stats.skipped_count += 1
                return None

            # Other non-200 status codes
            if response.status_code != 200:
                logger.warning(
                    "http_error",
                    norm_id=norm_id,
                    status_code=response.status_code,
                    reason=response.reason_phrase,
                )
                return None

            # Validate HTML content
            html = response.text
            if len(html) < 500:
                logger.warning("html_too_short", norm_id=norm_id, length=len(html))
                return None

            # Basic HTML validation
            if not ("<html" in html.lower() or "<!doctype" in html.lower()):
                logger.warning("invalid_html", norm_id=norm_id)
                return None

            logger.debug("fetch_success", norm_id=norm_id, html_length=len(html))
            return html

        except httpx.TimeoutException as e:
            logger.warning(
                "timeout",
                norm_id=norm_id,
                timeout=self.config.timeout_seconds,
                error=str(e),
            )
            self.stats.retry_count += 1
            raise  # Let tenacity retry

        except httpx.NetworkError as e:
            logger.warning("network_error", norm_id=norm_id, error=str(e))
            self.stats.retry_count += 1
            raise  # Let tenacity retry

        except Exception as e:
            logger.error(
                "fetch_unexpected_error",
                norm_id=norm_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    async def scrape_one(self, norm_id: int) -> Optional[ChileanLegalNorm]:
        """
        Scrape one norm (fetch + parse + validate).

        Returns:
            Validated ChileanLegalNorm or None if failed
        """
        # Rate limiting
        await asyncio.sleep(self.config.rate_limit_seconds)

        # Fetch HTML
        html = await self._fetch_html(norm_id)
        if html is None:
            self.stats.failed_count += 1
            self.stats.total_processed += 1
            return None

        # Parse + validate
        norm = self.parser.parse(html, norm_id)
        if norm is None:
            self.stats.failed_count += 1
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
    ) -> List[ChileanLegalNorm]:
        """
        Scrape a range of norm IDs.

        Args:
            start: Starting norm ID
            end: Ending norm ID (inclusive)
            on_checkpoint: Callback function(norm_id, stats) called at checkpoints
            checkpoint_every: Checkpoint frequency (uses config if None)

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
            checkpoint_every=checkpoint_freq,
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
        """Graceful cleanup."""
        await self.client.aclose()
        logger.info("scraper_closed", final_stats=self.stats.to_dict())

    def get_stats(self) -> ScraperStats:
        """Get current statistics."""
        return self.stats
