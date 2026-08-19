"""
Playwright-based scraper for BCN (handles JavaScript SPA).

Uses headless browser to render dynamic content.
"""

import asyncio
import structlog
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeout
from typing import Optional
from core.models import ChileanLegalNorm, ScraperStats
from core.parser import BCNHtmlParser
from utils.config import ScraperConfig

logger = structlog.get_logger()


class BCNPlaywrightScraper:
    """
    BCN scraper using Playwright for JavaScript rendering.

    Features:
    - Headless browser
    - JavaScript execution
    - Retry logic
    - Rate limiting
    """

    def __init__(self, config: ScraperConfig):
        self.config = config
        self.parser = BCNHtmlParser()
        self.stats = ScraperStats()

        self.playwright = None
        self.browser: Optional[Browser] = None

        logger.info(
            "playwright_scraper_initialized",
            rate_limit=config.rate_limit_seconds,
            timeout=config.timeout_seconds,
        )

    async def start(self):
        """Initialize Playwright and browser."""
        logger.info("starting_playwright")

        self.playwright = await async_playwright().start()

        # Launch browser (headless)
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
        )

        logger.info("playwright_started")

    async def _fetch_html(self, norm_id: int) -> Optional[str]:
        """
        Fetch HTML with Playwright (renders JavaScript).

        Args:
            norm_id: BCN norm ID

        Returns:
            Rendered HTML or None if failed
        """
        url = f"https://www.bcn.cl/leychile/navegar?idNorma={norm_id}"

        page: Optional[Page] = None

        try:
            # Create new page
            page = await self.browser.new_page()

            # Set realistic user agent
            await page.set_extra_http_headers({
                'Accept-Language': 'es-CL,es;q=0.9',
            })

            logger.debug("navigating", norm_id=norm_id, url=url)

            # Navigate and wait for content
            response = await page.goto(
                url,
                wait_until='networkidle',  # Wait until no network activity
                timeout=self.config.timeout_seconds * 1000,  # milliseconds
            )

            # Check response status
            if not response:
                logger.warning("no_response", norm_id=norm_id)
                return None

            if response.status == 404:
                logger.info("norm_not_found", norm_id=norm_id)
                self.stats.skipped_count += 1
                return None

            if response.status != 200:
                logger.warning(
                    "http_error",
                    norm_id=norm_id,
                    status=response.status,
                )
                return None

            # Wait for Angular app to render content
            # Look for common BCN content markers
            try:
                # Wait for main content (adjust selector as needed)
                await page.wait_for_selector('body', timeout=5000)

                # Additional wait for dynamic content
                await asyncio.sleep(2)  # Give Angular time to render

            except PlaywrightTimeout:
                logger.warning("content_timeout", norm_id=norm_id)
                # Continue anyway, we might have partial content

            # Get rendered HTML
            html = await page.content()

            # Validate
            if len(html) < 1000:
                logger.warning("html_too_short", norm_id=norm_id, length=len(html))
                return None

            logger.debug("fetch_success", norm_id=norm_id, html_length=len(html))
            return html

        except PlaywrightTimeout as e:
            logger.warning(
                "playwright_timeout",
                norm_id=norm_id,
                timeout=self.config.timeout_seconds,
            )
            self.stats.retry_count += 1
            return None

        except Exception as e:
            logger.error(
                "fetch_error",
                norm_id=norm_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

        finally:
            # Always close page
            if page:
                await page.close()

    async def scrape_one(self, norm_id: int) -> Optional[ChileanLegalNorm]:
        """
        Scrape one norm (fetch + parse + validate).

        Args:
            norm_id: BCN norm ID

        Returns:
            Validated ChileanLegalNorm or None if failed
        """
        # Rate limiting
        await asyncio.sleep(self.config.rate_limit_seconds)

        # Fetch HTML (with retries)
        max_retries = self.config.max_retries
        html = None

        for attempt in range(max_retries):
            html = await self._fetch_html(norm_id)

            if html:
                break

            if attempt < max_retries - 1:
                # Retry with exponential backoff
                wait_time = 2 ** attempt
                logger.info(
                    "retrying_fetch",
                    norm_id=norm_id,
                    attempt=attempt + 1,
                    wait=wait_time,
                )
                await asyncio.sleep(wait_time)

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

        # Filter BCN placeholder pages (invalid IDs)
        # These pages have generic title and minimal content (login form)
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
        """Cleanup Playwright resources."""
        if self.browser:
            await self.browser.close()

        if self.playwright:
            await self.playwright.stop()

        logger.info("playwright_closed", final_stats=self.stats.to_dict())

    def get_stats(self) -> ScraperStats:
        """Get current statistics."""
        return self.stats
