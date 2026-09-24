"""Configuration-only health check for the one-shot worker image."""

from .settings import ScraplingSettings

if __name__ == "__main__":
    ScraplingSettings()
