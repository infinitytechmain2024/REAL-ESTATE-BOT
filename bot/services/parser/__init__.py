"""Fetching and extracting the pages behind search results."""

from bot.services.parser.extractor import extract_text
from bot.services.parser.fetcher import PageFetcher

__all__ = ["PageFetcher", "extract_text"]
