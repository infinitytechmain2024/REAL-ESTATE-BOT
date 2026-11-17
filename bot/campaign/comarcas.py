"""PLACE_COMARCA_MEMBERSHIP (Q74): the comarcas of the province of Alicante and their municipalities.

A static table, no network. Sim b7-g4-s8 («участок в провинции Аликанте, районы: Вега-Баха, Марина-Альта»): finds in
Aspe, Villena, Elche, Finestrat... named their town, Fotocasa even named the comarca (``level3Name``), yet the place
was «Не подтверждено: место» 31 times, offered as similar. With the table a town of another comarca is another place
and a town of a named comarca is the place (bot/campaign/tolerance.py).

The nine comarcas of the province (the Generalitat's delimitation, as Fotocasa's ``level3Name`` names them) with their
Spanish and Valencian names and the usual Russian spellings. The municipalities: their Spanish and Valencian names,
plus a few well-known localities inside one municipality (Orihuela Costa, Moraira, Ciudad Quesada). Sax is filed in
both Alt Vinalopó (the Generalitat's and Fotocasa's) and Vinalopó Mitjà (the older delimitation): either one named
counts, neither is another place. A name not in the table is unknown: nothing is guessed (rule 9).
"""

from __future__ import annotations

import re
import unicodedata

# comarca key -> its names (Spanish, Valencian, Russian), compared folded (``fold``)
COMARCA_NAMES: dict[str, tuple[str, ...]] = {
    "Vega Baja": ("Vega Baja", "Vega Baja del Segura", "Baix Segura", "Bajo Segura", "Baix Segura / Vega Baja",
                  "Вега-Баха", "Вега-Баха-дель-Сегура", "Байш-Сегура"),
    "Marina Alta": ("Marina Alta", "Марина-Альта"),
    "Marina Baixa": ("Marina Baixa", "Marina Baja", "Марина-Баха", "Марина-Байша"),
    "Alacantí": ("Alacantí", "Alicantí", "Campo de Alicante", "Алаканти"),
    "Alcoià": ("Alcoià", "Hoya de Alcoy", "Foia d'Alcoi", "Алькойя"),
    "Comtat": ("Comtat", "Condado de Cocentaina", "Комтат"),
    "Alt Vinalopó": ("Alt Vinalopó", "Alto Vinalopó", "Альт-Виналопо", "Альто-Виналопо"),
    "Vinalopó Mitjà": ("Vinalopó Mitjà", "Vinalopó Medio", "Medio Vinalopó", "Виналопо-Митжа", "Виналопо-Медио"),
    "Baix Vinalopó": ("Baix Vinalopó", "Bajo Vinalopó", "Байш-Виналопо", "Бахо-Виналопо"),
}

# comarca key -> its municipalities («Spanish / Valencian» when the two differ; each name is looked up on its own)
MUNICIPALITIES: dict[str, tuple[str, ...]] = {
    "Vega Baja": (
        "Albatera", "Algorfa", "Almoradí", "Benejúzar / Benejússer", "Benferri", "Benijófar", "Bigastro",
        "Callosa de Segura", "Catral", "Cox / Coix", "Daya Nueva / Daia Nova", "Daya Vieja / Daia Vella",
        "Dolores / Dolors", "Formentera del Segura / Formentera de Segura", "Granja de Rocamora",
        "Guardamar del Segura / Guardamar de Segura", "Jacarilla", "Los Montesinos / Els Montesinos",
        "Orihuela / Oriola", "Pilar de la Horadada / El Pilar de la Foradada", "Rafal", "Redován", "Rojales",
        "San Fulgencio / Sant Fulgenci", "San Isidro / Sant Isidre", "San Miguel de Salinas / Sant Miquel de Salines",
        "Torrevieja / Torrevella",
        # localities of one municipality: Orihuela, Rojales
        "Orihuela Costa", "Ciudad Quesada",
    ),
    "Marina Alta": (
        "Adsubia / L'Atzúbia", "Alcalalí", "Beniarbeig", "Benidoleig", "Benigembla", "Benimeli", "Benissa",
        "Benitachell / El Poble Nou de Benitatxell", "Calpe / Calp", "Castell de Castells", "Denia / Dénia",
        "Gata de Gorgos", "Jalón / Xaló", "Jávea / Xàbia", "Lliber / Llíber", "Murla", "Ondara", "Orba", "Parcent",
        "Pedreguer", "Pego", "Els Poblets", "Ráfol de Almunia / El Ràfol d'Almúnia", "Sagra",
        "Sanet y Negrals / Sanet i els Negrals", "Senija", "Teulada", "Tormos", "Vall de Alcalá / La Vall d'Alcalà",
        "Vall de Ebo / La Vall d'Ebo", "Vall de Gallinera / La Vall de Gallinera", "Vall de Laguar / La Vall de Laguar",
        "Vergel / El Verger",
        # a locality of Teulada
        "Moraira", "Teulada-Moraira",
    ),
    "Marina Baixa": (
        "Alfaz del Pi / L'Alfàs del Pi", "Altea", "Beniardá / Beniardà", "Benidorm", "Benifato", "Benimantell",
        "Bolulla", "Callosa de Ensarriá / Callosa d'en Sarrià", "Confrides", "Finestrat",
        "Guadalest / El Castell de Guadalest", "La Nucía / La Nucia", "Orcheta / Orxeta", "Polop", "Relleu", "Sella",
        "Tárbena / Tàrbena", "Villajoyosa / La Vila Joiosa",
    ),
    "Alacantí": (
        "Agost", "Aigües", "Alicante / Alacant", "Busot", "El Campello", "Jijona / Xixona", "Muchamiel / Mutxamel",
        "San Juan de Alicante / Sant Joan d'Alacant", "San Vicente del Raspeig / Sant Vicent del Raspeig",
    ),
    "Alcoià": ("Alcoy / Alcoi", "Bañeres / Banyeres de Mariola", "Benifallim", "Castalla", "Ibi", "Onil",
               "Penáguila / Penàguila", "Tibi"),
    "Comtat": (
        "Agres", "Alcocer de Planes / Alcosser", "Alcolecha / Alcoleja", "Alfafara", "Almudaina",
        "Alquería de Aznar / L'Alqueria d'Asnar", "Balones", "Benasau", "Beniarrés", "Benilloba", "Benillup",
        "Benimarfull", "Benimassot", "Cocentaina", "Facheca / Fageca", "Famorca", "Gayanes / Gaianes", "Gorga",
        "Lorcha / L'Orxa", "Millena", "Muro de Alcoy / Muro d'Alcoi", "Planes", "Cuatretondeta / Quatretondeta",
        "Tollos",
    ),
    "Alt Vinalopó": ("Beneixama / Benejama", "Biar", "Campo de Mirra / El Camp de Mirra", "Cañada / La Canyada",
                     "Salinas", "Sax / Saix", "Villena"),
    "Vinalopó Mitjà": (
        "Algueña / L'Alguenya", "Aspe", "Elda", "Hondón de las Nieves / El Fondó de les Neus",
        "Hondón de los Frailes / El Fondó dels Frares", "Monforte del Cid", "Monóvar / Monòver", "Novelda",
        "Petrer / Petrel", "Pinoso / El Pinós", "La Romana", "Sax / Saix",
    ),
    "Baix Vinalopó": ("Crevillente / Crevillent", "Elche / Elx", "Santa Pola"),
}

# Names of a province as well as of a municipality: alone they say nothing about the comarca («Finestrat, Alicante»).
_PROVINCE_NAMES = frozenset({"alicante", "alacant", "valencia", "castellon", "castello"})
# Words before a comarca's name that are not part of it («la Marina Alta», «comarca de la Vega Baja»).
_LEADING = re.compile(r"^(?:comarca\s+(?:de\s+(?:la\s+|el\s+|l\s+)?|del\s+)?|la\s+|el\s+|l\s+|les\s+|els\s+|комарка\s+)")
_SEPARATORS = re.compile(r"[,;()|\n]")


def fold(text: str) -> str:
    """Case, accents, hyphens and apostrophes folded: «L'Alfàs del Pi» -> «l alfas del pi», «Вега-Баха» -> «вега баха»."""
    plain = "".join(ch for ch in unicodedata.normalize("NFD", text.casefold()) if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[-'’`´.]", " ", plain).split())


def _comarca_key(name: str) -> str:
    return _LEADING.sub("", fold(name))


_COMARCA_BY_NAME: dict[str, str] = {_comarca_key(n): key for key, names in COMARCA_NAMES.items() for n in (key, *names)}
_COMARCAS_OF_TOWN: dict[str, frozenset[str]] = {}
for _key, _towns in MUNICIPALITIES.items():
    for _town in _towns:  # each name, and the official «Spanish / Valencian» one whole («Alicante / Alacant»)
        for _name in {*_town.split("/"), _town}:
            _folded = fold(_name)
            _COMARCAS_OF_TOWN[_folded] = _COMARCAS_OF_TOWN.get(_folded, frozenset()) | {_key}


def comarca_of(name: str) -> str | None:
    """The comarca a name means («Вега-Баха», «Baix Segura», «la Marina Alta» -> its key), None for any other name."""
    return _COMARCA_BY_NAME.get(_comarca_key(name)) if isinstance(name, str) and name.strip() else None


def comarcas_of_place(*texts: object) -> frozenset[str] | None:
    """The comarcas of the municipality the place texts name («Finestrat Pueblo, Finestrat, Alicante» -> Marina
    Baixa): every comma / slash / bracket part that is a whole municipality name of the table. Parts naming towns of
    different comarcas: None (unknown), as is a place naming none. A province's name alone is no town; the official
    «Alicante / Alacant» (Fotocasa's municipality) is the town."""
    found: set[frozenset[str]] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for part in _SEPARATORS.split(text):
            whole = fold(part)
            names = [whole] if whole in _COMARCAS_OF_TOWN else [fold(n) for n in part.split("/")]
            found.update(_COMARCAS_OF_TOWN[n] for n in names if n and n not in _PROVINCE_NAMES and n in _COMARCAS_OF_TOWN)
    if not found:
        return None
    common = frozenset.intersection(*found)
    return common or None


def task_area_known(location: str | None) -> bool:
    """The task's place is one the table covers: the province of Alicante, the Costa Blanca, the Valencian Community
    or a town of the table (a «Condado» of another province is not the Comtat)."""
    folded = fold(location or "")
    return folded in {"alicante", "alacant", "provincia de alicante", "costa blanca", "comunidad valenciana",
                      "comunitat valenciana"} or folded in _COMARCAS_OF_TOWN
