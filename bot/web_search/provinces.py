"""IDEALISTA_PROVINCE_LOCATION_ID (Q36): the provinces of Spain with their INE codes, for Idealista's Location ID.

``igolaizola/idealista-scraper`` takes a province name in ``location`` as a city: «Madrid» for the whole province
ran as the municipality of Madrid only. Its ``location`` also takes an Idealista Location ID; for a province that is
``0-EU-ES-<INE province code, 2 digits>``. Checked by a live run only for 28 (Madrid, 2026-11-14, run
Ieo05WJB7ARinWhOB: 10 rows from Navalcarnero, Collado Villalba, Valdelaguna, Chinchón, Bustarviejo ...). The other
codes are the INE table (01 Álava ... 52 Melilla) and the same ID pattern is assumed for them: check one or two other
provinces live before turning the flag on in production.

A community of one province (Comunidad de Madrid, Región de Murcia, Cantabria, La Rioja, Asturias, Navarra, Illes
Balears) is that province. A community of several provinces (Andalucía, Cataluña, Comunidad Valenciana ...) and any
other name are not listed: the caller keeps the name. Names are matched like ``city_centers.py`` (``_fold``:
case and accents do not matter).
"""

from __future__ import annotations

from bot.campaign.provinces import PROVINCES
from bot.web_search.geocoding import _fold

# INE province code -> the province's names: the shared table in bot/campaign/provinces.py (telegram reads it too).
_PROVINCES: dict[str, tuple[str, ...]] = PROVINCES

_BY_NAME: dict[str, str] = {_fold(name): code for code, names in _PROVINCES.items() for name in names}


def province_code(name: str) -> str | None:
    """The INE code (two digits) of the listed province or one-province community, or None for any other name."""
    return _BY_NAME.get(_fold(name)) if isinstance(name, str) and name.strip() else None


def province_location_id(name: str) -> str | None:
    """Idealista's Location ID of the province (``0-EU-ES-28`` for Madrid), or None for a name not in the list."""
    code = province_code(name)
    return f"0-EU-ES-{code}" if code is not None else None
