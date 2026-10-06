"""Plain data passed between the web-search worker, its store and the campaign runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UrlKind = Literal["listing", "index", "unknown"]
RunState = Literal["searching", "done", "stopped"]
QUERY_LANGUAGES = ("es", "en", "ru", "uk")
# The last line of a post built from a search result (``worker.search_result``); the relevance check reads it.
INDEX_RESULT_NOTE = "Данные со страницы результатов {host}"  # last line of a post built from an index page's JSON-LD
SEARCH_RESULT_NOTE = "(Страница сайта не прочитана: это заголовок и описание объявления из результатов поиска.)"


@dataclass(frozen=True, slots=True)
class WebStatus:
    """What the campaign runner shows about the web stage.

    ``active``: the stage has not finished (the campaign must not complete yet);
    ``host``: the site being read right now (for «Ищу на сайте <host>…»);
    ``line``: the technical progress line, for owners only.
    """

    active: bool
    host: str | None = None
    line: str = ""


@dataclass(frozen=True, slots=True)
class SiteReport:
    """One site's search funnel in a campaign (the end-of-campaign summary).

    ``queries``/``results``: ``site:`` queries for it and what the search engine returned;
    ``links``: its URLs the campaign met; ``read``: pages read; ``from_search``: listings kept from the
    search result (the site refused); ``refused``: links it would not serve (403/429, robots.txt, a block).
    """

    host: str
    queries: int = 0
    results: int = 0
    links: int = 0
    read: int = 0
    from_search: int = 0
    refused: int = 0


@dataclass(frozen=True, slots=True)
class WebRun:
    campaign_id: str
    state: RunState
    rounds: int = 0
    host: str | None = None
    progress: str | None = None
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedQuery:
    text: str
    language: str | None = None


@dataclass(frozen=True, slots=True)
class PendingQuery:
    id: str
    text: str
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A URL to put in a campaign's queue (from a search result, or from an index page)."""

    url: str
    url_key: str
    host: str
    depth: int = 0
    kind: UrlKind = "unknown"
    query_id: str | None = None
    title: str = ""     # what the search engine showed for it (search results only)
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class QueuedUrl:
    url: str
    url_key: str
    host: str
    depth: int
    kind: UrlKind
    title: str = ""     # the search engine's title and snippet (``Candidate``)
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class Counts:
    queries: int = 0          # every query row of the campaign (pending, searched, failed, skipped)
    pending_queries: int = 0
    pages: int = 0            # fetch attempts (fetched + failed)
    queued_urls: int = 0


@dataclass(frozen=True, slots=True)
class Usage:
    """Rolling 24 h totals across every campaign (the global daily caps)."""

    pages: int = 0
    queries: int = 0


@dataclass(frozen=True, slots=True)
class FetchTicket:
    """One started fetch: the claimed URL and the acquisition run that records it."""

    campaign_id: str
    url: QueuedUrl
    source_id: str
    run_id: str
    render_layer: bool = False   # the browser layer is enabled for this fetch (host blocks count both layers)


@dataclass(frozen=True, slots=True)
class PageResult:
    """How a fetch ended. ``kind`` listing stores a post; index only enqueued its links.

    ``via`` "search": the site could not be read (``error`` says why, None when it was never
    contacted: robots.txt, a blocked site) and the post is the search engine's title and snippet.
    ``via`` "index": the same, but the post was built from the listing data (JSON-LD ``ItemList``) of the
    index page the link was found on.
    """

    ok: bool
    kind: UrlKind = "listing"
    final_url: str = ""
    title: str = ""
    text: str = ""
    error: str | None = None
    query: str | None = None
    via: Literal["page", "search", "index"] = "page"
    # the layer that produced this result or its error: "http", "render" (browser), "scrape" (unlocker API),
    # "none" (the site was never asked). A refusal counts against that layer's block of the host only.
    layer: Literal["http", "render", "scrape", "none"] = "http"
