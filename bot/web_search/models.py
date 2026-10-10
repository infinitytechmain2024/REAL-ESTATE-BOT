"""Plain data passed between the web-search worker, its store and the campaign runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .sources.base import SourceListing

UrlKind = Literal["listing", "index", "unknown"]
RunState = Literal["searching", "done", "stopped"]
QUERY_LANGUAGES = ("es", "en", "ru", "uk")
# The last line of a post built from a search result (``worker.search_result``); the relevance check reads it.
INDEX_RESULT_NOTE = "Данные со страницы результатов {host}"  # last line of a post built from an index page's JSON-LD
SEARCH_RESULT_NOTE = "(Страница сайта не прочитана: это заголовок и описание объявления из результатов поиска.)"


@dataclass(frozen=True, slots=True)
class WebProgress:
    """The web stage's live numbers for one campaign (in memory, kept by the worker; see ``WebSearchWorker.progress``).

    ``layer``: ``http`` | ``browser`` | ``api`` (structured source) | ``unlocker`` (HTML provider);
    ``read``: pages read from the sites;
    ``found``: pages that are listings (a search-result card counts); ``portals_done``/``portals_total``: sites with
    nothing left to read / sites known so far; ``refusals``: the current host's consecutive refusals per layer
    (owners only); ``finished``: the stage has ended; ``url``: the page being read now (the status line links to it).
    """

    host: str | None = None
    layer: str | None = None
    read: int = 0
    found: int = 0
    portals_done: int = 0
    portals_total: int = 0
    refusals: tuple[tuple[str, int], ...] = ()
    finished: bool = False
    url: str | None = None


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
    progress: WebProgress | None = None
    # Sites that asked for a person's check and wait for it (owners' status: «Сайт <host> просит проверку»).
    verification: tuple[str, ...] = ()


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
    unverified: bool = False   # the site asked for a person's check and nobody passed it («проверку никто не прошёл»)


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
    ``via`` "api": structured listing facts from a listing provider, counted as a real read.
    API imports may reclaim failed URLs and leave HTML refusal counters unchanged.
    """

    ok: bool
    kind: UrlKind = "listing"
    final_url: str = ""
    title: str = ""
    text: str = ""
    error: str | None = None
    query: str | None = None
    via: Literal["page", "search", "index", "api"] = "page"
    # the layer that produced this result or its error: "http", "render" (browser), "scrape" (unlocker API),
    # "api" (structured listing provider), "none" (the site was never asked).
    # Provider results do not count as HTTP/browser refusals.
    layer: Literal["http", "render", "scrape", "none", "api"] = "http"


@dataclass(frozen=True, slots=True)
class HostVerification:
    """Where a site stands in the human verification of the web stage (``store.host_verification``).

    ``open``: a job waits for a person (the site is skipped); ``verified``: a person passed the check at
    ``solved_at`` (the site is read through the checked browser profile); ``unsolved``: the job expired, was
    cancelled or failed during this campaign (the site is unreadable for it); ``none``: nothing special.
    """

    state: Literal["none", "open", "verified", "unsolved"] = "none"
    job_id: str | None = None
    solved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceRun:
    """Durable source checkpoint: a claimed launch is never launched a second time."""

    campaign_id: str
    name: str
    state: Literal["starting", "running", "ready", "completed", "failed"] = "starting"
    run_id: str | None = None
    dataset_id: str | None = None
    listings: tuple[SourceListing, ...] = ()
    import_offset: int = 0
    error_code: str | None = None
