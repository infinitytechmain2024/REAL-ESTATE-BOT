"""HTML -> readable text and links, with the standard library only (no browser, no scripts run).

``parse_html`` keeps the title, the meta description and the visible body
text (menus, forms, scripts and styles dropped) plus every ``<a href>``.
``listing_links`` picks, from a portal's search/list page, the links to the
concrete listings on the same site; ``post_text`` is what a listing becomes in
``collected_posts.body_text``.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

from .urls import absolute, classify_url, fetchable, host_of, url_key

_SKIP = frozenset({"script", "style", "noscript", "svg", "template", "iframe", "head", "nav", "footer", "form",
                   "select", "option", "button", "canvas", "object"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                   "track", "wbr"})
_BLOCK = frozenset({"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
                    "section", "article", "header", "main", "aside", "dd", "dt", "dl", "table", "blockquote"})
MIN_INDEX_LINKS = 3


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    text: str


@dataclass(frozen=True, slots=True)
class ParsedPage:
    title: str
    description: str
    text: str
    links: tuple[Link, ...]


class _Parser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.in_title = False
        self.title: list[str] = []
        self.description = ""
        self.og_title = ""
        self.lines: list[str] = []
        self.current: list[str] = []
        self.links: list[Link] = []
        self.anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = " ".join(values.get("content", "").split())[:1000]
            elif name == "og:title" and not self.og_title:
                self.og_title = " ".join(values.get("content", "").split())[:300]
        elif tag == "base" and values.get("href"):
            self.base_url = absolute(self.base_url, values["href"]) or self.base_url
        if tag in _VOID:
            if tag == "br":
                self._flush()
            return
        if tag in _SKIP:
            self.skip_depth += 1
            return
        if tag in _BLOCK:
            self._flush()
        if tag == "a" and self.skip_depth == 0 and values.get("href"):
            url = absolute(self.base_url, values["href"])
            self.anchor = (url, []) if url else None

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in _VOID:
            return
        if tag in _SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "a" and self.anchor is not None:
            url, words = self.anchor
            self.links.append(Link(url, " ".join(" ".join(words).split())[:200]))
            self.anchor = None
        if tag in _BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
            return
        if self.skip_depth:
            return
        self.current.append(data)
        if self.anchor is not None:
            self.anchor[1].append(data)

    def _flush(self) -> None:
        line = " ".join(" ".join(self.current).split())
        self.current = []
        if line and (not self.lines or self.lines[-1] != line):
            self.lines.append(line)

    def result(self) -> ParsedPage:
        self._flush()
        if self.anchor is not None:
            self.links.append(Link(self.anchor[0], " ".join(" ".join(self.anchor[1]).split())[:200]))
        title = " ".join(" ".join(self.title).split())[:300] or self.og_title
        return ParsedPage(title, self.description, "\n".join(self.lines), tuple(self.links))


def parse_html(html: str, url: str) -> ParsedPage:
    parser = _Parser(url)
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):  # malformed markup: keep what was read
        pass
    return parser.result()


def listing_links(page: ParsedPage, page_url: str, *, limit: int,
                  extra_blocked: frozenset[str] = frozenset()) -> list[str]:
    """Links to concrete listings on the same site as ``page_url``, in page order, without repeats."""
    host = host_of(page_url)
    found: list[str] = []
    seen: set[str] = set()
    for link in page.links:
        if len(found) >= limit:
            break
        url = link.url
        if host_of(url) != host or not fetchable(url, extra_blocked) or classify_url(url) != "listing":
            continue
        key = url_key(url)
        if key not in seen:
            seen.add(key)
            found.append(url)
    return found


def looks_like_index(page: ParsedPage, page_url: str) -> bool:
    """A page with several links to concrete listings on its own site is a list of them."""
    return len(listing_links(page, page_url, limit=MIN_INDEX_LINKS)) >= MIN_INDEX_LINKS


def post_text(page: ParsedPage, *, limit: int) -> str:
    """The listing as a post: title, description, then the visible text, bounded."""
    parts: list[str] = []
    for part in (page.title, page.description):
        if part and part not in parts:
            parts.append(part)
    body = page.text
    text = "\n".join([*parts, body]) if body else "\n".join(parts)
    return text[:limit].strip()
