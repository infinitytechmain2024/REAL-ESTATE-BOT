"""The provinces of Spain with their INE codes: data shared by the campaign-runner and the telegram images.

``bot/web_search/provinces.py`` (Idealista's Location ID, Q36; the row's province by code, Q38) reads this table;
the intake (INTAKE_SKIP_WIDENS_PLACE, Q42) asks ``is_province`` whether a city's name is also its province's
(Madrid, Valencia, Málaga ...) or a one-province community (Comunidad de Madrid, Asturias ...). Marbella, Elche,
Bilbao are no province names. Names are matched with case and accents ignored.
"""

from __future__ import annotations

import unicodedata

# INE province code -> the province's names (Spanish, co-official, short forms).
PROVINCES: dict[str, tuple[str, ...]] = {
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



def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c)).strip()


_NAMES = frozenset(_fold(name) for names in PROVINCES.values() for name in names)


def is_province(name: str | None) -> bool:
    """True when ``name`` is a province of Spain (or a one-province community) by any of its listed names."""
    return isinstance(name, str) and bool(name.strip()) and _fold(name) in _NAMES
