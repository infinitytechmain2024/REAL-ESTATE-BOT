"""Location-first query planning for Facebook.

Facebook's group search is noisy when it receives the user's full sentence.
The group query therefore contains only the target place and a broad local
property phrase. Posts are collected from the recent feed and then judged by
the normal criterion-aware ranker.
"""

from __future__ import annotations

import re
import unicodedata

from bot.models.enums import Mode
from bot.models.query import ParsedQuery

_COUNTRY_LANGUAGE = {
    "spain": ("es", ("terreno", "parcela", "solar", "finca", "urbanizable")),
    "españa": ("es", ("terreno", "parcela", "solar", "finca", "urbanizable")),
    "cyprus": ("en", ("land", "plot", "parcel", "building plot")),
    "greece": ("el", ("οικόπεδο", "αγροτεμάχιο", "γή")),
    "portugal": ("pt", ("terreno", "lote", "quinta")),
    "italy": ("it", ("terreno", "lotto", "edificabile")),
}


def target_language(query: ParsedQuery) -> str:
    location = " ".join(
        part for part in (query.location.country, query.location.region, query.location.city) if part
    ).lower()
    for country, (language, _terms) in _COUNTRY_LANGUAGE.items():
        if country in location:
            return language
    return "en"


def group_query(query: ParsedQuery) -> str:
    """Build one broad, location-anchored group discovery query."""
    location = query.location.city or query.location.region or query.location.country or query.location.raw
    if query.mode is Mode.INVESTORS:
        phrase = {"es": "inmobiliaria", "el": "μεσιτικό γραφείο", "pt": "imobiliária"}.get(
            target_language(query), "real estate agency"
        )
    else:
        phrase = {"es": "terrenos parcelas", "el": "οικόπεδα", "pt": "terrenos lotes"}.get(
            target_language(query), "land property"
        )
    return " ".join(part for part in (phrase, location) if part).strip()


def post_terms(query: ParsedQuery, *, limit: int) -> list[str]:
    """Return short in-group terms; the full sentence is never sent to Facebook."""
    language = target_language(query)
    country = query.location.country or ""
    terms = list(_COUNTRY_LANGUAGE.get(country.lower(), (language, ("land", "plot")))[1])
    if query.area_min is not None:
        terms.append(str(int(query.area_min)))
    if query.buildable_required:
        terms.extend({"es": ["urbanizable", "edificable"], "el": ["οικοδομήσιμο"]}.get(language, ["buildable"]))
    if query.object_type:
        terms.append(query.object_type)
    # Diaspora groups may publish in the requester's language. Keep these as
    # secondary post terms only; they never influence group discovery.
    user_terms = {
        "uk": ("ділянка", "земля", "нерухомість"),
        "ru": ("участок", "земля", "недвижимость"),
    }
    diaspora_terms: list[str] = []
    for language in query.languages:
        diaspora_terms.extend(user_terms.get(language.lower(), ()))
    if diaspora_terms and any(language.lower() != target_language(query) for language in query.languages):
        # One local anchor plus diaspora wording gives both Spanish listings
        # and Ukrainian/Russian diaspora groups without polluting discovery.
        terms = terms[:1] + diaspora_terms + terms[1:]
    return list(dict.fromkeys(term for term in terms if term))[:limit]


def location_matches(title: str, query: ParsedQuery) -> bool:
    """Reject obviously unrelated groups while allowing generic city groups."""
    tokens = [query.location.city, query.location.region, query.location.country]
    tokens = [_normalise(token) for token in tokens if token]
    if not tokens:
        return True
    haystack = _normalise(title)
    return any(token and token in haystack for token in tokens)


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9а-яё]+", " ", value.lower()).strip()
