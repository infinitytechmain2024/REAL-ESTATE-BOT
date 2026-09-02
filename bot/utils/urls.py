"""URL normalisation and hashing.

De-duplication is only as good as the normalisation in front of it: the same
listing routinely appears as ``http://Example.com/a/?utm_source=fb`` and
``https://example.com/a#gallery``. :func:`normalize_url` collapses those to one
canonical string and :func:`url_hash` turns it into the ``url_hash`` column
that carries the ``UNIQUE (user_id, url_hash)`` constraint.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change which document is served.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "mc_", "ga_", "_hs")
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "yclid",
        "msclkid",
        "igshid",
        "mkt_tok",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "spm",
        "trk",
        "cmpid",
        "campaign",
        "_openstat",
    }
)

_DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """Return a canonical form of *url* for comparison purposes.

    Lower-cases the host, drops ``www.``, the fragment, default ports,
    tracking parameters and a trailing slash, and sorts the remaining query
    parameters. ``http`` is folded into ``https`` because the two serve the
    same document and we only ever want to show a listing once -- the original
    URL is what actually gets fetched and displayed, this form exists purely
    for comparison. Input without a host (or that cannot be parsed at all) is
    returned stripped, so this never raises on engine-supplied data.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return candidate

    host = parts.hostname or ""
    if not host:
        # Relative paths, "mailto:", plain text -- nothing to canonicalise.
        return candidate
    if host.startswith("www."):
        host = host[4:]

    # http and https address the same document; treat them as one.
    scheme = "https" if parts.scheme.lower() in ("", "http", "https") else parts.scheme.lower()

    netloc = host
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept), doseq=True)

    # Fragment is dropped entirely: it never selects a different document.
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """Stable SHA-256 of :func:`normalize_url`, hex-encoded (64 chars)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    """Registrable-ish host of *url* without ``www.``; empty string if unparsable."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Telling a listing apart from the catalogue page it lives on.
#
# A search engine happily returns "Land for sale in Madrid -- 548 listings" for
# a query about a plot in Madrid. It is topically perfect and useless as a
# lead: there is no address, no price, nothing to act on. Individual listings,
# by contrast, almost always carry an id in the path.
#
# This is a signal, never a filter. Some sites do publish slug-only listing
# URLs, so a false positive must not delete a real result -- it only pushes it
# down the order and warns the ranking model.
# ---------------------------------------------------------------------------

_ID_IN_PATH = re.compile(r"\d{5,}")
"""A run of five or more digits: an listing id, an MLS number, a reference."""

# Query parameters that only ever appear on a results page.
_INDEX_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "page", "pagina", "pageno", "p", "offset", "start",
        "sort", "sortby", "orden", "ordenado-por", "order", "sortierung",
        "filter", "filters", "search", "q", "query", "keyword",
        "min_price", "max_price", "precio-min", "precio-max",
    }
)

# Path segments that name a category rather than an object. Deliberately
# multilingual: these bots search in the local language of the target country.
_CATALOGUE_SEGMENTS: frozenset[str] = frozenset(
    {
        # english
        "for-sale", "sale", "buy", "rent", "search", "results", "listings",
        "properties", "property", "plots", "land", "real-estate", "catalog",
        "catalogue", "find", "browse", "all",
        # spanish / portuguese
        "venta", "ventas", "comprar", "alquiler", "terrenos", "parcelas",
        "inmuebles", "buscar", "imoveis", "venda",
        # russian / ukrainian
        "prodazha", "kupit", "poisk", "katalog", "nedvizhimost", "uchastki",
        "obyavleniya", "prodazh", "kupiti", "dilyanki",
        # italian / french / german / greek / turkish
        "vendita", "immobili", "terreni", "vente", "terrain", "immobilier",
        "kaufen", "grundstueck", "immobilien", "poliseis", "satilik", "arsa",
    }
)


def looks_like_index(url: str) -> bool:
    """Whether *url* looks like a catalogue or search page, not a single item.

    Returns ``False`` whenever the path carries something id-shaped, since that
    is the strongest available evidence of an individual listing and should
    outweigh every category word around it.
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False

    path = (parts.path or "/").strip("/")

    # An id in the path settles it: this is one object.
    if _ID_IN_PATH.search(path):
        return False

    # Bare domain.
    if not path:
        return True

    # Sorting, paging and filtering only exist on a results page.
    query_keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if query_keys & _INDEX_QUERY_KEYS:
        return True

    segments = [segment.lower() for segment in path.split("/") if segment]

    # A whole segment that is nothing but a category word.
    if any(segment in _CATALOGUE_SEGMENTS for segment in segments):
        return True

    # Hyphenated category slugs: "venta-terrenos", "plots-land-and-ruins",
    # "venta-urbano-de-particular". Individual listings do use slugs too, but
    # theirs describe one object and are rarely built purely from these words.
    for segment in segments:
        words = [word for word in segment.split("-") if word]
        if len(words) > 1 and sum(word in _CATALOGUE_SEGMENTS for word in words) >= 2:
            return True

    return False
