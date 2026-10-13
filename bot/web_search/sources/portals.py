"""Default-off Apify adapters for Spanish portals built on ``ApifyListingSource`` (one actor run per campaign).

* ``ApifyFotocasaSource`` — ``igolaizola/fotocasa-scraper``; the row shape below is taken from a live run
  (5 plots in Madrid, 2026-10-13): ``url``, ``transaction.{type,price}``, ``surface``, ``rooms``, ``phone``,
  ``agency.name``, ``location.level{2,5,7,8}Name``, ``location.{latitude,longitude}`` (strings), ``description``.

Every fact is copied from the row, never guessed: no price without a positive number, EUR because the portal
lists euros only, a plot area only for land and only when the description does not name a different plot.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

from bot.utils.listing_text import areas
from bot.web_search.queries import QueryTask, task_kind
from bot.web_search.sources.apify import ApifyListingSource, _distance_km, _fold, _number, _text
from bot.web_search.sources.base import SourceListing
from bot.web_search.urls import classify_url, fetchable, host_of

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


def plot_area(reported: float | None, description: str) -> float | None:
    """The portal's area as the plot, unless the description names a plot more than 10 % away from it."""
    if reported is None:
        return None
    described = [area.m2 for area in areas(description) if area.kind == "plot"]
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
    """``igolaizola/yaencontre-scraper``; rows follow the output schema the Store infers from its recent runs
    (``url``, ``title``, ``price``, ``area``, ``rooms``, ``operation``, ``address.{qualifiedName,geoLocation}``,
    ``owner.{name,phoneAvailable,virtualPhoneNumber}``, ``description``), not a run of ours."""

    name = "apify_yaencontre"
    hosts = frozenset({"yaencontre.com"})
    kinds: ClassVar[dict[str, str]] = {"land": "land", "house": "house", "apartment": "flat",
                                       "commercial": "premises"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "house": "house", "apartment": "apartment",
                                        "commercial": "commercial"}
    _OPERATIONS: ClassVar[dict[str, frozenset[str]]] = {"sale": frozenset({"sale", "venta", "buy"}),
                                                        "rent": frozenset({"rent", "alquiler"})}

    def __init__(self, token: str, *, actor_id: str = "igolaizola/yaencontre-scraper", **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        constraints = task.constraints
        actor_input: dict[str, Any] = {"maxItems": amount, "operation": self._deal(task),
                                       "propertyType": self.kinds[task_kind(task)], "sortBy": "newest"}
        center = None
        if task.radius_km is not None:
            center = await self._center(task) if not run_id else None
            if center is not None:
                actor_input |= {"latitude": round(center[0], 6), "longitude": round(center[1], 6),
                                "radiusKm": max(1, min(100, round(task.radius_km)))}
        else:
            actor_input |= {"location": task.location.strip()[:120],
                            "locationType": "province" if task.place_level == "province" else "municipality"}
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
        if url is None or (operation and operation not in self._OPERATIONS[self._deal(task)]):
            return None
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
        place = _text(address.get("qualifiedName"))
        geo = address.get("geoLocation") if isinstance(address.get("geoLocation"), dict) else {}
        if task.radius_km is not None:
            if not self._place_ok(task, center, _point(geo.get("lat"), geo.get("lon")), "", ""):
                return None
        elif task.place_level == "city":
            parts = {_fold(p) for p in place.split(",")}
            if not parts & {_fold(task.location), *(_fold(p) for p in task.nearby_places)}:
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
    ``contactInfo.{phone1,commercialName}``, ``suggestedTexts.title``), not a run of ours."""

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
        return self._listing(
            task, url=url, title=_text(texts.get("title")) or f"Inmueble en {municipality}".strip(),
            price=item.get("price"), area=item.get("size"), rooms=item.get("rooms"),
            address=[_text(item.get("address")), municipality, province], description=_text(item.get("description")),
            phone=phone.get("phoneNumberForMobileDialing") or phone.get("formattedPhone"),
            advertiser=contact.get("commercialName") or contact.get("contactName"),
            property_type=self._TYPES.get(task_kind(task)))
