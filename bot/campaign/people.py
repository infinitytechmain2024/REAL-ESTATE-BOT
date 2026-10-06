"""Investor mode: the people a reach search found -- spec-driven queries, enrichment, scoring, one card per person.

Pure helpers (no database, no network except the injected page fetcher):

* ``investor_of`` / ``spec_queries`` -- the ``TaskSpec.investor`` block (who, ticket, asset class, geography,
  languages, role) turned into search queries and into the model's hard criteria;
* ``enrichable_url`` / ``enrich`` -- the one public page a relevant result is opened on (sites and public Telegram
  channels only: LinkedIn, Instagram, TikTok, X, Reddit, YouTube are never opened) and what is read from it:
  e-mails, phones, website, company, a description, the last visible activity;
* ``score`` -- 0..100 against the spec, with the reasons in Russian;
* ``person_key`` / ``build_cards`` -- the same person on several platforms is one card; cards come in groups.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

DESCRIPTION_CHARS = 600
FETCH_TIMEOUT_SECONDS = 30.0
SPEC_QUERY_LIMIT = 8

# --- the investor block of the spec ----------------------------------------------------------------------


def investor_of(spec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The spec's ``investor`` block as a clean dict, or None when there is none (or nothing is filled in)."""
    raw = spec.get("investor") if isinstance(spec, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    from .spec import Investor

    try:
        block = Investor.model_validate(dict(raw)).model_dump()
    except Exception:  # noqa: BLE001 - a malformed block is no block
        return None
    ticket = block.get("ticket") or {}
    filled = any(block.get(k) for k in ("who", "asset_class", "geography", "languages", "yield_min", "user_role")) \
        or ticket.get("min") is not None or ticket.get("max") is not None
    return block if filled else None


def investor_brief(investor: Mapping[str, Any]) -> dict[str, Any]:
    """The compact version of the block the model reads (empty fields left out)."""
    ticket = investor.get("ticket") or {}
    brief: dict[str, Any] = {k: investor[k] for k in ("who", "asset_class", "geography", "languages", "yield_min",
                                                     "user_role") if investor.get(k)}
    sums = {k: ticket[k] for k in ("min", "max", "currency") if ticket.get(k) is not None}
    if sums:
        brief["ticket"] = sums
    return brief


# who (spec) -> the kinds a reach result can have that satisfy it
WHO_KINDS: dict[str, frozenset[str]] = {
    "private": frozenset({"investor"}),
    "fund": frozenset({"fund"}),
    "family_office": frozenset({"fund"}),
    "developer": frozenset({"developer"}),
    "agency": frozenset({"agency", "agent"}),
    "network": frozenset({"network"}),
}
# who -> a search phrase per language
WHO_TERMS: dict[str, dict[str, str]] = {
    "private": {"en": "angel investor real estate", "es": "inversor inmobiliario privado",
                "ru": "частный инвестор недвижимость", "uk": "приватний інвестор нерухомість"},
    "fund": {"en": "real estate investment fund", "es": "fondo de inversión inmobiliaria",
             "ru": "инвестиционный фонд недвижимость", "uk": "інвестиційний фонд нерухомість"},
    "family_office": {"en": "family office real estate", "es": "family office inversión inmobiliaria",
                      "ru": "семейный офис инвестиции в недвижимость", "uk": "сімейний офіс інвестиції"},
    "developer": {"en": "real estate developer", "es": "promotora inmobiliaria",
                  "ru": "девелопер застройщик", "uk": "забудовник девелопер"},
    "agency": {"en": "real estate agency", "es": "agencia inmobiliaria",
               "ru": "агентство недвижимости", "uk": "агентство нерухомості"},
    "network": {"en": "real estate investor club", "es": "club de inversores inmobiliarios",
                "ru": "клуб инвесторов недвижимость", "uk": "клуб інвесторів нерухомість"},
}
_LANGUAGE_NAMES = {"русский": "ru", "russian": "ru", "английский": "en", "english": "en", "испанский": "es",
                   "spanish": "es", "español": "es", "украинский": "uk", "ukrainian": "uk", "українська": "uk"}


def _language_codes(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        text = name.strip().casefold()
        code = _LANGUAGE_NAMES.get(text) or (text[:2] if text[:2] in ("ru", "en", "es", "uk") else None)
        if code and code not in out:
            out.append(code)
    return out


def spec_queries(investor: Mapping[str, Any], *, city_for: Mapping[str, str] | Any, languages: Sequence[str],
                 spanish_ok: bool) -> list[tuple[str, str, str]]:
    """``(platform, language, text)`` queries built from who / asset class / languages, most specific first.

    ``city_for(language)`` is the place's name in that language. Languages: the spec's, else the campaign's;
    English is always allowed; Spanish only for Spain.
    """
    wanted = _language_codes(investor.get("languages") or []) or list(languages)
    langs = [lang for lang in dict.fromkeys([*wanted, "en"])
             if lang in WHO_TERMS["private"] and (lang in languages or lang == "en") and (lang != "es" or spanish_ok)]
    asset = next(iter(investor.get("asset_class") or []), "")
    out: list[tuple[str, str, str]] = []
    for lang in langs:
        city = city_for(lang) if callable(city_for) else city_for.get(lang, "")
        for who in (investor.get("who") or [""])[:3]:
            term = WHO_TERMS.get(who, {}).get(lang) or (who if who else WHO_TERMS["private"][lang])
            text = " ".join(part for part in (term, asset, city) if part)
            out.append(("web", lang, text))
            if lang in ("en", "es"):
                out.append(("linkedin", lang, f'site:linkedin.com/in "{term}" {city}'.strip()))
            else:
                out.append(("telegram", lang, f"site:t.me {text}"))
    seen: set[str] = set()
    unique = [q for q in out if not (q[2].casefold() in seen or seen.add(q[2].casefold()))]
    return unique[:SPEC_QUERY_LIMIT]


# --- enrichment -------------------------------------------------------------------------------------------

_NEVER_OPENED = frozenset({"linkedin", "instagram", "tiktok", "x", "reddit", "youtube"})
_EMAIL = re.compile(r"[\w.+-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+", re.I)
_NOT_EMAIL_TLD = ("png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "ico", "woff", "woff2")
_NOT_EMAIL_HOST = ("example.com", "sentry.io", "wixpress.com", "domain.com", "email.com")
_TEL = re.compile(r"""href=["']tel:([+\d\s().-]{7,25})""", re.I)
_PHONE = re.compile(r"(?<![\w+])\+?\d[\d \-().]{7,17}\d(?!\w)")
_DATE_ATTR = re.compile(r"""(?:datetime|dateModified|datePublished|article:modified_time|article:published_time)"""
                        r"""["']?\s*[:=]\s*["'](\d{4}-\d{2}-\d{2})""", re.I)
_DATE_TAG = re.compile(r"""<time[^>]*datetime=["'](\d{4}-\d{2}-\d{2})""", re.I)
_TITLE_SEP = re.compile(r"\s[|–—·-]\s")


@dataclass(frozen=True, slots=True)
class Enrichment:
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    website: str | None = None
    company: str | None = None
    description: str = ""
    last_activity: date | None = None

    def contacts(self) -> dict[str, Any]:
        """The ``reach_contacts.contacts`` JSON: only what was found."""
        data: dict[str, Any] = {}
        if self.emails:
            data["emails"] = list(self.emails)
        if self.phones:
            data["phones"] = list(self.phones)
        if self.website:
            data["website"] = self.website
        if self.company:
            data["company"] = self.company
        if self.last_activity:
            data["last_activity"] = self.last_activity.isoformat()
        return data


def enrichable_url(platform: str, url: str) -> str | None:
    """The public page to open for a result, or None when it must not be opened.

    Sites: the page itself (never a blocked host). Telegram: the channel's public preview ``t.me/s/<name>``.
    LinkedIn, Instagram, TikTok, X, Reddit, YouTube (and any blocked host): never.
    """
    from bot.web_search.urls import fetchable, host_of, is_blocked

    if platform in _NEVER_OPENED:
        return None
    if platform == "telegram":
        parts = urlsplit(url)
        seg = [s for s in parts.path.split("/") if s]
        if host_of(url) not in ("t.me", "telegram.me") or not seg:
            return None
        name = seg[1] if seg[0] == "s" and len(seg) > 1 else seg[0]
        return f"https://t.me/s/{name}" if re.fullmatch(r"[A-Za-z0-9_]{3,64}", name) else None
    host = host_of(url)
    if platform != "web" or not host or is_blocked(host) or not fetchable(url):
        return None
    return url


def _valid_email(value: str) -> bool:
    host = value.rsplit("@", 1)[-1].casefold()
    return not (host.endswith(tuple("." + t for t in _NOT_EMAIL_TLD)) or host in _NOT_EMAIL_HOST)


def _phone_ok(value: str) -> bool:
    return 9 <= len(re.sub(r"\D", "", value)) <= 15


def extract(html: str, url: str, platform: str) -> Enrichment:
    """What the public page tells: contacts, website, company, description, last activity. Pure."""
    from bot.web_search.extract import parse_html
    from bot.web_search.urls import fetchable, host_of, is_blocked

    raw = html_lib.unescape(html)
    page = parse_html(html, url)
    emails = list(dict.fromkeys(m.casefold() for m in _EMAIL.findall(raw) if _valid_email(m)))[:5]
    phones: list[str] = []
    for value in [*_TEL.findall(raw), *_PHONE.findall(page.text)]:
        value = " ".join(value.split())
        if _phone_ok(value) and value not in phones:
            phones.append(value)
    host = host_of(url)
    website: str | None = None
    if platform == "web" and host:
        website = f"{urlsplit(url).scheme}://{host}"
    else:
        website = next((link.url for link in page.links
                        if host_of(link.url) not in ("t.me", "telegram.me", "telegram.org") and fetchable(link.url)
                        and not is_blocked(host_of(link.url))), None)
    title = page.title.strip()
    parts = [p.strip() for p in _TITLE_SEP.split(title) if p.strip()]
    company = (parts[-1] if len(parts) > 1 else (parts[0] if parts else "")) or None
    description = " ".join((page.text or page.description).split())[:DESCRIPTION_CHARS]
    today = datetime.now(UTC).date()
    dates: list[date] = []
    for value in [*_DATE_ATTR.findall(raw), *_DATE_TAG.findall(raw)]:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            continue
        if date(2000, 1, 1) <= parsed <= today:
            dates.append(parsed)
    return Enrichment(tuple(emails), tuple(phones[:3]), website, company[:120] if company else None, description,
                      max(dates) if dates else None)


class PageFetcherLike(Protocol):
    async def fetch(self, url: str, *, country: str | None = None) -> Any: ...


async def enrich(fetcher: PageFetcherLike, platform: str, url: str, *, country: str | None = None,
                 ) -> Enrichment | None:
    """Open the result's public page once (robots.txt respected) and read it; None when it is not to be opened
    or could not be read. Never raises."""
    target = enrichable_url(platform, url)
    if target is None:
        return None
    try:
        allowed = getattr(fetcher, "allowed", None)
        if allowed is not None and not await allowed(target):
            return None
        page = await asyncio.wait_for(fetcher.fetch(target, country=country), FETCH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a page that cannot be read costs the enrichment only
        log.info("campaign.reach.enrich_failed %s", getattr(exc, "code", type(exc).__name__))
        return None
    return extract(page.html, page.url, platform)


# --- scoring ----------------------------------------------------------------------------------------------

POINTS = {"kind": 40, "geography": 20, "asset": 15, "ticket": 15, "contact": 10, "activity": 5}
ASSET_WORDS: dict[str, tuple[str, ...]] = {
    "residential": ("residential", "vivienda", "жил", "apartment", "piso", "квартир"),
    "commercial": ("commercial", "comercial", "коммерч", "офис", "office", "retail", "local comercial"),
    "land": ("land", "suelo", "terreno", "земл", "участ"),
    "hotel": ("hotel", "hospitality", "отел", "гостиниц"),
    "villa": ("villa", "вилл"),
    "new_build": ("new build", "obra nueva", "новостро"),
}
_NUMBER_WORDS = {"k": 1e3, "тыс": 1e3, "thousand": 1e3, "mil": 1e3, "m": 1e6, "mm": 1e6, "mln": 1e6, "млн": 1e6,
                 "million": 1e6, "millones": 1e6}
_AMOUNT = re.compile(
    r"(?P<pre>[€$£]\s?)?(?P<num>\d{1,3}(?:[ ,.]\d{3})+|\d+(?:[.,]\d+)?)\s?"
    r"(?P<suf>millones|million|thousand|mln|млн|тыс|mil|mm|k|m)?(?![^\W\d_])"
    r"(?P<post>\s?(?:[€$£]|eur\b|usd\b|euros?\b|евро\b|долл))?", re.I)


def amounts_in(text: str) -> list[float]:
    """Sums written in the text (``€500k``, ``2 млн``, ``1 000 000 EUR``); a bare number is not a sum."""
    found: list[float] = []
    for m in _AMOUNT.finditer(text):
        if not (m.group("pre") or m.group("suf") or m.group("post")):
            continue
        num = m.group("num")
        if re.fullmatch(r"\d{1,3}(?:[ ,.]\d{3})+", num):
            value = float(re.sub(r"[ ,.]", "", num))
        else:
            value = float(num.replace(",", "."))
        value *= _NUMBER_WORDS.get((m.group("suf") or "").casefold(), 1.0)
        if value >= 1000:
            found.append(value)
    return found


def _text_of(contact: Any) -> str:
    info = getattr(contact, "contacts", None) or {}
    return " ".join(str(part) for part in (getattr(contact, "name", None), getattr(contact, "title", None),
                                           getattr(contact, "snippet", None), getattr(contact, "summary_ru", None),
                                           getattr(contact, "profile_text", None), info.get("company"),
                                           info.get("website")) if part)


def kind_matches(kind: str, who: Sequence[str]) -> bool:
    if not who or kind == "company":  # no wish stated, or the judge already matched the kind the task asks for
        return True
    return any(kind in WHO_KINDS.get(w, frozenset()) for w in who)


def score(contact: Any, spec_investor: Mapping[str, Any] | None, *, places: Sequence[str] = (),
          now: datetime | None = None) -> tuple[int, list[str]]:
    """How well a found contact fits the task, 0..100, with the reasons in Russian.

    kind matches ``who`` +40, the place (``geography`` and ``places``) is named +20, an asset class word +15,
    a sum within the ticket range +15, a contact (e-mail / phone) +10, activity within a year +5.
    Without a spec only the place, contact and activity count (the rest is not asked).
    """
    from . import geo
    from .leads import contacts_in

    spec = spec_investor or {}
    text = _text_of(contact)
    points = 0
    reasons: list[str] = []
    who = list(spec.get("who") or [])
    if spec and kind_matches(str(getattr(contact, "kind", "")), who):
        points += POINTS["kind"]
        reasons.append("тип совпадает с запросом")
    names = [*places, *(spec.get("geography") or [])]
    place = next((p for p in names if p and geo.mentions_place(text, geo.place_names(str(p)))), None)
    if place:
        points += POINTS["geography"]
        reasons.append(f"упомянут {place}")
    assets = [a for a in spec.get("asset_class") or [] if a]
    asset = next((a for a in assets if a.casefold() in text.casefold()
                  or any(w in text.casefold() for w in ASSET_WORDS.get(a.casefold(), ()))), None)
    if asset:
        points += POINTS["asset"]
        reasons.append(f"класс актива: {asset}")
    ticket = spec.get("ticket") or {}
    low, high = ticket.get("min"), ticket.get("max")
    if (low is not None or high is not None) and any(
            (low is None or v >= low) and (high is None or v <= high) for v in amounts_in(text)):
        points += POINTS["ticket"]
        reasons.append("тикет в вашем диапазоне")
    info = getattr(contact, "contacts", None) or {}
    if info.get("emails") or info.get("phones") or contacts_in(str(getattr(contact, "snippet", "") or "")):
        points += POINTS["contact"]
        reasons.append("есть контакт")
    seen = info.get("last_activity")
    try:
        last = date.fromisoformat(str(seen)) if seen else None
    except ValueError:
        last = None
    today = (now or datetime.now(UTC)).date()
    if last and 0 <= (today - last).days < 365:
        points += POINTS["activity"]
        reasons.append("активен в последний год")
    return min(100, points), reasons


# --- one card per person ------------------------------------------------------------------------------------

_LEGAL = frozenset({"sl", "slu", "sa", "ltd", "llc", "inc", "gmbh", "ooo", "ооо", "тов", "co", "corp", "the"})


def _norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", (value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    words = [w for w in re.sub(r"[^\w]+", " ", text).split() if w not in _LEGAL]
    return " ".join(words)


def person_key(name: str | None, company: str | None, city: str | None) -> str:
    """The identity of a person across platforms: normalised name, company and city (empty: no safe identity).

    A one-word name without a company is no identity (every «Ana» would merge).
    """
    n, c = _norm(name), _norm(company)
    if not n or (len(n.split()) < 2 and not c):
        return ""
    return f"{n}|{c}|{_norm(city)}"


GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("investors", "🏦 Инвесторы и фонды", frozenset({"investor", "fund", "seeking"})),
    ("developers", "🏗 Девелоперы", frozenset({"developer"})),
    ("agents", "🤝 Агенты и сети", frozenset({"agent", "agency", "network"})),
    ("companies", "🏢 Компании", frozenset({"company", "other"})),
)
GROUP_HEADERS = {gid: header for gid, header, _ in GROUPS}


def group_of(kind: str) -> str:
    return next((gid for gid, _, kinds in GROUPS if kind in kinds), "companies")


@dataclass(frozen=True, slots=True)
class Card:
    """One person to send: the best contact, where else they are, and why they fit."""

    contact: Any
    score: int
    reasons: tuple[str, ...]
    links: tuple[tuple[str, str], ...] = ()  # (platform, url) of the merged contacts
    keys: tuple[str, ...] = field(default=())  # delivery keys of every merged contact
    group: str = "companies"


def build_cards(contacts: Sequence[Any], spec_investor: Mapping[str, Any] | None, *, places: Sequence[str] = (),
                now: datetime | None = None) -> list[Card]:
    """Score every contact, merge those that are the same person, and order: group, then score (high first)."""
    city = places[0] if places else ""
    scored: list[tuple[Any, int, list[str]]] = []
    for contact in contacts:
        points, reasons = score(contact, spec_investor, places=places, now=now)
        scored.append((contact, points, reasons))
    merged: dict[str, list[tuple[Any, int, list[str]]]] = {}
    for index, item in enumerate(scored):
        info = getattr(item[0], "contacts", None) or {}
        key = person_key(getattr(item[0], "name", None), info.get("company"), city) or f"#{index}"
        merged.setdefault(key, []).append(item)
    cards: list[Card] = []
    for items in merged.values():
        items.sort(key=lambda it: -it[1])
        best, points, reasons = items[0]
        links = tuple((getattr(c, "platform", ""), getattr(c, "url", "")) for c, _, _ in items[1:])
        cards.append(Card(best, points, tuple(reasons), links, tuple(c.delivery_key for c, _, _ in items),
                          group_of(str(getattr(best, "kind", "")))))
    order = {gid: n for n, (gid, _, _) in enumerate(GROUPS)}
    cards.sort(key=lambda c: (order[c.group], -c.score, str(getattr(c.contact, "name", "") or ""), c.keys[0]))
    return cards
