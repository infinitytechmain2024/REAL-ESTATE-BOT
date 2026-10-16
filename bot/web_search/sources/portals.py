"""Default-off Apify adapters for Spanish portals built on ``ApifyListingSource`` (one actor run per campaign).

* ``ApifyPisosSource``, ``ApifyHabitacliaSource``, ``ApifyMilanunciosSource`` — B5: input and output field names
  come from the actors' published schemas (Apify Store, read 2026-10-15), not from a run of ours.
* ``ApifyFotocasaSource`` — ``igolaizola/fotocasa-scraper``; the row shape below is taken from a live run
  (5 plots in Madrid, 2026-10-13): ``url``, ``transaction.{type,price}``, ``surface``, ``rooms``, ``phone``,
  ``agency.name``, ``location.level{2,5,7,8}Name``, ``location.{latitude,longitude}`` (strings), ``description``.

Every fact is copied from the row, never guessed: no price without a positive number, EUR because the portal
lists euros only, a plot area only for land and only when the description does not name a different plot.
"""

from __future__ import annotations

import math
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

from bot.utils.listing_text import _AREA, _to_m2, areas, number
from bot.web_search.queries import QueryTask, task_kind
from bot.web_search.sources.apify import ApifyListingSource, _distance_km, _fold, _number, _text
from bot.web_search.sources.base import SourceListing
from bot.web_search.urls import classify_url, deal_conflict, fetchable, host_of

_DESCRIPTION_CHARS = 1500
_DEALS = ("sale", "rent")


def _whole(value: Any) -> int | None:
    try:
        number = int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _point(lat: Any, lon: Any) -> tuple[float, float] | None:
    try:
        point = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return None
    ok = all(math.isfinite(v) for v in point) and -90 <= point[0] <= 90 and -180 <= point[1] <= 180
    return point if ok else None


_NOT_PLOT_BEFORE = re.compile(r"(?<!\w)(?:edificab|nave|edificio|cortijo|vivienda|casa|construid)", re.IGNORECASE)
_NOT_PLOT_AFTER = re.compile(r"t(?![a-z])|de\s+techo|(?:edificab|construid)", re.IGNORECASE)


def _not_plot(description: str) -> set[float]:
    """Areas the text gives to a building or to what may be built (``m²t``, ``de techo``, ``edificabilidad``,
    ``nave``, ``edificio``, ``cortijo``, ``vivienda``, ``casa``, ``construid``): never a plot."""
    out: set[float] = set()
    for found in _AREA.finditer(description):
        value = number(found.group(1))
        if value is None:
            continue
        before = description[max(0, found.start() - 40):found.start()]
        after = description[found.end():found.end() + 12].lstrip()
        if _NOT_PLOT_BEFORE.search(before) or _NOT_PLOT_AFTER.match(after):
            out.add(round(_to_m2(value, found.group(2)), 1))
    return out


def plot_area(reported: float | None, description: str) -> float | None:
    """The portal's area as the plot, unless the description names a plot more than 10 % away from it.

    When the portal reports no area (``None``; the actors leave ``areaSqm`` empty for most plots) the plot is read
    from the description, and only when that is unambiguous: one distinct area labelled as a plot, else one distinct
    area without any label. An area of a building or a buildable one (``_not_plot``) never counts; two candidates
    mean ``None``."""
    found = areas(description)
    if reported is None:
        skip = _not_plot(description)
        plots = {a.m2 for a in found if a.kind == "plot" and a.m2 not in skip}
        unlabelled = {a.m2 for a in found if a.kind is None and a.m2 not in skip}
        chosen = plots or unlabelled
        return next(iter(chosen)) if len(chosen) == 1 else None
    described = [area.m2 for area in found if area.kind == "plot"]
    return reported if all(abs(value - reported) <= reported * .10 for value in described) else None


def _phone(value: Any) -> str | None:
    text = _text(value)
    digits = sum(c.isdigit() for c in text)
    return text[:40] if 6 <= digits <= 15 else None


class _PortalSource(ApifyListingSource):
    """Shared task rules: Spain, real estate, a sale or a rent, a city (or a province) or an explicit radius."""

    kinds: ClassVar[dict[str, str]] = {}  # task kind -> the actor's propertyType
    deals: ClassVar[dict[str, str]] = {"sale": "sale", "rent": "rent"}  # campaign deal -> the actor's operation

    def supports(self, task: QueryTask) -> bool:
        return bool(self._token and task.location.strip() and task.country == "ES"
                    and task.vertical == "real_estate" and not task.suburbs_only
                    and task_kind(task) in self.kinds and task.constraints.get("deal") in _DEALS
                    and task.place_level in ("city", "province")
                    and (task.radius_km is None or (0 < task.radius_km <= 100 and self._geocoder is not None))
                    and not self._blocked(task))

    def _deal(self, task: QueryTask) -> str:
        return str(task.constraints.get("deal"))

    def _place_ok(self, task: QueryTask, center: tuple[float, float] | None, point: tuple[float, float] | None,
                  municipality: str, province: str) -> bool:
        if task.radius_km is not None:
            return center is not None and point is not None and _distance_km(center, point) <= task.radius_km
        wanted = {_fold(task.location), *(_fold(p) for p in task.nearby_places)}
        found = _fold(province) if task.place_level == "province" else _fold(municipality)
        return found in wanted

    def _url(self, value: Any, task: QueryTask) -> str | None:
        url = _text(value)
        if url.startswith("/"):
            url = f"https://www.{next(iter(self.hosts))}{url}"
        if (len(url) > 2048 or not fetchable(url, task.blocked_hosts) or host_of(url) not in self.hosts
                or classify_url(url) != "listing"):
            return None
        return url

    def _listing(self, task: QueryTask, *, url: str, title: str, price: Any, area: Any, rooms: Any,
                 address: list[str], description: str, phone: Any, advertiser: Any,
                 property_type: str | None) -> SourceListing:
        description = description[:_DESCRIPTION_CHARS]
        land = task_kind(task) == "land"
        reported = _number(area, positive=True)
        amount = _number(price, positive=True)
        count = _number(rooms, positive=True)
        place = ", ".join(dict.fromkeys(p for p in address if p))
        return SourceListing(
            url=url, title=title[:300], price=amount, currency="EUR" if amount is not None else None,
            area_m2=None if land else reported,
            rooms=int(count) if count is not None and count.is_integer() and count <= 99 else None,
            address=place[:300] or None, property_type=property_type, deal=self._deal(task),
            description=description, plot_m2=plot_area(reported, description) if land else None,
            phone=_phone(phone), advertiser=_text(advertiser)[:200] or None,
        )


class ApifyFotocasaSource(_PortalSource):
    name = "apify_fotocasa"
    hosts = frozenset({"fotocasa.es"})
    kinds: ClassVar[dict[str, str]] = {"land": "land", "house": "home", "apartment": "home", "commercial": "premises"}
    deals: ClassVar[dict[str, str]] = {"sale": "buy", "rent": "rent"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "commercial": "commercial"}
    _TITLES: ClassVar[dict[str, str]] = {"land": "Terreno", "house": "Vivienda", "apartment": "Vivienda", "commercial": "Local"}

    def __init__(self, token: str, *, actor_id: str = "igolaizola/fotocasa-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        kind, constraints = task_kind(task), task.constraints
        actor_input: dict[str, Any] = {"maxItems": amount, "operation": self.deals[self._deal(task)],
                                       "propertyType": self.kinds[kind], "sortBy": "latest"}
        center = None
        if task.radius_km is not None:
            center = await self._center(task) if not run_id else None
            if center is not None:
                actor_input |= {"latitude": round(center[0], 6), "longitude": round(center[1], 6),
                                "distanceKm": max(1, min(100, round(task.radius_km)))}
        else:
            actor_input |= {"location": task.location.strip()[:120],
                            "locationType": "province" if task.place_level == "province" else "municipality"}
        for key, field in (("minPrice", "min_price"), ("maxPrice", "max_price"), ("minSize", "min_area")):
            value = _whole(constraints.get(field))
            if value:
                actor_input[key] = value
        return self.actor_id, actor_input, center

    def _normalize(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> SourceListing | None:
        if not isinstance(item, dict):
            return None
        url = self._url(item.get("url"), task)
        transaction = item.get("transaction") if isinstance(item.get("transaction"), dict) else {}
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        if url is None or _text(transaction.get("type")).casefold() != self._deal(task):
            return None
        municipality, province = _text(location.get("level5Name")), _text(location.get("level2Name"))
        if not self._place_ok(task, center, _point(location.get("latitude"), location.get("longitude")),
                              municipality, province):
            return None
        kind = task_kind(task)
        agency = item.get("agency") if isinstance(item.get("agency"), dict) else {}
        neighbourhood = _text(location.get("level8Name")) or _text(location.get("level7Name"))
        title = f"{self._TITLES[kind]} en {'venta' if self._deal(task) == 'sale' else 'alquiler'}"
        return self._listing(
            task, url=url, title=f"{title}, {neighbourhood or municipality}".rstrip(", "),
            price=transaction.get("price"), area=item.get("surface"), rooms=item.get("rooms"),
            address=[_text(item.get("street")), neighbourhood, municipality, province],
            description=_text(item.get("description")), phone=item.get("phone"), advertiser=agency.get("name"),
            property_type=self._TYPES.get(kind))


_YAENCONTRE_PRICES = (30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 200, 250, 300, 400, 450, 500, 550, 600,
                      650, 700, 750, 800, 850, 900, 950, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900,
                      2000, 2300, 2500, 2600, 2900, 3000, 3500, 4000, 4500, 5000, 6000, 7000, 8000, 9000, 10000,
                      11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000, 20000, 25000, 30000, 50000,
                      75000, 80000, 100000, 125000, 150000, 175000, 200000, 225000, 250000, 275000, 300000, 350000,
                      400000, 450000, 500000, 600000, 700000, 800000, 900000, 1000000, 2000000, 3000000, 4000000,
                      5000000, 6000000, 7000000, 8000000)
_YAENCONTRE_AREAS = (50, 80, 100, 120, 150, 200, 300, 500, 700, 1000, 3000, 5000, 7000, 10000)


def _floor(value: int | None, allowed: tuple[int, ...]) -> str | None:
    """The largest allowed filter value not above ``value``: a lower bound that never drops a matching listing."""
    below = [a for a in allowed if value is not None and a <= value]
    return str(below[-1]) if below else None


def _ceil(value: int | None, allowed: tuple[int, ...]) -> str | None:
    """The smallest allowed filter value not below ``value``: an upper bound that never drops a matching listing."""
    above = [a for a in allowed if value is not None and a >= value]
    return str(above[0]) if above else None


class ApifyYaencontreSource(_PortalSource):
    """``igolaizola/yaencontre-scraper``; the row shape is taken from a live run (4 plots around Madrid, 2026-10-16):
    ``url``, ``title``, ``price`` (euros, no currency field), ``area`` (the plot for ``family=LAND``), ``family``
    (``LAND``, ``FLAT``, ``HOUSE``), ``operation`` (``BUY``/``RENT``), ``address.{qualifiedName,geoLocation}`` (the
    qualified name is ``district, borough, municipality``: the last part is the municipality),
    ``owner.{name,type,phoneAvailable,virtualPhoneNumber,source}``, ``description``.

    Input is by COORDINATES only: a text ``location`` makes the actor fail on the portal's autocomplete (HTTP 403,
    DataDome). Every task is therefore geocoded (``_center``) and sent as ``latitude``, ``longitude``, ``radiusKm``
    (+ ``operation``, ``propertyType``, ``maxItems``, ``sortBy``): the user's radius, else 15 km for a city and 60 km
    for a province. Without a geocoder the source is not served. The circle spills over the border (15 km around
    Madrid reaches Getafe), so a city task keeps only rows whose municipality is the city (or a named nearby place);
    a province task trusts the circle (the rows name no province), so a large province is covered only around its
    capital. ``owner.source=IDEALISTA`` marks an aggregated Idealista ad: expect duplicates of the Idealista source
    (``SourceListing`` has no field for it; the owner name stays in ``advertiser``). Live 2026-10-16: ``house`` rows are
    ``family=HOUSE`` (``area`` is the house); flat rent rows are ``operation=RENT``, ``periodicity=MONTHLY`` (``price`` per
    month; ``SourceListing`` has no period field); a "alquiler de temporada" ad is only marked in its title and is kept.
    Not checked live: ``premises`` and house rent."""

    name = "apify_yaencontre"
    hosts = frozenset({"yaencontre.com"})
    kinds: ClassVar[dict[str, str]] = {"land": "land", "house": "house", "apartment": "flat",
                                       "commercial": "premises"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "house": "house", "apartment": "apartment",
                                        "commercial": "commercial"}
    _OPERATIONS: ClassVar[dict[str, frozenset[str]]] = {"sale": frozenset({"sale", "venta", "buy"}),
                                                        "rent": frozenset({"rent", "alquiler"})}
    _FAMILIES: ClassVar[dict[str, frozenset[str]]] = {"land": frozenset({"land"}), "house": frozenset({"house"}),
                                                      "apartment": frozenset({"flat"})}  # commercial: not seen live
    _CITY_RADIUS_KM = 15
    _PROVINCE_RADIUS_KM = 60

    def __init__(self, token: str, *, actor_id: str = "igolaizola/yaencontre-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    def supports(self, task: QueryTask) -> bool:
        return self._geocoder is not None and super().supports(task)

    def _radius(self, task: QueryTask) -> float:
        if task.radius_km is not None:
            return task.radius_km
        return self._PROVINCE_RADIUS_KM if task.place_level == "province" else self._CITY_RADIUS_KM

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        constraints = task.constraints
        if run_id:  # resuming a paid run: never lose its dataset because the geocoder is down now
            try:
                center = await self._center(task)
            except Exception:  # noqa: BLE001 - rows are then filtered without the circle (see ``_normalize``)
                center = None
        else:
            center = await self._center(task)  # raises before any paid launch when the place cannot be geocoded
        actor_input: dict[str, Any] = {
            "maxItems": amount, "operation": self._deal(task), "propertyType": self.kinds[task_kind(task)],
            "sortBy": "newest"}
        if center is not None:
            actor_input |= {"latitude": round(center[0], 6), "longitude": round(center[1], 6),
                            "radiusKm": max(1, min(100, round(self._radius(task))))}
        filters = {"minPrice": _floor(_whole(constraints.get("min_price")), _YAENCONTRE_PRICES),
                   "maxPrice": _ceil(_whole(constraints.get("max_price")), _YAENCONTRE_PRICES),
                   "minArea": _floor(_whole(constraints.get("min_area")), _YAENCONTRE_AREAS)}
        actor_input |= {key: value for key, value in filters.items() if value}
        return self.actor_id, actor_input, center

    def _normalize(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> SourceListing | None:
        if not isinstance(item, dict):
            return None
        url = self._url(item.get("url"), task)
        operation = _fold(_text(item.get("operation")))
        period = _fold(_text(item.get("periodicity")))
        family = _fold(_text(item.get("family")))
        wanted_families = self._FAMILIES.get(task_kind(task))
        if (url is None or (operation and operation not in self._OPERATIONS[self._deal(task)])
                or (self._deal(task) == "rent" and period and period != "monthly")  # weekly/daily is not a month price
                or (family and wanted_families is not None and family not in wanted_families)):
            return None
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
        place = _text(address.get("qualifiedName"))
        geo = address.get("geoLocation") if isinstance(address.get("geoLocation"), dict) else {}
        if task.radius_km is not None or task.place_level == "province":
            point = _point(geo.get("lat"), geo.get("lon"))
            if center is None or point is None or _distance_km(center, point) > self._radius(task):
                return None
        else:
            municipality = _place_name(place.rsplit(",", 1)[-1])
            if municipality not in {_place_name(task.location), *(_place_name(p) for p in task.nearby_places)}:
                return None
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        phone = owner.get("virtualPhoneNumber") if owner.get("phoneAvailable") is not False else None
        kind = task_kind(task)
        return self._listing(
            task, url=url, title=_text(item.get("title")) or place, price=item.get("price"), area=item.get("area"),
            rooms=item.get("rooms"), address=[place], description=_text(item.get("description")), phone=phone,
            advertiser=owner.get("name"), property_type=self._TYPES.get(kind))


_IDEALISTA_PRICES = (500, 550, 600, 650, 700, 750, 800, 850, 900, 950, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700,
                     1800, 1900, 2000, 2100, 2400, 2700, 3000, 50000, 75000, 100000, 125000, 150000, 175000, 200000,
                     225000, 250000, 275000, 300000, 325000, 350000, 375000, 400000, 425000, 450000, 475000, 500000,
                     550000, 600000, 650000, 700000, 750000, 800000, 850000, 900000, 950000, 1000000, 1100000,
                     1200000, 1300000, 1400000, 1500000, 1600000, 1700000, 1800000, 1900000, 2000000, 2250000,
                     2500000, 2750000, 3000000, 3500000, 4000000)
_IDEALISTA_SIZES = (60, 80, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300)


class ApifyIdealistaPortalSource(_PortalSource):
    """Idealista through ``igolaizola/idealista-scraper`` (``APIFY_IDEALISTA_PROVIDER=igolaizola``), any city or
    province by name; rows follow the output schema the Store infers from its recent runs (``url``, ``operation``,
    ``propertyType``, ``municipality``, ``province``, ``price``, ``size``, ``rooms``, ``latitude``/``longitude``,
    ``contactInfo.{phone1,commercialName}``, ``suggestedTexts.title``). Checked against a live run (3 plots in
    Madrid, 2026-10-16): ``size`` is the plot in m2 for ``propertyType=land``, the rows carry no ``description`` and
    the price is also in ``priceInfo.price.{amount,currencySuffix}``."""

    name = "apify_idealista"
    hosts = frozenset({"idealista.com"})
    kinds: ClassVar[dict[str, str]] = {"land": "lands", "house": "homes", "apartment": "homes",
                                       "commercial": "premises"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "commercial": "commercial"}

    def __init__(self, token: str, *, actor_id: str = "igolaizola/idealista-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        constraints = task.constraints
        actor_input: dict[str, Any] = {"maxItems": amount, "operation": self._deal(task),
                                       "propertyType": self.kinds[task_kind(task)], "country": "es",
                                       "sortBy": "mostRecent"}
        center = None
        if task.radius_km is not None:
            center = await self._center(task) if not run_id else None
            if center is not None:
                actor_input |= {"latitude": round(center[0], 6), "longitude": round(center[1], 6),
                                "distanceKm": max(1, min(100, round(task.radius_km)))}
        else:
            actor_input["location"] = task.location.strip()[:120]
        filters = {"minPrice": _floor(_whole(constraints.get("min_price")), _IDEALISTA_PRICES),
                   "maxPrice": _ceil(_whole(constraints.get("max_price")), _IDEALISTA_PRICES),
                   "minSize": _floor(_whole(constraints.get("min_area")), _IDEALISTA_SIZES)}
        actor_input |= {key: value for key, value in filters.items() if value}
        return self.actor_id, actor_input, center

    def _normalize(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> SourceListing | None:
        if not isinstance(item, dict):
            return None
        url = self._url(item.get("url"), task)
        operation = _text(item.get("operation")).casefold()
        land = task_kind(task) == "land"
        kind = _text(item.get("propertyType")).casefold()
        if (url is None or (operation and operation != self._deal(task))
                or _text(item.get("country")).casefold() not in ("", "es")
                or (land and kind not in ("", "land", "lands"))):
            return None
        municipality, province = _text(item.get("municipality")), _text(item.get("province"))
        if not self._place_ok(task, center, _point(item.get("latitude"), item.get("longitude")),
                              municipality, province):
            return None
        contact = item.get("contactInfo") if isinstance(item.get("contactInfo"), dict) else {}
        phone = contact.get("phone1") if isinstance(contact.get("phone1"), dict) else {}
        texts = item.get("suggestedTexts") if isinstance(item.get("suggestedTexts"), dict) else {}
        street = _text(item.get("address"))  # already "Terreno en <calle>, <barrio>, <municipio>"
        shown = (item.get("priceInfo") or {}).get("price") if isinstance(item.get("priceInfo"), dict) else None
        shown = shown if isinstance(shown, dict) else {}
        suffix = _text(shown.get("currencySuffix")).casefold()
        price = item.get("price")
        if price is None and suffix in _EURO:  # the portal's own price block, only when it says euros
            price = shown.get("amount")
        return self._listing(
            task, url=url, title=_text(texts.get("title")) or f"Inmueble en {municipality}".strip(),
            price=price, area=item.get("size"), rooms=item.get("rooms"),
            address=[street, *(p for p in (municipality, province) if _fold(p) not in _fold(street))],
            description=_text(item.get("description")),
            phone=(phone.get("phoneNumberForMobileDialing") or phone.get("phoneNumber")
                   or phone.get("formattedPhone")),
            advertiser=contact.get("commercialName") or contact.get("contactName"),
            property_type=self._TYPES.get(task_kind(task)))


_EURO = frozenset({"eur", "euro", "euros", "\u20ac"})
_OFFER_WORDS = frozenset({"supply", "sell", "sale", "offer", "oferta", "venta", "rent", "alquiler"})
_PISOS_CITIES = frozenset({"madrid", "barcelona", "valencia", "sevilla", "malaga"})  # slug ``<name>_capital``
_PISOS_PROVINCES = frozenset({"madrid", "barcelona", "valencia", "sevilla", "malaga", "alicante", "zaragoza", "cadiz"})


def _slug(value: str) -> str:
    """Lowercase ASCII with ``_`` for spaces, the slug form the portal actors use."""
    return re.sub(r"[^a-z0-9]+", "_", _fold(value)).strip("_")


def _place_name(value: str) -> str:
    """A place name as the portals write it: ``Madrid Capital`` is Madrid."""
    folded = _fold(value)
    return folded[: -len(" capital")] if folded.endswith(" capital") and len(folded) > 8 else folded


_SALE = frozenset({"sale", "venta"})
_RENT = frozenset({"rent", "alquiler"})
_LAND_WORDS = ("land", "terren", "solar", "parcel", "rural", "finca")


def _landish(value: Any) -> bool:
    """True for a missing value or one that names land (a plot, a rural estate); a house or a flat is not."""
    text = _fold(_text(value))
    return not text or any(word in text for word in _LAND_WORDS)


def _pages(amount: int, per_page: int) -> int:
    return max(1, -(-amount // per_page))


class _SchemaPortal(_PortalSource):
    """Shared rules of the three schema-based portal actors: city or province tasks only (no radius), sale or rent
    read from the row's ``transactionType`` when it has one and from the URL otherwise."""

    kinds: ClassVar[dict[str, str]] = {"land": "land", "house": "house", "apartment": "apartment",
                                       "commercial": "commercial"}
    _TITLES: ClassVar[dict[str, str]] = {"land": "Terreno", "house": "Casa", "apartment": "Piso", "commercial": "Local"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "house": "house", "apartment": "apartment",
                                        "commercial": "commercial"}

    def supports(self, task: QueryTask) -> bool:
        return task.radius_km is None and super().supports(task)

    def _filters(self, task: QueryTask, names: tuple[str, str, str]) -> dict[str, int]:
        found = {}
        for key, field in zip(names, ("min_price", "max_price", "min_area"), strict=True):
            value = _whole(task.constraints.get(field))
            if value:
                found[key] = value
        return found

    def _row_ok(self, item: Any, task: QueryTask) -> tuple[dict[str, Any], str] | None:
        """(row, url) when the row is a dict with a listing URL of this portal and no other deal."""
        if not isinstance(item, dict):
            return None
        url = self._url(item.get("detailUrl", item.get("url")), task)
        wanted = self._deal(task)
        row_deal = _fold(_text(item.get("transactionType")))
        if url is None or (row_deal and row_deal not in (_SALE if wanted == "sale" else _RENT)) or deal_conflict(url, wanted):
            return None
        return item, url

    def _place_ok(self, task: QueryTask, center: tuple[float, float] | None, point: tuple[float, float] | None,
                  municipality: str, province: str) -> bool:
        return super()._place_ok(task, center, point, _place_name(municipality), _place_name(province))

    def _price(self, item: dict[str, Any]) -> Any:
        currency = _fold(_text(item.get("priceCurrency")))
        return None if currency and currency not in _EURO else item.get("price")

    def _title(self, item: dict[str, Any], task: QueryTask, place: str) -> str:
        deal = "venta" if self._deal(task) == "sale" else "alquiler"
        return _text(item.get("title")) or f"{self._TITLES[task_kind(task)]} en {deal}, {place}".rstrip(", ")


class ApifyPisosSource(_SchemaPortal):
    """Pisos.com through ``logiover/pisos-com-property-scraper`` ($0.0035 per result, $0.00005 per start).

    Input (published schema): ``transactionType`` (sale/rent), ``propertyType`` (land, house, apartment, commercial),
    ``location`` slug (``madrid_capital`` for a city, ``madrid`` for a province; only the cities and provinces in
    ``_PISOS_CITIES``/``_PISOS_PROVINCES`` are served, any other place is not supported), ``sortBy``, ``maxListings``,
    ``maxPages`` (about 33 per page), ``fetchDetails=false`` (the mapper needs nothing from the detail page, and a detail run is slower),
    ``minPrice``/``maxPrice``/``minArea``. Output: ``detailUrl``, ``price`` + ``priceCurrency``, ``areaSqm``,
    ``rooms``, ``city``, ``province``, ``fullAddress``, ``advertiserName``, ``shortDescription``. There is no phone
    in the output (``phone`` stays empty); ``terrainAreaSqm`` is always null today and is not used: a plot is
    ``plot_area(areaSqm, shortDescription)``. Live run 2026-10-16 (plots, Madrid; 4 rows received): ``areaSqm`` was null in 1 of 4
    rows (its text names no area), ``advertiserType`` may be null, ``city`` is ``Madrid Capital``.
    """

    name = "apify_pisos"
    hosts = frozenset({"pisos.com"})

    def __init__(self, token: str, *, actor_id: str = "logiover/pisos-com-property-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    @staticmethod
    def _location(task: QueryTask) -> str | None:
        slug = _slug(task.location)
        if task.place_level == "province":
            return slug if slug in _PISOS_PROVINCES else None
        return f"{slug}_capital" if slug in _PISOS_CITIES else None

    def supports(self, task: QueryTask) -> bool:
        return super().supports(task) and self._location(task) is not None

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        actor_input: dict[str, Any] = {
            "transactionType": self._deal(task), "propertyType": self.kinds[task_kind(task)],
            "location": self._location(task), "sortBy": "newest", "maxListings": amount,
            "maxPages": _pages(amount, 30), "fetchDetails": False}
        actor_input |= self._filters(task, ("minPrice", "maxPrice", "minArea"))
        return self.actor_id, actor_input, None

    def _normalize(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> SourceListing | None:
        found = self._row_ok(item, task)
        if found is None:
            return None
        row, url = found
        city, province = _text(row.get("city")), _text(row.get("province"))
        if (not self._place_ok(task, None, None, city, province)
                or (task_kind(task) == "land" and not _landish(row.get("propertyType")))):
            return None
        return self._listing(
            task, url=url, title=self._title(row, task, city), price=self._price(row), area=row.get("areaSqm"),
            rooms=row.get("rooms"),
            address=[_text(row.get("fullAddress"))] if row.get("fullAddress") else [city, province],
            description=_text(row.get("shortDescription")), phone=None, advertiser=row.get("advertiserName"),
            property_type=self._TYPES.get(task_kind(task)))


class ApifyHabitacliaSource(_SchemaPortal):
    """Habitaclia through ``trev0n/habitaclia-com-spain-scraper`` ($0.001 per result, $0.00005 per start).

    Input (published schema): ``transactionType`` (sale/rent), ``propertyType`` (land, house, apartment, commercial),
    ``location`` (free text, the actor makes the slug), ``sort``, ``maxListings``, ``maxPages`` (about 25 per page),
    ``fetchPropertyDetails=false``, ``minPrice``/``maxPrice``/``minArea``. Output: ``detailUrl``, ``price`` +
    ``priceCurrency``, ``areaSqm``, ``rooms``, ``city``, ``province``, ``location``, ``advertiserName``,
    ``description``/``shortDescription``. There is no phone and no separate plot area (``areaSqm`` + the text:
    ``plot_area``). Live run 2026-10-16 (3 plots, Madrid; 3 rows received): ``areaSqm`` was null in all of them, the area is only in the
    text; ``city`` is ``Madrid Capital``, ``propertySubtype`` is ``industrialland``, ``residentialland`` or null.
    """

    name = "apify_habitaclia"
    hosts = frozenset({"habitaclia.com"})

    def __init__(self, token: str, *, actor_id: str = "trev0n/habitaclia-com-spain-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        actor_input: dict[str, Any] = {
            "transactionType": self._deal(task), "propertyType": self.kinds[task_kind(task)],
            "location": task.location.strip()[:120], "sort": "newest", "maxListings": amount,
            "maxPages": _pages(amount, 25), "fetchPropertyDetails": False}
        actor_input |= self._filters(task, ("minPrice", "maxPrice", "minArea"))
        return self.actor_id, actor_input, None

    def _normalize(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> SourceListing | None:
        found = self._row_ok(item, task)
        if found is None:
            return None
        row, url = found
        city, province = _text(row.get("city")), _text(row.get("province"))
        if (not self._place_ok(task, None, None, city, province)
                or (task_kind(task) == "land" and not (_landish(row.get("propertyType"))
                                                       and _landish(row.get("propertySubtype"))))):
            return None
        return self._listing(
            task, url=url, title=self._title(row, task, city), price=self._price(row), area=row.get("areaSqm"),
            rooms=row.get("rooms"),
            address=[place := _text(row.get("location")), *(p for p in (city, province) if _fold(p) not in _fold(place))],
            description=_text(row.get("description")) or _text(row.get("shortDescription")), phone=None,
            advertiser=row.get("advertiserName"), property_type=self._TYPES.get(task_kind(task)))


class ApifyMilanunciosSource(_SchemaPortal):
    """Milanuncios through ``rastriq/milanuncios-scraper`` ($0.00125 per result, $0.005 per start).

    Input (live runs, 2026-10-16): ``categorySlug`` + ``province`` (slug: ``madrid``, ``barcelona``, ``vizcaya``,
    ``las_palmas``; an unknown slug stops the run), ``maxItems``, ``maxPages`` (about 41 per page), ``fetchDetails=false``
    and ``stripSellerPII=true`` always (no private seller ids, phones or e-mails). ``searchUrl`` is NOT used: with
    ``/terrenos/`` it ignored ``keywords`` and returned the whole country. The province is the task's place for a
    province task, and for a city only when the city is its own province (the five capitals of ``_PISOS_CITIES``);
    any other city has no known province, so it is not served. A city task keeps only rows of that ``cityName``.
    ``proxyConfiguration`` is the actor's prefill (residential, ES). Offers only: ``sellType`` ``demand`` is dropped.
    Categories: LAND is ``solares`` (urban, checked live); ``venta-de-fincas`` (rural land) exists but is NOT covered
    (one run per campaign, one category; its rows are accepted if they ever arrive); ``venta-de-terrenos`` does not
    exist (404). ``venta-de-pisos``, ``alquiler-de-pisos``, ``venta-de-casas``, ``alquiler-de-casas`` and premises are checked live; premises are
    input ``locales-comerciales`` (``venta-de-locales`` is 404; rows say ``venta-de-locales-comerciales``).
    A rent of land or of premises is not supported. A row whose category
    (``categorySlug`` and URL prefix) is not the task's is dropped.
    Output (live): ``url`` (``/solares-en-<city>-<province>/<slug>-<id>.htm``), ``title``, ``price`` (euros; 0 is
    "precio a convenir" and becomes None), ``description``, ``tag_sqm`` (the plot), ``tag_bedrooms``, ``cityName``,
    ``provinceName``, ``sellerType``, ``sellType``, ``categorySlug``, ``latitude``/``longitude``. No phone and no
    advertiser name; a plot is ``plot_area(tag_sqm, description)``.
    """

    name = "apify_milanuncios"
    hosts = frozenset({"milanuncios.com"})
    _CATEGORIES: ClassVar[dict[tuple[str, str], str]] = {
        ("sale", "land"): "solares", ("sale", "apartment"): "venta-de-pisos",
        ("rent", "apartment"): "alquiler-de-pisos", ("sale", "house"): "venta-de-casas",
        ("rent", "house"): "alquiler-de-casas", ("sale", "commercial"): "locales-comerciales"}
    _LAND_CATEGORIES: ClassVar[tuple[str, ...]] = ("solares", "venta-de-fincas")
    # what a row of each task may carry in ``categorySlug`` / the URL prefix (the input slug of premises differs
    # from the output one: live 2026-10-16, input ``locales-comerciales`` -> ``venta-de-locales-comerciales``)
    _ROW_CATEGORIES: ClassVar[dict[tuple[str, str], tuple[str, ...]]] = {
        ("sale", "land"): _LAND_CATEGORIES, ("sale", "apartment"): ("venta-de-pisos",),
        ("rent", "apartment"): ("alquiler-de-pisos",), ("sale", "house"): ("venta-de-casas",),
        ("rent", "house"): ("alquiler-de-casas",), ("sale", "commercial"): ("venta-de-locales-comerciales",)}
    # slugs seen in live runs or documented by the actor; extend only after a live check
    _PROVINCES: ClassVar[frozenset[str]] = frozenset({"madrid", "barcelona", "valencia", "sevilla", "malaga",
                                                      "vizcaya", "las_palmas"})

    def __init__(self, token: str, *, actor_id: str = "rastriq/milanuncios-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    def supports(self, task: QueryTask) -> bool:
        return (super().supports(task) and self._province(task) is not None
                and (str(task.constraints.get("deal")), task_kind(task)) in self._CATEGORIES)

    @staticmethod
    def _province(task: QueryTask) -> str | None:
        """The province slug when it is a known one: the place of a province task; a city only when it is its own
        province (the capitals). Any other slug is not guessed."""
        slug = _slug(task.location)
        if slug not in ApifyMilanunciosSource._PROVINCES:
            return None
        return slug if task.place_level == "province" or slug in _PISOS_CITIES else None

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        actor_input: dict[str, Any] = {
            "categorySlug": self._CATEGORIES[(self._deal(task), task_kind(task))], "province": self._province(task),
            "maxItems": amount, "maxPages": _pages(amount, 41), "fetchDetails": False, "stripSellerPII": True,
            "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"],
                                   "apifyProxyCountry": "ES"}}
        return self.actor_id, actor_input, None

    def _normalize(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> SourceListing | None:
        found = self._row_ok(item, task)
        if found is None:
            return None
        row, url = found
        city, province = _text(row.get("cityName")), _text(row.get("provinceName"))
        sell = _fold(_text(row.get("sellType")))
        category = _fold(_text(row.get("categorySlug")))
        first = urlsplit(url).path.strip("/").split("/")[0]
        wanted = self._ROW_CATEGORIES.get((self._deal(task), task_kind(task)), ())
        if (not self._place_ok(task, None, None, city, province)
                or (sell and sell not in _OFFER_WORDS)  # a wanted-ad ("demand", "busco") is not an offer
                or not first.startswith(tuple(f"{c}-en-" for c in wanted))
                or (category and category not in wanted)):  # the row must belong to the task's category
            return None
        return self._listing(
            task, url=url, title=self._title(row, task, city), price=row.get("price"), area=row.get("tag_sqm"),
            rooms=row.get("tag_bedrooms"), address=[city, province], description=_text(row.get("description")),
            phone=None, advertiser=None, property_type=self._TYPES.get(task_kind(task)))
