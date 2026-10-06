"""URL keys, site names and portal rules: which page is a concrete listing, which is a list of them.

``url_key`` is the global identity of a page (SHA-256 of ``bot.utils.urls.normalize_url``:
lower-case host without ``www.``, no fragment, no tracking parameters, sorted
query, ``http`` folded into ``https``). ``host_of`` is the identity of a site.
Both are what migration 021 de-duplicates on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bot.utils.urls import normalize_url, url_hash

from .models import UrlKind

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
# Never fetched: social networks (Facebook has its own stage), video, encyclopaedias, search engines.
BLOCKED_HOSTS = frozenset({
    "facebook.com", "fb.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "youtube.com", "youtu.be",
    "linkedin.com", "reddit.com", "pinterest.com", "t.me", "telegram.me", "telegram.org", "vk.com", "ok.ru",
    "wikipedia.org", "wikidata.org", "google.com", "google.es", "bing.com", "duckduckgo.com", "yandex.ru",
    "yandex.com", "maps.google.com", "apple.com", "play.google.com", "whatsapp.com",
})
_SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".doc", ".docx", ".xls",
                    ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".avi", ".xml", ".json", ".rss")


def url_key(url: str) -> str:
    """The global identity of a page: SHA-256 hex of its normalised form."""
    return url_hash(url)


def host_of(url: str) -> str:
    """The site of ``url``: lower-case host without ``www.``/``m.``; ``""`` when there is none."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    for prefix in ("www.", "m."):
        if host.startswith(prefix) and host.count(".") > 1:
            host = host[len(prefix):]
    return host if _HOST.match(host) else ""


def is_blocked(host: str, extra: frozenset[str] = frozenset()) -> bool:
    """A host (or a subdomain of one) the web stage never fetches."""
    blocked = BLOCKED_HOSTS | extra
    return any(host == b or host.endswith("." + b) for b in blocked)


def fetchable(url: str, extra_blocked: frozenset[str] = frozenset()) -> bool:
    """A plain public http(s) page URL that is not a file, a login or a blocked site."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.username or parts.password or len(url) > 2000:
        return False
    host = host_of(url)
    if not host or is_blocked(host, extra_blocked):
        return False
    path = parts.path.lower()
    if path.endswith(_SKIP_EXTENSIONS):
        return False
    return not any(word in path for word in ("/login", "/signin", "/sign-in", "/registro", "/register", "/account",
                                             "/mi-cuenta", "/checkout", "/cart"))


def absolute(base: str, href: str) -> str | None:
    """``href`` resolved against ``base`` without its fragment; None for mailto:, javascript: and the like."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    try:
        joined = urljoin(base, href)
    except ValueError:
        return None
    return joined.split("#", 1)[0] if joined.startswith(("http://", "https://")) else None


def same_site(url: str, host: str) -> bool:
    return host_of(url) == host


def normalized(url: str) -> str:
    return normalize_url(url)


# --- portals ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Portal:
    """URL shapes of one listing portal. ``listing``: one concrete ad; ``index``: a search/list page."""

    listing: re.Pattern[str]
    index: re.Pattern[str] | None = None


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


PORTALS: dict[str, Portal] = {
    "idealista.com": Portal(_p(r"^/(?:[a-z]{2}/)?inmueble/\d{5,}/?$"),
                            _p(r"^/(?:[a-z]{2}/)?(?:venta|alquiler|comprar|alquilar|obra-nueva)-[a-z0-9-]+/")),
    "fotocasa.es": Portal(_p(r"/\d{6,}/d/?$"), _p(r"^/(?:es|en|ca)/(?:comprar|alquiler|obra-nueva)/.+/l(?:/\d+)?/?$")),
    "pisos.com": Portal(_p(r"^/(?:comprar|alquilar)/[a-z0-9_-]+-\d{6,}(?:_\d+)?/?$"),
                        _p(r"^/(?:venta|alquiler|obra_nueva)/[a-z0-9_-]+(?:/[a-z0-9_-]+)*/?$")),
    "milanuncios.com": Portal(_p(r"-\d{6,}\.htm$"), _p(r"^/[a-z0-9-]+(?:-en-[a-z0-9_-]+)?/?$")),
    "habitaclia.com": Portal(_p(r"-i\d{6,}\.htm$"), _p(r"^/[a-z0-9_-]+\.htm$")),
    "yaencontre.com": Portal(_p(r"/inmueble-[a-z0-9-]*\d{5,}"), _p(r"^/(?:venta|alquiler)/")),
    "kyero.com": Portal(_p(r"/property/\d{5,}"), _p(r"-(?:property|properties|land|plots)-for-sale")),
    "thinkspain.com": Portal(_p(r"/property-for-sale/\d{5,}"), _p(r"^/property-for-sale(?:/[a-z0-9-]+)*/?$")),
    "spainhouses.net": Portal(_p(r"-\d{5,}\.html$"), _p(r"^/(?:en|es)/[a-z0-9-]+\.html$")),
    "solvia.es": Portal(_p(r"/(?:comprar|alquilar)/.+-\d{5,}/?$"), _p(r"^/es/(?:comprar|alquilar)/[a-z0-9-]+/?$")),
    "servihabitat.com": Portal(_p(r"/ficha/\d{5,}"), _p(r"^/es/(?:venta|alquiler)/")),
    "tucasa.com": Portal(_p(r"/\d{6,}/?$"), _p(r"^/(?:compra-venta|alquiler)/")),
    "enalquiler.com": Portal(_p(r"_\d{5,}\.html$"), _p(r"^/alquiler_")),
    "dom.ria.com": Portal(_p(r"-\d{6,}\.html$"), _p(r"^/(?:uk/|ru/)?(?:prodazha|arenda)-")),
    "lun.ua": Portal(_p(r"/\d{6,}/?$"), _p(r"^/(?:uk/|ru/)?(?:продаж|оренда|sale|rent)")),
    "olx.ua": Portal(_p(r"/obyavlenie/.+-ID[a-zA-Z0-9]+\.html$"), _p(r"^/(?:uk/)?nedvizhimost/")),
    "rieltor.ua": Portal(_p(r"/(?:flats|houses|land)-sale/view/\d+"), _p(r"^/(?:flats|houses|land)-sale/")),
}
# Every Spanish campaign searches each of these (``queries.cover_portals``), in this order:
# Idealista and Fotocasa first, then the big national portals, classifieds, foreign-buyer
# portals, land, and the bank/Sareb portfolios (land and unusual objects not on the leaders).
SPAIN_PORTALS = ("idealista.com", "fotocasa.es", "yaencontre.com", "pisos.com", "habitaclia.com",
                 "milanuncios.com", "indomio.es", "tucasa.com", "kyero.com", "thinkspain.com", "terrenos.es",
                 "solvia.es", "alisedainmobiliaria.com", "servihabitat.com", "sareb.es", "altamirainmuebles.com",
                 "haya.es", "hogaria.net", "spainhouses.net", "green-acres.es")
# What a Spanish campaign searches by property kind (``QueryTask.portals``); ``SPAIN_PORTALS`` stays the union.
SPAIN_PORTALS_BY_KIND: dict[str, tuple[str, ...]] = {
    "apartment": ("idealista.com", "fotocasa.es", "habitaclia.com", "pisos.com", "yaencontre.com", "kyero.com",
                  "thinkspain.com", "milanuncios.com", "indomio.es", "tucasa.com", "spainhouses.net"),
    "land": ("idealista.com", "fotocasa.es", "terrenos.es", "sareb.es", "milanuncios.com", "pisos.com", "kyero.com"),
    "commercial": ("idealista.com", "fotocasa.es", "pisos.com", "milanuncios.com", "habitaclia.com"),
    "room": ("idealista.com", "fotocasa.es", "milanuncios.com", "habitaclia.com", "pisos.com"),
}
SPAIN_PORTALS_BY_KIND["house"] = SPAIN_PORTALS_BY_KIND["apartment"]
# Bank/Sareb portfolios: searched only when the task asks for a bargain (``SPAIN_BANK_WORDS``).
SPAIN_BANK_PORTALS = ("solvia.es", "servihabitat.com", "alisedainmobiliaria.com", "sareb.es",
                      "altamirainmuebles.com", "haya.es", "hogaria.net", "green-acres.es")
SPAIN_BANK_WORDS = ("banco", "bank", "embargo", "sareb", "дешев", "cheap", "барат", "barato", "oportunidad")
UKRAINE_PORTALS = ("dom.ria.com", "lun.ua", "olx.ua", "rieltor.ua")
_GENERIC_LISTING = re.compile(r"(?:^|[/_-])(?:id)?\d{6,}(?:[/_.-]|$)|/(?:inmueble|anuncio|ficha|property|listing|detalle|obyavlenie)[/-][^/]*\d{4,}",
                              re.IGNORECASE)


def portal_of(host: str) -> Portal | None:
    for name, portal in PORTALS.items():
        if host == name or host.endswith("." + name):
            return portal
    return None


def classify_url(url: str) -> UrlKind:
    """``listing`` (one concrete ad), ``index`` (a portal search/list page) or ``unknown``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unknown"
    path = parts.path or "/"
    portal = portal_of(host_of(url))
    if portal is not None:
        if portal.listing.search(path):
            return "listing"
        if portal.index is not None and portal.index.search(path):
            return "index"
        return "unknown"
    return "listing" if _GENERIC_LISTING.search(path) else "unknown"
