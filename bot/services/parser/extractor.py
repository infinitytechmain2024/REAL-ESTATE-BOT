"""HTML -> readable text.

trafilatura does the real work; it is built for exactly this (strip navigation,
boilerplate and comments, keep the article body). The lxml fallback exists
because listing pages are frequently not articles at all, and trafilatura
returns nothing for them -- in that case any visible text beats none, since the
LLM is looking for prices and addresses rather than prose.
"""

from __future__ import annotations

import trafilatura
from lxml import etree
from lxml import html as lxml_html
from lxml.html import HtmlElement

from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.utils.text import collapse_whitespace

log = get_logger(__name__)

_MIN_USEFUL_CHARS = 200
"""Below this, trafilatura almost certainly missed the content."""

# Elements that never carry information the LLM wants.
_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "nav", "footer", "form")


def extract_text(
    html: str,
    *,
    url: str,
    final_url: str | None = None,
    max_chars: int = 6000,
) -> PageContent:
    """Extract readable text from *html*. Never raises."""
    page = PageContent(url=url, final_url=final_url if final_url != url else None)

    if not html or not html.strip():
        page.error = "empty response body"
        return page

    text = ""
    try:
        text = (
            trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,  # prices and areas live in tables
                favor_recall=True,
                url=final_url or url,
            )
            or ""
        )
    except Exception as exc:  # noqa: BLE001 - trafilatura raises on odd markup
        log.debug("parser.trafilatura.failed", url=url, error=str(exc))

    # One parse serves both the fallback text and the title.
    tree = _parse(html)

    if len(text.strip()) < _MIN_USEFUL_CHARS:
        fallback = _visible_text(tree)
        if len(fallback) > len(text):
            text = fallback

    text = collapse_whitespace(text)
    if not text:
        page.error = "no readable text found"
        return page

    page.text = text[:max_chars]
    page.title = _title(tree)
    return page


def _parse(html: str) -> HtmlElement | None:
    """Parse *html*, or return ``None`` if lxml cannot make sense of it."""
    try:
        return lxml_html.fromstring(html)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None


def _visible_text(tree: HtmlElement | None) -> str:
    """All visible text, boilerplate removed. Crude, but rarely empty.

    Text nodes are joined with newlines rather than concatenated: lxml's
    ``text_content()`` would run "Plot 1500 m2" straight into the next
    listing's "Plot 3000 m2", which reads as one nonsensical number to the LLM.
    """
    if tree is None:
        return ""

    for element in tree.xpath("//" + " | //".join(_STRIP_TAGS)):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)

    lines = [chunk.strip() for chunk in tree.itertext()]
    return collapse_whitespace("\n".join(line for line in lines if line))


def _title(tree: HtmlElement | None) -> str | None:
    """The document ``<title>``, if there is one.

    Read after :func:`_visible_text` has already stripped its tags, which is
    harmless: ``<title>`` is not on the strip list.
    """
    if tree is None:
        return None
    found = tree.xpath("//title/text()")
    return collapse_whitespace(str(found[0]))[:300] if found else None
