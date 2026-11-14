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

from bot.web_search.geocoding import _fold

# INE province code -> the province's names (Spanish, co-official, short forms).
_PROVINCES: dict[str, tuple[str, ...]] = {
    "01": ("Álava", "Araba", "Araba/Álava", "Álava/Araba"),
    "02": ("Albacete",),
    "03": ("Alicante", "Alacant", "Alicante/Alacant", "Alacant/Alicante"),
    "04": ("Almería",),
    "05": ("Ávila",),
    "06": ("Badajoz",),
    "07": ("Illes Balears", "Islas Baleares", "Baleares", "Balears", "Balearic Islands"),
    "08": ("Barcelona",),
    "09": ("Burgos",),
    "10": ("Cáceres",),
    "11": ("Cádiz",),
    "12": ("Castellón", "Castelló", "Castellón/Castelló", "Castelló/Castellón"),
    "13": ("Ciudad Real",),
    "14": ("Córdoba",),
    "15": ("A Coruña", "La Coruña", "Coruña"),
    "16": ("Cuenca",),
    "17": ("Girona", "Gerona"),
    "18": ("Granada",),
    "19": ("Guadalajara",),
    "20": ("Gipuzkoa", "Guipúzcoa"),
    "21": ("Huelva",),
    "22": ("Huesca",),
    "23": ("Jaén",),
    "24": ("León",),
    "25": ("Lleida", "Lérida"),
    "26": ("La Rioja", "Rioja"),
    "27": ("Lugo",),
    "28": ("Madrid", "Comunidad de Madrid", "Comunidad Autónoma de Madrid"),
    "29": ("Málaga",),
    "30": ("Murcia", "Región de Murcia"),
    "31": ("Navarra", "Comunidad Foral de Navarra", "Nafarroa", "Navarre"),
    "32": ("Ourense", "Orense"),
    "33": ("Asturias", "Principado de Asturias"),
    "34": ("Palencia",),
    "35": ("Las Palmas",),
    "36": ("Pontevedra",),
    "37": ("Salamanca",),
    "38": ("Santa Cruz de Tenerife",),
    "39": ("Cantabria",),
    "40": ("Segovia",),
    "41": ("Sevilla", "Seville"),
    "42": ("Soria",),
    "43": ("Tarragona",),
    "44": ("Teruel",),
    "45": ("Toledo",),
    "46": ("Valencia", "València", "Valencia/València"),
    "47": ("Valladolid",),
    "48": ("Bizkaia", "Vizcaya"),
    "49": ("Zamora",),
    "50": ("Zaragoza",),
    "51": ("Ceuta",),
    "52": ("Melilla",),
}

_BY_NAME: dict[str, str] = {_fold(name): code for code, names in _PROVINCES.items() for name in names}


def province_code(name: str) -> str | None:
    """The INE code (two digits) of the listed province or one-province community, or None for any other name."""
    return _BY_NAME.get(_fold(name)) if isinstance(name, str) and name.strip() else None


def province_location_id(name: str) -> str | None:
    """Idealista's Location ID of the province (``0-EU-ES-28`` for Madrid), or None for a name not in the list."""
    code = province_code(name)
    return f"0-EU-ES-{code}" if code is not None else None
