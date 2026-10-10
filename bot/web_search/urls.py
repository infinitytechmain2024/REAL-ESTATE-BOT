"""URL keys, site names and portal rules: which page is a concrete listing, which is a list of them.

``url_key`` is the global identity of a page (SHA-256 of ``bot.utils.urls.normalize_url``:
lower-case host without ``www.``, no fragment, no tracking parameters, sorted
query, ``http`` folded into ``https``). ``host_of`` is the identity of a site.
Both are what migration 021 de-duplicates on.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

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
# Never listing sources: dictionaries, calculators, ministries, news/TV, transport, travel guides. Data tables.
# A host matches itself and its subdomains; the suffixes (``.gob.es``) match any host that ends with them.
NON_LISTING_HOSTS = frozenset({
    # dictionaries and reference
    "rae.es", "wordreference.com", "fandom.com", "wiktionary.org", "linguee.com", "reverso.net",
    # calculators and maths
    "wumbo.net", "calculator.net", "omnicalculator.com", "calculadora.es", "symbolab.com", "wolframalpha.com",
    # government
    "boe.es", "gob.es", "gov", "gov.ua", "gov.uk",
    # news and TV
    "rtve.es", "elpais.com", "lasprovincias.es", "levante-emv.com", "elmundo.es", "abc.es", "20minutos.es",
    "europapress.es", "eldiario.es", "lavanguardia.com", "elconfidencial.com", "okdiario.com", "bbc.com",
    "cnn.com", "pravda.com.ua", "ukrinform.ua",
    # transport
    "metrovalencia.es", "renfe.com", "emtvalencia.es",
    # travel and city guides
    "citiesinsider.com", "booking.com", "expedia.com", "lonelyplanet.com",
    # more dictionaries and translators (a «terreno en venta» query finds their entries)
    "cambridge.org", "pons.com", "ingles.com", "spanishdict.com", "collinsdictionary.com", "merriam-webster.com",
    "dictionary.com", "oxfordlearnersdictionaries.com", "deepl.com", "glosbe.com", "bab.la", "larousse.fr",
    "wordhippo.com", "thefreedictionary.com", "tureng.com", "linguee.es", "diccionario.reverso.net",
    # science, statistics and land-use data
    "fao.org", "mdpi.com", "copernicus.eu", "sciencedirect.com", "springer.com", "researchgate.net",
    "academia.edu", "scielo.org", "dialnet.unirioja.es", "europa.eu", "ine.es", "catastro.minhap.es",
    "worldbank.org", "statista.com", "jstor.org", "nature.com", "wiley.com", "tandfonline.com",
})
# Brand names that exist under many TLDs: ``web2.0calc.es``, ``tripadvisor.co.uk``, ``airbnb.com``.
NON_LISTING_BRANDS = ("web2.0calc", "tripadvisor", "airbnb")
# Path prefixes that are not listings on a site that otherwise is a portal.
NON_LISTING_PATHS: dict[str, tuple[str, ...]] = {"idealista.com": ("/news/", "/en/news/", "/ca/news/")}
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
    blocked = BLOCKED_HOSTS | NON_LISTING_HOSTS | extra
    if any(host == b or host.endswith("." + b) for b in blocked):
        return True
    return any(re.search(rf"(?:^|\.){re.escape(brand)}\.[a-z]{{2,3}}(?:\.[a-z]{{2}})?$", host)
               for brand in NON_LISTING_BRANDS)


def non_listing_path(host: str, path: str) -> bool:
    """A path on a portal that is editorial (``idealista.com/news/...``), never a listing."""
    path = path.lower()
    return any((host == h or host.endswith("." + h)) and path.startswith(prefixes)
               for h, prefixes in NON_LISTING_PATHS.items())


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
    if path.endswith(_SKIP_EXTENSIONS) or non_listing_path(host, path):
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
_LISTING_WORD = re.compile(
    r"(?<![a-z])(?:inmueble|anuncio|ficha|property|listing|detalle|obyavlenie|piso|casa|apartamento|chalet|terreno"
    r"|parcela|vivienda|venta|alquiler|rent|sale)(?![a-z])", re.IGNORECASE)
_DIGITS = re.compile(r"(?<!\d)\d{5,}(?!\d)")
_DATE_DIR = re.compile(r"/(?:19|20)\d\d/(?:0?[1-9]|1[0-2])/")          # /2024/05/ before the id: a news/blog path
_DATE_ID = re.compile(r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")  # 20240512
_PRICE = re.compile(r"\d\s*(?:€|k\s?€|eur\b|euros?\b)|(?:€|eur\b)\s?\d", re.IGNORECASE)
_AREA = re.compile(r"\d\s*(?:m²|m2|m\^2|metros?\b|mts?\b)", re.IGNORECASE)


_POSTAL = re.compile(r"[0-5]\d{4}")                                   # a Spanish postal code: not an ad id by itself
_ID_MARK = re.compile(r"(?:ref-?|id-?|-id)[a-z]{0,3}$", re.IGNORECASE)  # "ref-", "ref", "id-", "-id" right before a number
_REF_ID = re.compile(r"(?<![a-z])(?:ref|id)-?[a-z]{0,3}\d{4,}(?!\d)", re.IGNORECASE)
_WORD_ID = re.compile(r"-(\d{4,})\.?(?:html?)?$", re.IGNORECASE)      # "piso-centro-4567" (end of a segment)


def _segment_listing(path: str) -> bool:
    """A 4+ digit id after ``ref``/``id`` or at the end of a segment that carries a listing word."""
    if _REF_ID.search(path):
        return True
    for segment in path.split("/"):
        found = _WORD_ID.search(segment)
        if found and _LISTING_WORD.search(segment[:found.start()]) and not re.fullmatch(r"(?:19|20)\d\d", found.group(1)):
            return True
    return False


def _generic_listing(path: str) -> bool:
    """An unknown site's concrete ad: a listing word and a 5+ digit id, or a 6+ digit id that is not a news date.

    A bare 5-digit postal code (``/venta/pisos-valencia-46001/``) is no id unless ``.htm(l)`` follows or
    ``ref-``/``id-`` precedes it."""
    has_word = bool(_LISTING_WORD.search(path))
    if _segment_listing(path):
        return True
    for match in _DIGITS.finditer(path):
        number = match.group(0)
        if (len(number) == 8 and _DATE_ID.fullmatch(number)) or _DATE_DIR.search(path[:match.start()]):
            continue
        before, after = path[:match.start()], path[match.end():match.end() + 1]
        if (len(number) == 5 and _POSTAL.fullmatch(number) and not _ID_MARK.search(before)
                and not path[match.end():].lower().startswith((".htm", ".html"))):
            continue
        bounded = (not before or before[-1] in "/_-" or before[-2:].lower() == "id") and (not after or after in "/_.-")
        if has_word or (len(number) >= 6 and bounded):
            return True
    return False


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
    return "listing" if _generic_listing(path) else "unknown"


def portal_listing(url: str) -> bool:
    """True only for a concrete ad of a known portal (by its URL regex), never by the generic rule."""
    portal = portal_of(host_of(url))
    return portal is not None and bool(portal.listing.search(urlsplit(url).path or "/"))


def classify_page(url: str, *, has_listing_data: bool = False, text: str = "") -> UrlKind:
    """The kind of a page that was read: ``classify_url`` first; an unknown page becomes ``listing`` only
    when it carries listing JSON-LD (``has_listing_data``) or its text shows both a price and an area."""
    kind = classify_url(url)
    if kind != "unknown":
        return kind
    if has_listing_data or (_PRICE.search(text) and _AREA.search(text)):
        return "listing"
    return "unknown"


# --- known hosts, the deal in a path, evidence in a search hit ---------------------------------


def known_portal(host: str, extra: Iterable[str] = ()) -> bool:
    """A listing portal we know by name (``PORTALS``, the country lists) or one the plan/spec names (``extra``)."""
    names = (*PORTALS, *SPAIN_PORTALS, *SPAIN_BANK_PORTALS, *UKRAINE_PORTALS, *extra)
    return any(host == n or host.endswith("." + n) for n in names)


_RENT_WORDS = frozenset({"alquiler", "alquilar", "alquileres", "alquilo", "arrendamiento", "arrendar", "rent", "rental",
                         "rentals", "lloguer", "оренда", "аренда", "arenda"})
_SALE_WORDS = frozenset({"venta", "comprar", "compra", "sale", "prodazha", "продаж", "продажа"})


def path_deal(url: str) -> str | None:
    """``rent`` or ``sale`` when the URL path says so; None when it says neither or both (words are the path's
    segments split on «/» and «-», so ``/alquiler-venta/`` or ``/for-sale/to-rent/`` is ambiguous)."""
    try:
        path = unquote(urlsplit(url).path or "/").lower()
    except ValueError:
        return None
    words = {w for w in re.split(r"[/\-_.]+", path) if w}
    rent, sale = bool(words & _RENT_WORDS), bool(words & _SALE_WORDS)
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def deal_conflict(url: str, deal: object) -> bool:
    """True when the campaign wants ``deal`` (sale/rent) and the URL path is the opposite one."""
    if deal not in ("sale", "rent"):
        return False
    found = path_deal(url)
    return found is not None and found != deal


# The words that make a text «about a property». Kept in step with ``queries._KIND_WORDS`` / ``_KIND_TERMS`` (a test
# checks that every kind word and term passes) plus the kinds a campaign may name: offices, garages, plots, hotels ...
# Latin whole words (an optional plural ending, a word boundary on both sides); Latin and Cyrillic stems match from a
# word's start. Texts are folded first (``fold_text``: lower case, no accents, ё -> е).
_PROPERTY_WORDS = (
    "piso", "casa", "chalet", "atico", "duplex", "estudio", "terreno", "parcela", "solar", "finca", "inmueble",
    "propiedad", "nave", "local", "villa", "flat", "apartment", "house", "home", "land", "plot", "condo", "bungalow",
    "townhouse", "loft", "room", "property", "propertie", "studio", "penthouse", "garage", "office", "warehouse",
    "oficina", "garaje", "parking", "aparcamiento", "trastero", "hotel", "masia", "edificio",
)
_PROPERTY_PHRASES = (r"obra\s+nueva", r"bajo\s+comercial", r"real\s+estate", r"local(?:es)?\s+comercial")
_PROPERTY_STEMS = (
    "vivienda", "apartament", "habitaci", "oficin", "garaj", "edifici", "adosad", "inmobiliari", "hotel", "masia",
    "квартир", "будин", "комнат", "кімнат", "участ", "ділянк", "вилл", "нерухом", "недвижим", "гараж", "офис", "офіс",
    "помещени", "приміщ", "земл", "жиль", "житл", "пентхаус", "таунхаус", "котедж", "студи", "склад", "апартамент",
    "магазин", "коммерч", "сотк", "дача", "дачи",
)
_PROPERTY_WORD = re.compile(
    r"(?<!\w)(?:(?:" + "|".join((*_PROPERTY_WORDS, *_PROPERTY_PHRASES)) + r")(?:e?s)?(?!\w)"
    r"|(?:" + "|".join(_PROPERTY_STEMS) + r")|дом(?:а|у|е|ом|ов|ы)?(?!\w))")
_SIGNAL_PRICE = re.compile(r"\d\s*(?:€|\$|£|k\s?€|грн|uah|usd|eur\b|euros?\b|евро|євро|дол)|(?:€|\$|eur\b)\s?\d", re.IGNORECASE)
_SIGNAL_AREA = re.compile(r"\d\s*(?:m²|m2|m\^2|м²|м2|кв\.?\s*м|metros?\b|mts?\b|sq\.?\s*m)", re.IGNORECASE)
_SIGNAL_ROOMS = re.compile(r"\d\s*[-.]?\s*(?:hab|dorm|bed|room|комн|кімн|рум|ambientes|bedrooms?)", re.IGNORECASE)


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch)).replace("ё", "е")


def has_property_word(text: str) -> bool:
    return bool(_PROPERTY_WORD.search(fold_text(text)))


# A deal word: an offer («en venta», «alquila», «for rent», «продам», «сдам»); wanted posts («куплю», «сниму») are not.
_SIGNAL_DEAL = re.compile(
    r"(?<!\w)(?:venta|vende(?:n|mos)?|vendo|alquiler(?:es)?|alquila(?:n|mos)?|alquilo|comprar|for\s+sale|for\s+rent|to\s+let"
    r"|se\s+vende|se\s+alquila)(?!\w)|(?<!\w)(?:продаж|продам|продаю|продаетс|оренд|аренд|сдам|сдаю|сдаетс|здам|здаю)")
# A count written in words before a room word: «tres habitaciones», «two bedrooms», «две комнаты».
_SIGNAL_ROOM_WORDS = re.compile(
    r"(?<!\w)(?:uno|una|dos|tres|cuatro|cinco|one|two|three|four|five|одна|одну|две|два|три|четыре|пять|дві|чотири)"
    r"\s+(?:hab|dorm|bedroom|bed\b|комнат|кімнат)")


def listing_figures(title: str, snippet: str) -> bool:
    """A search hit that states a listing's figure: a price or an area with its unit (``domain_policy`` soft)."""
    folded = fold_text(f"{title} {snippet}")
    return bool(_SIGNAL_PRICE.search(folded) or _SIGNAL_AREA.search(folded))


def listing_evidence(title: str, snippet: str) -> bool:
    """A search hit that looks like a listing: a property word AND one of a figure (price, area or rooms with a
    unit), a deal word («en venta», «for rent», «продам») or a room count in words («tres habitaciones»). A bare
    word like «precio» is not evidence, a news piece on prices has none of these; agency pages without digits
    («Piso en Valencia - Inmobiliaria X ... tres habitaciones») pass by their deal or room words."""
    text = f"{title} {snippet}"
    if not has_property_word(text):
        return False
    folded = fold_text(text)
    return bool(_SIGNAL_PRICE.search(folded) or _SIGNAL_AREA.search(folded) or _SIGNAL_ROOMS.search(folded)
                or _SIGNAL_DEAL.search(folded) or _SIGNAL_ROOM_WORDS.search(folded))
