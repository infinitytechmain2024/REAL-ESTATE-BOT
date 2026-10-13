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
