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

from bot.utils.listing_text import _AREA, _to_m2, areas, auction_named, number
from bot.web_search.queries import QueryTask, task_kind
from bot.web_search.sources.apify import (
    ApifyListingSource,
    _distance_km,
    _execution,
    _fold,
    _number,
    _text,
)
from bot.web_search.sources.base import SourceListing
from bot.web_search.urls import classify_url, deal_conflict, fetchable, host_of

_DESCRIPTION_CHARS = 1500
# PORTAL_DESCRIPTION_FULL (Q18): the description up to the analysis limit, not 1500 characters. Pilot G3 lost the
# «piscina» of 4 houses at characters 1693, 2036, 2097 and 3408. 8000 = ``WebSearchConfig.max_post_chars``
# (bot/web_search/worker.py), the size of the post the analysis gets: ``_source_text`` cuts the description again
# to fit it after the facts block, so a longer limit here would never reach the model.
_DESCRIPTION_CHARS_FULL = 8000
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


def _reference(value: Any) -> str | None:
    """The agency's reference of the ad as the portal lists it (a string or a whole number), at most 80 characters."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _text(value)[:80] or None


# PORTAL_FACTS_FROM_ROW (Q16). With the flag the facts line carries the portal's own type and deal, never the task's:
# a value the row does not give leaves the field out. The words are the rows' (recorded output of the portal actors,
# 2026-10/11): Habitaclia ``propertyType`` flat/house/land (+ ``propertySubtype`` groundfloor, terracedhouse,
# pairedhouse, chalet, industrialland, residentialland), Idealista ``propertyType`` land/flat/studio/chalet/duplex/
# penthouse, Yaencontre ``family`` LAND/FLAT/HOUSE, Milanuncios ``categorySlug``. A word not listed here is passed as
# the portal wrote it (folded); a bare code is not (``_row_type``).
_ROW_TYPES: dict[str, str] = {
    "land": "landparcel", "lands": "landparcel", "industrialland": "landparcel", "residentialland": "landparcel",
    "flat": "apartment", "apartment": "apartment", "studio": "apartment", "duplex": "apartment",
    "penthouse": "apartment", "groundfloor": "apartment",
    "house": "house", "chalet": "house", "terracedhouse": "house", "pairedhouse": "house",
    "independanthouse": "house", "premises": "commercial", "commercial": "commercial",
}
_ROW_DEALS: dict[str, str] = {"sale": "sale", "venta": "sale", "buy": "sale", "rent": "rent", "alquiler": "rent"}
# Milanuncios ``categorySlug`` -> (type, deal); «solares» names no deal. The input slugs of ``_CATEGORIES`` and the
# output slug of premises (``venta-de-locales-comerciales``, live 2026-10-16).
_MILANUNCIOS_ROW: dict[str, tuple[str, str | None]] = {
    "solares": ("landparcel", None), "venta-de-fincas": ("landparcel", "sale"),
    "venta-de-pisos": ("apartment", "sale"), "alquiler-de-pisos": ("apartment", "rent"),
    "venta-de-casas": ("house", "sale"), "alquiler-de-casas": ("house", "rent"),
    "venta-de-locales-comerciales": ("commercial", "sale"),
}
# Fotocasa's ``propertyType`` is a code. Only the two seen in the recorded output (80 rows): ``4`` on every
# ``/comprar/terreno/`` URL, ``2`` on every ``/comprar/vivienda/`` one (flats and houses alike: «vivienda», no more).
# ``propertySubtype`` codes and ``transactionType`` ``1`` are not mapped (their meaning is not in the output).
_FOTOCASA_TYPES: dict[str, str] = {"4": "landparcel", "2": "vivienda"}


def _row_type(*values: Any, land: bool = False) -> str | None:
    """The first of the row's own type values: our word for a known one, the portal's word for another, None for
    a missing value or a bare code. ``land``: the row passed a land task's filter (``_landish``), so a word naming
    land (Pisos ``ruralEstate``, Habitaclia «Terreno urbano» / «Terreno rústico») is ``landparcel``."""
    for value in values:
        word = _fold(_text(value))
        if word and not word.isdigit():
            return "landparcel" if land and _landish(word) else _ROW_TYPES.get(word, word[:40])
    return None


def _code(value: Any) -> str:
    """A portal code as text: the number ``4`` and the string ``"4"`` alike."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value)


def _row_deal(value: Any) -> str | None:
    return _ROW_DEALS.get(_fold(_text(value)))


# PORTAL_RENTAL_TERM (Q20): the row's own rental term, never guessed from the text. Recorded output (Barcelona flat
# rent, 2026-11): Fotocasa ``rentalType`` is LONG_TERM, TEMPORARY or null; Idealista igolaizola marks a seasonal let
# with the label ``{"name": "seasonalRental", "text": "Alquiler de temporada"}`` and has no long-term mark (a row
# without the label is not known to be long-term). Habitaclia ``trev0n`` and Milanuncios ``rastriq`` rows carry no
# term field: nothing is written for them.
_RENTAL_TERMS = frozenset({"long", "temporary"})
_FOTOCASA_RENTAL_TERMS = {"LONG_TERM": "long", "TEMPORARY": "temporary"}


def _fotocasa_rental_term(value: Any) -> str | None:
    return _FOTOCASA_RENTAL_TERMS.get(_text(value).upper())


def _idealista_rental_term(labels: Any) -> str | None:
    if not isinstance(labels, list):
        return None
    names = {_text(label.get("name")) for label in labels if isinstance(label, dict)}
    return "temporary" if "seasonalRental" in names else None


class _PortalSource(ApifyListingSource):
    """Shared task rules: Spain, real estate, a sale or a rent, a city (or a province) or an explicit radius."""

    kinds: ClassVar[dict[str, str]] = {}  # task kind -> the actor's propertyType
    deals: ClassVar[dict[str, str]] = {"sale": "sale", "rent": "rent"}  # campaign deal -> the actor's operation
    agency_ref = False  # DEDUP_DISTRICT_NOT_STREET (Q15): pass the row's agency reference on

    def __init__(self, token: str, *, facts_from_row: bool = False, description_full: bool = False,
                 rental_term: bool = False, **kwargs: Any) -> None:
        super().__init__(token, **kwargs)
        self.facts_from_row = facts_from_row  # PORTAL_FACTS_FROM_ROW (Q16)
        self.description_full = description_full  # PORTAL_DESCRIPTION_FULL (Q18)
        self.rental_term = rental_term  # PORTAL_RENTAL_TERM (Q20)

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

    def _place_wrong(self, task: QueryTask, center: tuple[float, float] | None, point: tuple[float, float] | None,
                     municipality: str, province: str) -> bool:
        """The row says it is elsewhere (``_place_ok`` fails on a fact the row gives, not on a missing one)."""
        if task.radius_km is not None:
            return center is not None and point is not None and _distance_km(center, point) > task.radius_km
        found = province if task.place_level == "province" else municipality
        return bool(_fold(found)) and not self._place_ok(task, center, point, municipality, province)

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
                 property_type: str | None, place_unverified: bool = False,
                 row_type: str | None = None, row_deal: str | None = None, reference: Any = None,
                 rental_term: str | None = None) -> SourceListing:
        """``property_type`` and the deal are the task's; with ``facts_from_row`` (Q16) ``row_type``/``row_deal``,
        the row's own (None: not in the row), are written instead, and an auction named in the row is marked.
        ``rental_term`` (the row's own, ``_RENTAL_TERMS``) is written only with PORTAL_RENTAL_TERM (Q20) and a rent."""
        auction = self.facts_from_row and auction_named(title, description)
        if self.facts_from_row:
            property_type, deal = row_type, row_deal
        else:
            deal = self._deal(task)
        description = description[:_DESCRIPTION_CHARS_FULL if self.description_full else _DESCRIPTION_CHARS]
        land = task_kind(task) == "land"
        reported = _number(area, positive=True)
        amount = _number(price, positive=True)
        count = _number(rooms, positive=True)
        place = ", ".join(dict.fromkeys(p for p in address if p))
        return SourceListing(
            url=url, title=title[:300], price=amount, currency="EUR" if amount is not None else None,
            area_m2=None if land else reported,
            rooms=int(count) if count is not None and count.is_integer() and count <= 99 else None,
            address=place[:300] or None, property_type=property_type, deal=deal,
            description=description, plot_m2=plot_area(reported, description) if land else None,
            phone=_phone(phone), advertiser=_text(advertiser)[:200] or None,
            place_unverified=True if place_unverified else None, auction=True if auction else None,
            agency_ref=_reference(reference) if self.agency_ref else None,
            rental_term=(rental_term if self.rental_term and self._deal(task) == "rent" and rental_term in _RENTAL_TERMS
                         else None),
        )


# PORTAL_DEFAULT_POINT_GUARD (Q12). Fotocasa files an ad whose advertiser gave no usable address at a fallback point
# of the searched city and names the district of that point (``level7/8Name``): in a live run «plots in Madrid»
# (2026-11, pilot G1) plots in Móstoles, Torroella de Fluvià, Molina de Segura, Llubí and Vinaròs all sat within
# 0.32 km of one of these points, each with the point's ``level8Name``. The sign is three facts together:
# * the row's ``level8Name`` is one of these neighbourhoods AND its point is within ``_DEFAULT_POINT_KM`` of the
#   neighbourhood's fallback point (the portal jitters the point by up to ~0.3 km, so equal coordinates are no sign);
# * the description does not name that neighbourhood (``_DEFAULT_POINTS`` synonyms, whole words; an empty
#   description names nothing): a flat «en pleno Sol» really is in Sol, and a 0.5 km circle covers most of Sol and
#   Cortes - Huertas;
# * the task is for land (``ApifyFotocasaSource._normalize``): all the evidence is plots; a flat or a shop in central
#   Madrid is ordinary, a plot there is not, so homes and premises are never marked.
# On the recorded output (37 distinct rows of two Madrid land runs) it marks the 9 misplaced rows and no other; equal
# coordinates to 3 decimals would mark 2 of them and 6 real plots of one La Florida development, to 4 decimals none.
# A marked object goes to «similar» (campaign/tolerance.py), never lost. The reduction agents' gate
# (bot/agents/gate.py, AGENT_REDUCTION_ENABLED) classifies its own extraction and does not read ``place_unverified``.
_DEFAULT_POINTS: dict[str, tuple[tuple[float, float], tuple[str, ...]]] = {
    "ciudad universitaria": ((40.4625, -3.749), ("ciudad universitaria",)),
    "cortes - huertas": ((40.417, -3.700), ("cortes - huertas", "huertas", "barrio de las letras")),
    "sol": ((40.418, -3.705), ("sol", "puerta del sol")),
}
_DEFAULT_POINT_KM = 0.5


def default_point(neighbourhood: str, point: tuple[float, float] | None, description: str = "") -> bool:
    """The row sits at one of Fotocasa's fallback points of its ``level8Name`` and its description does not name
    that neighbourhood (see ``_DEFAULT_POINTS``)."""
    known = _DEFAULT_POINTS.get(" ".join(_fold(neighbourhood).split()))
    if known is None or point is None or _distance_km(known[0], point) > _DEFAULT_POINT_KM:
        return False
    text = " ".join(_fold(description).split())
    return not any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) for name in known[1])


class ApifyFotocasaSource(_PortalSource):
    name = "apify_fotocasa"
    hosts = frozenset({"fotocasa.es"})
    kinds: ClassVar[dict[str, str]] = {"land": "land", "house": "home", "apartment": "home", "commercial": "premises"}
    deals: ClassVar[dict[str, str]] = {"sale": "buy", "rent": "rent"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "commercial": "commercial"}
    _TITLES: ClassVar[dict[str, str]] = {"land": "Terreno", "house": "Vivienda", "apartment": "Vivienda", "commercial": "Local"}

    def __init__(self, token: str, *, actor_id: str = "igolaizola/fotocasa-scraper",
                 default_point_guard: bool = False, agency_ref: bool = False, **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)
        self.default_point_guard = default_point_guard  # PORTAL_DEFAULT_POINT_GUARD (Q12)
        self.agency_ref = agency_ref  # DEDUP_DISTRICT_NOT_STREET (Q15)

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
        point = _point(location.get("latitude"), location.get("longitude"))
        if not self._place_ok(task, center, point, municipality, province):
            return None
        kind = task_kind(task)
        agency = item.get("agency") if isinstance(item.get("agency"), dict) else {}
        neighbourhood = _text(location.get("level8Name")) or _text(location.get("level7Name"))
        # Q12: at a fallback point the district, town and province are the point's, not the object's: not passed on.
        description = _text(item.get("description"))
        unverified = (self.default_point_guard and kind == "land"
                      and default_point(_text(location.get("level8Name")), point, description))
        place = [] if unverified else [neighbourhood, municipality, province]
        title = f"{self._TITLES[kind]} en {'venta' if self._deal(task) == 'sale' else 'alquiler'}"
        return self._listing(
            task, url=url, title=f"{title}, {'' if unverified else neighbourhood or municipality}".rstrip(", "),
            price=transaction.get("price"), area=item.get("surface"), rooms=item.get("rooms"),
            address=[_text(item.get("street")), *place],
            description=description, phone=item.get("phone"), advertiser=agency.get("name"),
            property_type=self._TYPES.get(kind), place_unverified=unverified,
            row_type=_FOTOCASA_TYPES.get(_code(item.get("propertyType"))), row_deal=_row_deal(transaction.get("type")),
            reference=item.get("agencyReference"), rental_term=_fotocasa_rental_term(item.get("rentalType")))


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
            advertiser=owner.get("name"), property_type=self._TYPES.get(kind),
            row_type=_row_type(item.get("family")), row_deal=_row_deal(item.get("operation")))


_IDEALISTA_PRICES = (500, 550, 600, 650, 700, 750, 800, 850, 900, 950, 1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700,
                     1800, 1900, 2000, 2100, 2400, 2700, 3000, 50000, 75000, 100000, 125000, 150000, 175000, 200000,
                     225000, 250000, 275000, 300000, 325000, 350000, 375000, 400000, 425000, 450000, 475000, 500000,
                     550000, 600000, 650000, 700000, 750000, 800000, 850000, 900000, 950000, 1000000, 1100000,
                     1200000, 1300000, 1400000, 1500000, 1600000, 1700000, 1800000, 1900000, 2000000, 2250000,
                     2500000, 2750000, 3000000, 3500000, 4000000)
_IDEALISTA_SIZES = (60, 80, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300)


class _CityCircle(tuple[float, float]):
    """A city's centre (``(lat, lon)``, usable wherever a centre is) with the circle sent to the actor (Q17)."""

    radius_km: float

    def __new__(cls, center: tuple[float, float], radius_km: float = 15) -> _CityCircle:
        circle = super().__new__(cls, center)
        circle.radius_km = radius_km
        return circle

    def __getnewargs__(self) -> tuple[tuple[float, float], float]:  # copy/pickle rebuild it with its radius
        return tuple(self), self.radius_km  # type: ignore[return-value]


class ApifyIdealistaPortalSource(_PortalSource):
    """Idealista through ``igolaizola/idealista-scraper`` (``APIFY_IDEALISTA_PROVIDER=igolaizola``), any city or
    province by name; rows follow the output schema the Store infers from its recent runs (``url``, ``operation``,
    ``propertyType``, ``municipality``, ``province``, ``price``, ``size``, ``rooms``, ``latitude``/``longitude``,
    ``contactInfo.{phone1,commercialName}``, ``suggestedTexts.title``). Checked against a live run (3 plots in
    Madrid, 2026-10-16): ``size`` is the plot in m2 for ``propertyType=land``, the rows carry no ``description`` and
    the price is also in ``priceInfo.price.{amount,currencySuffix}``.

    ``minSize`` stops at 300 m2 (``_IDEALISTA_SIZES``): a larger ``min_area`` is sent as 300 and the ``maxItems``
    window of the newest rows fills with smaller plots. With ``sort_biggest`` (``IDEALISTA_SORT_BIGGEST``) such a
    task without a ``max_area`` is sorted ``biggest`` instead of ``mostRecent``; the rows are still cut by ``min_area`` after the run
    (the analysis prefilter and ``bot/campaign/tolerance.py``).

    A province task (SEARCH_PLACE_LEVEL_FROM_SPEC) sends the same input as a city one: the actor's schema has no place
    type, only ``location`` by name, and what the actor makes of a province name is not checked. Only the row filter
    differs: it compares the row's ``province`` instead of its ``municipality``.

    ``unambiguous_place`` (``IDEALISTA_UNAMBIGUOUS_PLACE``, Q17): a city task whose centre the cached geocoder knows is
    sent with ``location`` and also ``latitude``/``longitude`` + ``distanceKm`` from the municipality's size (the actor ranks coordinates above
    the name; "Valencia" by name ran as València d'Àneu, Lleida). Rows are still kept for the city's municipality only.
    An output whose every row is from another municipality/province (and, for a city sent with its circle, lies
    farther than twice the radius, at most 40 km) ends the run as ``apify_wrong_place``."""

    name = "apify_idealista"
    hosts = frozenset({"idealista.com"})
    kinds: ClassVar[dict[str, str]] = {"land": "lands", "house": "homes", "apartment": "homes",
                                       "commercial": "premises"}
    _TYPES: ClassVar[dict[str, str]] = {"land": "landparcel", "commercial": "commercial"}

    # IDEALISTA_UNAMBIGUOUS_PLACE: the circle around a city's centre is half the diagonal of the municipality's
    # Nominatim ``boundingbox`` (Burjassot ~2x2 km -> 3 km; Valencia with El Palmar and El Saler, Madrid with El Pardo
    # and Barajas -> 15-25 km), within 3-30 km; 15 km without a box (an old cache entry). A row up to twice the circle
    # (at most 40 km) from the centre is a neighbour, never «another place».
    _CITY_RADIUS_KM = 15
    _CITY_RADIUS_RANGE = (3, 30)
    _NEIGHBOUR_KM = 40

    def __init__(self, token: str, *, actor_id: str = "igolaizola/idealista-scraper", sort_biggest: bool = False,
                 unambiguous_place: bool = False, **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)
        self.sort_biggest = sort_biggest
        self.unambiguous_place = unambiguous_place  # IDEALISTA_UNAMBIGUOUS_PLACE (Q17)

    def _sort(self, min_area: int | None, max_area: int | None) -> str:
        """``biggest`` when the flag is on, ``min_area`` is above the actor's largest ``minSize`` and there is no
        ``max_area``: with one, the biggest rows would all be above it and dropped by ``tolerance`` (a paid empty run)."""
        if self.sort_biggest and min_area is not None and min_area > _IDEALISTA_SIZES[-1] and max_area is None:
            return "biggest"
        return "mostRecent"

    async def _plan(self, task: QueryTask, amount: int, run_id: str | None
                    ) -> tuple[str, dict[str, Any], tuple[float, float] | None]:
        constraints = task.constraints
        min_area = _whole(constraints.get("min_area"))
        actor_input: dict[str, Any] = {"maxItems": amount, "operation": self._deal(task),
                                       "propertyType": self.kinds[task_kind(task)], "country": "es",
                                       "sortBy": self._sort(min_area, _whole(constraints.get("max_area")))}
        center = None
        if task.radius_km is not None:
            center = await self._center(task) if not run_id else None
            if center is not None:
                actor_input |= {"latitude": round(center[0], 6), "longitude": round(center[1], 6),
                                "distanceKm": max(1, min(100, round(task.radius_km)))}
        else:
            # Q17: a bare name may be another town for the actor ("Valencia" ran as València d'Àneu, Lleida), and
            # which names are shared is not known here (the geocoder returns one centre). So a city whose centre is
            # known goes as its name AND its centre + a circle of the municipality's size: the actor ranks the
            # coordinates above ``location``. The rows are still kept only for the city's municipality (``_place_ok``). No centre: the
            # name alone, as before. A resumed run sends nothing; its centre is looked up again for ``_wrong_place``.
            actor_input["location"] = task.location.strip()[:120]
            if self.unambiguous_place and task.place_level == "city":
                center = await self._city_circle(task)
            if center is not None and not run_id:
                actor_input |= {"latitude": round(center[0], 6), "longitude": round(center[1], 6),
                                "distanceKm": max(1, min(100, round(center.radius_km)))}
        filters = {"minPrice": _floor(_whole(constraints.get("min_price")), _IDEALISTA_PRICES),
                   "maxPrice": _ceil(_whole(constraints.get("max_price")), _IDEALISTA_PRICES),
                   "minSize": _floor(min_area, _IDEALISTA_SIZES)}
        actor_input |= {key: value for key, value in filters.items() if value}
        # PORTAL_RENTAL_TERM (Q20): a rent «на год» asks the actor for long-term lets only (input ``rentalTypes``,
        # enum ``longTerm``/``seasonal``). A task without a term, or a short one, sends no ``rentalTypes``.
        if self.rental_term and self._deal(task) == "rent" and task.rental_term == "long":
            actor_input["rentalTypes"] = ["longTerm"]
        return self.actor_id, actor_input, center

    async def _city_circle(self, task: QueryTask) -> _CityCircle | None:
        """The city's centre and circle (``_CITY_RADIUS_KM`` notes); None without a geocoder, an unknown place or a
        geocoder failure (the name only is then sent)."""
        try:
            found = await self._geocoder.resolve_area(task.location) if self._geocoder is not None else None
            center, box = found if found is not None else (None, None)
            point = _point(*center) if center is not None else None
        except Exception:  # noqa: BLE001 - a geocoder failure never stops the run: the name alone, as before
            return None
        if point is None:
            return None
        radius: float = self._CITY_RADIUS_KM
        if box is not None:
            south, north, west, east = box
            low, high = self._CITY_RADIUS_RANGE
            radius = max(low, min(high, _distance_km((south, west), (north, east)) / 2))
        return _CityCircle(point, radius)

    def _wrong_place(self, item: Any, task: QueryTask, center: tuple[float, float] | None) -> bool:
        """A row of another place. For a city sent with its circle a row whose point is near the circle (twice its
        radius, at most 40 km: the portal's points are approximate) is the circle's neighbour, not another place: it
        stays invalid. A resumed city run whose centre the geocoder did not give now marks nothing (its input is
        unknown)."""
        if not self.unambiguous_place or not isinstance(item, dict):
            return False
        city = task.radius_km is None and task.place_level == "city"
        execution = _execution.get()
        if city and center is None and execution is not None and execution.run_id:
            return False
        point = _point(item.get("latitude"), item.get("longitude"))
        if city and center is not None and point is not None:
            radius = getattr(center, "radius_km", self._CITY_RADIUS_KM)
            if _distance_km(center, point) <= min(2 * radius, self._NEIGHBOUR_KM):
                return False
        return self._place_wrong(task, center, point, _text(item.get("municipality")), _text(item.get("province")))

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
            property_type=self._TYPES.get(task_kind(task)), row_type=_row_type(item.get("propertyType")),
            row_deal=_row_deal(item.get("operation")), rental_term=_idealista_rental_term(item.get("labels")))


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
            property_type=self._TYPES.get(task_kind(task)),
            row_type=_row_type(row.get("propertyType"), land=task_kind(task) == "land"),
            row_deal=_row_deal(row.get("transactionType")))


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

    def __init__(self, token: str, *, actor_id: str = "trev0n/habitaclia-com-spain-scraper", agency_ref: bool = False,
                 **kwargs: Any) -> None:
        super().__init__(token, actor_id=actor_id, **kwargs)
        self.agency_ref = agency_ref  # DEDUP_DISTRICT_NOT_STREET (Q15)

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
            advertiser=row.get("advertiserName"), property_type=self._TYPES.get(task_kind(task)),
            row_type=_row_type(row.get("propertyType"), row.get("propertySubtype"), land=task_kind(task) == "land"),
            row_deal=_row_deal(row.get("transactionType")),
            reference=row.get("agencyRef"))


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
            phone=None, advertiser=None, property_type=self._TYPES.get(task_kind(task)),
            row_type=_MILANUNCIOS_ROW.get(category, (None, None))[0],
            row_deal=_MILANUNCIOS_ROW.get(category, (None, None))[1])
