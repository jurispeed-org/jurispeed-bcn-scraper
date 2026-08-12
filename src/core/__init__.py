"""Core scraping and parsing functionality."""

from .models import ChileanLegalNorm, NormType, ScraperStats
from .parser import BCNHtmlParser
from .scraper import BCNScraper
from .scraper_playwright import BCNPlaywrightScraper

__all__ = [
    "ChileanLegalNorm",
    "NormType",
    "ScraperStats",
    "BCNHtmlParser",
    "BCNScraper",
    "BCNPlaywrightScraper",
]
