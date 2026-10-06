"""Fetching and extracting the pages behind search results."""

from bot.services.parser.browser import BrowserFetcher
from bot.services.parser.extractor import extract_text
from bot.services.parser.fetcher import PageFetcher
from bot.services.parser.routing import Fetcher, RoutingFetcher, build_fetcher

__all__ = [
    "BrowserFetcher",
    "Fetcher",
    "PageFetcher",
    "RoutingFetcher",
    "build_fetcher",
    "extract_text",
]
