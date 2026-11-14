"""Investor-vertical findings: one organisation on several hosts (Q60, ``INVESTOR_LEAD_SAME_ORG``).

``org_key``: the analysis fact ``who`` (the person, company, fund or club named by the page), normalised: accents,
case, punctuation, a parenthesised abbreviation («(PDN)») and legal suffixes («S.L.», «Ltd») dropped. ``None`` when
nothing is named or only a generic word («Private investor», «inversor particular»): never merged then.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


_LEGAL = frozenset({"s", "l", "sl", "slu", "sa", "sau", "sll", "ltd", "llc", "inc", "plc", "gmbh", "bv", "co", "the"})
# compared after ``_plain`` (accents and «й» folded like the name)
_GENERIC = frozenset(map(_plain, {
    "investor", "investors", "inversor", "inversores", "inversora", "inversionista", "inversionistas", "private",
    "privado", "privada", "particular", "particulares", "owner", "propietario", "propietaria", "agency", "agencia",
    "fund", "fondo", "company", "empresa", "developer", "promotor", "promotora", "user", "usuario", "anonymous",
    "anonimo", "unknown", "desconocido", "seller", "vendedor", "buyer", "comprador", "group", "grupo", "a", "an",
    "de", "del", "la", "el", "los", "las", "y", "and", "of", "инвестор", "частный", "частные", "инвесторы", "фонд",
    "компания", "застройщик", "агентство", "владелец", "собственник",
}))


def org_key(payload: dict[str, Any] | None) -> str | None:
    """The normalised name of the person, company or club of an investor finding (``who``); None when not named."""
    who = (payload or {}).get("who")
    if not isinstance(who, str):
        return None
    text = re.sub(r"\([^)]*\)", " ", _plain(who))
    words = [w for w in re.split(r"[^\w]+", text) if w and w not in _LEGAL]
    if not words or all(w in _GENERIC for w in words) or len("".join(words)) < 3:
        return None
    return " ".join(words)


def same_org(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    key = org_key(a)
    return key is not None and key == org_key(b)
