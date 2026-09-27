"""Scrapling: a listing site's own structured data (JSON-LD, OpenGraph) as plain JSON.

Most portals embed their listings as schema.org JSON-LD (``RealEstateListing``,
``Offer``, ``Apartment``, ``House``, ``ItemList`` ...) for search engines. That
data is exact: price, currency, area, rooms, address. ``structured`` reads it
with Scrapling's ``Selector`` (a parser only: no request, no browser) and turns
it into flat JSON objects:

* ``listings``: one object per listing found on the page;
* ``item_urls``: the listing links of an ``ItemList`` (a portal's result page).

``facts_block`` renders the page's own listing as one JSON line that goes on
top of the post text, so the analysis model reads the site's exact figures
next to the visible text, and the finding becomes an ordinary card.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .urls import absolute, url_key

MAX_BLOBS = 20
MAX_BLOB_CHARS = 200_000
MAX_LISTINGS = 30
MAX_DEPTH = 8
LISTING_TYPES = frozenset({
    "realestatelisting", "offer", "product", "residence", "apartment", "house", "singlefamilyresidence",
    "apartmentcomplex", "accommodation", "room", "suite", "landparcel", "place", "gatedresidencecommunity",
})
# The most specific type wins for ``property_type``; generic wrappers say nothing about the property.
_GENERIC_TYPES = frozenset({"realestatelisting", "offer", "product", "place", "thing", "webpage", "listitem"})
_NESTED = ("offers", "itemOffered", "mainEntity", "about", "containsPlace", "item")
_NUMBER = re.compile(r"\d[\d\s.,]*")


@dataclass(frozen=True, slots=True)
class Structured:
    listings: tuple[dict[str, Any], ...] = ()
    item_urls: tuple[str, ...] = ()

    def listing_for(self, page_url: str) -> dict[str, Any] | None:
        """The page's own listing: the one whose URL is the page, else the only one on it."""
        key = url_key(page_url)
        for listing in self.listings:
            if listing.get("url") and url_key(str(listing["url"])) == key:
                return listing
        return self.listings[0] if len(self.listings) == 1 else None


def structured(html: str, page_url: str) -> Structured:
    """The JSON-LD and OpenGraph listing data of ``html``; empty when there is none or it is broken."""
    try:
        from scrapling import Selector
    except ImportError:  # pragma: no cover - scrapling is in requirements.txt
        return Structured()
    try:
        page = Selector(content=html, url=page_url)
        blobs = page.css('script[type="application/ld+json"]::text').getall()[:MAX_BLOBS]
        meta = {key: page.css(f'meta[property="{key}"]::attr(content), meta[name="{key}"]::attr(content)').get() or ""
                for key in ("og:title", "og:description", "og:url", "og:type", "product:price:amount",
                            "product:price:currency", "og:price:amount", "og:price:currency")}
    except Exception:  # noqa: BLE001 - broken markup yields no structured data, never an error
        return Structured()
    return from_jsonld(blobs, page_url, meta=meta)


def from_jsonld(blobs: Iterable[str], page_url: str, *, meta: dict[str, str] | None = None) -> Structured:
    """Listings and ItemList links from raw JSON-LD texts (also what a browser read returns)."""
    listings: list[dict[str, Any]] = []
    item_urls: list[str] = []
    seen: set[str] = set()
    for blob in list(blobs)[:MAX_BLOBS]:
        try:
            data = json.loads(blob[:MAX_BLOB_CHARS].strip().rstrip(";"))
        except (ValueError, TypeError):
            continue
        for node, in_list in _walk(data):
            if in_list and _types(node) & {"listitem"} and not isinstance(node.get("item"), dict):
                url = absolute(page_url, _url_of(node.get("url") or node.get("item")) or "")
                if url and url not in item_urls:
                    item_urls.append(url)
                continue
            listing = _listing(node, page_url)
            if listing is None:
                continue
            key = json.dumps(listing, sort_keys=True, ensure_ascii=False)
            if key not in seen and len(listings) < MAX_LISTINGS:
                seen.add(key)
                listings.append(listing)
                if in_list and listing.get("url") and listing["url"] not in item_urls:
                    item_urls.append(listing["url"])
    if not listings and meta:
        og = _from_opengraph(meta, page_url)
        if og:
            listings.append(og)
    return Structured(tuple(listings), tuple(item_urls))


def facts_block(listing: dict[str, Any]) -> str:
    """One line for the top of the post: «JSON-LD: {...}» (no words of ours: the post keeps the language of the site)."""
    return "JSON-LD: " + json.dumps(listing, ensure_ascii=False, separators=(", ", ": "))


# -- walking the JSON-LD --


def _walk(data: Any, depth: int = 0, in_list: bool = False) -> Iterator[tuple[dict[str, Any], bool]]:
    """Every outermost listing node (a listing's own offers/place are merged, not listed again) and list item."""
    if depth > MAX_DEPTH:
        return
    if isinstance(data, list):
        for item in data:
            yield from _walk(item, depth + 1, in_list)
        return
    if not isinstance(data, dict):
        return
    types = _types(data)
    if "itemlist" in types:
        yield from _walk(data.get("itemListElement"), depth + 1, True)
        return
    if "listitem" in types:
        item = data.get("item")
        if isinstance(item, dict) and _types(item) & LISTING_TYPES:
            merged = {"url": data.get("url"), **item} if data.get("url") and not item.get("url") else item
            yield merged, True
        else:
            yield data, True
        return
    if types & LISTING_TYPES:
        yield data, in_list
        return
    for key in ("@graph", "mainEntity", "itemListElement"):
        if key in data:
            yield from _walk(data[key], depth + 1, in_list)


def _types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).rsplit("/", 1)[-1].lower() for v in values if isinstance(v, str)}


def _parts(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The node and the dicts it nests (offers, itemOffered, mainEntity ...), two levels deep."""
    found = [node]
    for current in (node, *[_first(node.get(k)) for k in _NESTED]):
        if not isinstance(current, dict):
            continue
        for key in _NESTED:
            child = _first(current.get(key))
            if isinstance(child, dict) and child not in found:
                found.append(child)
    return found


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return next((v for v in value if isinstance(v, dict)), None)
    return value


def _get(parts: list[dict[str, Any]], *keys: str) -> Any:
    for part in parts:
        for key in keys:
            value = part.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _listing(node: dict[str, Any], page_url: str) -> dict[str, Any] | None:
    parts = _parts(node)
    listing: dict[str, Any] = {}
    title = _text(_get(parts, "name", "headline"), 300)
    if title:
        listing["title"] = title
    url = absolute(page_url, _url_of(_get(parts, "url", "@id")) or "")
    if url and url.startswith(("http://", "https://")) and "#" not in url:
        listing["url"] = url
    spec = _first(_get(parts, "priceSpecification"))
    spec = spec if isinstance(spec, dict) else {}
    price = _amount(_get(parts, "price", "lowPrice") or spec.get("price"))
    if price:
        listing["price"] = price
        currency = _get(parts, "priceCurrency") or spec.get("priceCurrency")
        if isinstance(currency, str) and 3 <= len(currency.strip()) <= 4:
            listing["currency"] = currency.strip().upper()
    area = _area(_get(parts, "floorSize", "size", "area", "lotSize"))
    if area:
        listing["area_m2"] = area
    rooms = _amount(_get(parts, "numberOfRooms", "numberOfBedrooms", "numberOfBedroomsTotal"))
    if rooms and rooms < 100:
        listing["rooms"] = int(rooms)
    address = _address(_get(parts, "address"))
    if address:
        listing["address"] = address
    kind = next((t for part in parts for t in sorted(_types(part)) if t not in _GENERIC_TYPES and t in LISTING_TYPES),
                None)
    if kind:
        listing["property_type"] = kind
    deal = _deal(_get(parts, "businessFunction"))
    if deal:
        listing["deal"] = deal
    description = _text(_get(parts, "description"), 1500)
    if description:
        listing["description"] = description
    # A bare «Product: name» is a shop item, not a listing: keep only what carries a real-estate fact.
    if not any(k in listing for k in ("price", "area_m2", "rooms", "address")):
        return None
    return listing


def _from_opengraph(meta: dict[str, str], page_url: str) -> dict[str, Any] | None:
    price = _amount(meta.get("product:price:amount") or meta.get("og:price:amount"))
    if not price:
        return None
    listing: dict[str, Any] = {"price": price}
    currency = (meta.get("product:price:currency") or meta.get("og:price:currency") or "").strip().upper()
    if 3 <= len(currency) <= 4:
        listing["currency"] = currency
    if meta.get("og:title"):
        listing["title"] = _text(meta["og:title"], 300)
    listing["url"] = absolute(page_url, meta.get("og:url") or "") or page_url
    if meta.get("og:description"):
        listing["description"] = _text(meta["og:description"], 1500)
    return listing


# -- values --


def _text(value: Any, limit: int) -> str:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str)), "")
    return " ".join(str(value).split())[:limit] if isinstance(value, str | int | float) else ""


def _url_of(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("@id")
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str)), None)
    return value if isinstance(value, str) and value.strip() else None


def _amount(value: Any) -> float | None:
    """A positive number from 480000, "480000.00", "480.000 €" or "1,200"; None otherwise."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        match = _NUMBER.search(str(value))
        if not match:
            return None
        raw = match.group(0).replace(" ", "").replace(" ", "").rstrip(".,")
        if raw.count(",") + raw.count(".") > 1 or re.search(r"[.,]\d{3}$", raw):
            raw = raw.replace(",", "").replace(".", "")  # thousands separators
        else:
            raw = raw.replace(",", ".")
        try:
            number = float(raw)
        except ValueError:
            return None
    if number <= 0:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def _area(value: Any) -> float | None:
    unit = ""
    if isinstance(value, dict):
        unit = str(value.get("unitCode") or value.get("unitText") or "").lower()
    elif isinstance(value, str):
        unit = value.lower()
    number = _amount(value)
    if number is None:
        return None
    if unit in ("ftk", "sqft", "ft2") or "ft" in unit:
        return round(number * 0.092903, 1)
    if unit in ("har", "ha") or unit.endswith(" ha"):
        return round(number * 10_000, 1)
    return number


def _address(value: Any) -> str:
    if isinstance(value, str):
        return _text(value, 300)
    if not isinstance(value, dict):
        return ""
    parts = []
    for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
        part = value.get(key)
        if isinstance(part, dict):
            part = part.get("name")
        if isinstance(part, str | int) and str(part).strip() and str(part).strip() not in parts:
            parts.append(str(part).strip())
    return _text(", ".join(parts), 300)


def _deal(value: Any) -> str | None:
    text = " ".join(value) if isinstance(value, list) else str(value or "")
    text = text.lower()
    if "leaseout" in text or "lease" in text:
        return "rent"
    if "sell" in text:
        return "sale"
    return None
