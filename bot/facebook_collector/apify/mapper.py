"""One dataset item of a public-groups actor (default ``apify/facebook-groups-scraper``) -> ``AcquiredPost``.

The field names follow the actors' documentation, not a live run, so each fact is read from the few names the
public-group actors use (``url``/``postUrl``, ``text``/``postText``, ``time``/``date``, ``facebookUrl``/``groupUrl``).
Nothing is guessed: no price, currency or period is taken from the post's text (the analysis worker reads the text
and checks every number against a quote). A post whose text names several prices is marked ``needs_split``.

An item that is an actor's error report (``error``/``errorDescription``) is no post: ``group_error`` says which
group it was about, so the provider can tell «the group is closed or gone» from «the group had no new posts».
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from bot.acquisition.contracts import AcquiredPost, ListingFacts, Provenance

_PRICE = re.compile(r"\d[\d.\s]{2,}\s*(?:€|eur\b|euros?\b)|€\s*\d[\d.\s]{2,}", re.IGNORECASE)
_TEXT_KEYS = ("text", "postText", "message", "content")
_URL_KEYS = ("url", "postUrl", "permalink", "link")
_TIME_KEYS = ("time", "date", "timestamp", "publishedAt")
_GROUP_KEYS = ("facebookUrl", "groupUrl", "inputUrl", "groupLink")


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str | None:
    text = " ".join(str(value).split()) if isinstance(value, str | int | float) else ""
    return text or None


def _when(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def group_of(item: dict[str, Any]) -> str | None:
    return _text(_first(item, _GROUP_KEYS))


def group_error(item: Any) -> tuple[str | None, str] | None:
    """(group URL, short code) when the item is an actor's error report, else None."""
    if not isinstance(item, dict):
        return None
    error = _text(item.get("error") or item.get("errorDescription"))
    if error is None:
        return None
    folded = error.lower()
    code = ("group_private" if any(w in folded for w in ("private", "join", "login", "member")) else
            "group_not_found" if any(w in folded for w in ("not found", "unavailable", "removed", "404")) else
            "actor_error")
    return group_of(item), code


def _facts(item: dict[str, Any]) -> ListingFacts:
    """Only facts the actor states in fields of their own (Marketplace-style items); text is never parsed here."""
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    amount = price.get("amount") if price else item.get("priceAmount")
    currency = price.get("currency") if price else item.get("priceCurrency")
    return ListingFacts(
        price_amount=float(amount) if isinstance(amount, int | float) and not isinstance(amount, bool) else None,
        price_currency=str(currency).upper() if isinstance(currency, str) and currency.strip() else None,
        location_text=_text(item.get("location") if isinstance(item.get("location"), str) else None),
    )


def apify_item_to_post(item: dict[str, Any], *, campaign_id: str, actor_id: str, run_id: str, group_url: str,
                       collected_at: datetime | None = None) -> AcquiredPost:
    """The post of one dataset item (``ValueError``/``ValidationError`` for an item that is not a usable post)."""
    text, url = _text(_first(item, _TEXT_KEYS)), _text(_first(item, _URL_KEYS))
    if text is None or url is None:
        raise ValueError("apify item without text or url")
    quotes = {"needs_split": "true"} if len(set(_PRICE.findall(text))) >= 2 else {}
    return AcquiredPost(
        campaign_id=campaign_id,
        source=Provenance(provider="apify", actor_id=actor_id, run_id=run_id, group_url=group_url, post_url=url,
                          published_at=_when(_first(item, _TIME_KEYS)), collected_at=collected_at or datetime.now(UTC)),
        raw_text=text[:50_000], provider_facts=_facts(item), quotes=quotes)
