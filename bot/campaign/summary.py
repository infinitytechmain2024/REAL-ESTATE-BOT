"""The end-of-campaign summary «Итог поиска»: what each source gave, sent once when a campaign ends.

One line per source: Facebook (groups, posts read, cards), each known portal
(Idealista and Fotocasa always: links the search found, pages read, listings
taken from the search result because the site refused, cards), each social
network, the other sites together, and the known portals the search found
nothing on. Numbers come from ``RunStore.source_counts`` (posts, findings,
cards) and ``WebStore.site_report`` (the search funnel per site); either may
be empty. Russian, no ids, the same text for every requester.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bot.analysis_pipeline.cards import SOURCES

from .runs import SourceCount

MAX_SITE_LINES = 8  # sites with a line of their own besides the always-shown portals; the rest are summed
ALWAYS_SHOWN = ("idealista.com", "fotocasa.es")
PLATFORM_NAMES = {"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok", "linkedin": "LinkedIn",
                  "telegram": "Telegram"}


@dataclass(frozen=True, slots=True)
class Site:
    """One site's numbers from both stores (``SiteReport`` fields are read by name)."""

    host: str
    links: int = 0
    read: int = 0
    from_search: int = 0
    refused: int = 0
    queries: int = 0
    results: int = 0
    posts: int = 0
    sent: int = 0
    held: int = 0
    unverified: bool = False   # the site asked for a person's check and nobody passed it


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 ссылка», «3 ссылки», «5 ссылок»."""
    tail = n % 100
    word = many if 11 <= tail <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
    return f"{n} {word}"


def _cards(sent: int, held: int) -> str:
    text = f"→ {plural(sent, 'объявление', 'объявления', 'объявлений')}"
    return text + (f" (похожих: {held})" if held else "")


def site_name(host: str) -> str:
    for domain, name in SOURCES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def _portal_of(host: str, portals: Sequence[str]) -> str | None:
    return next((p for p in portals if host == p or host.endswith("." + p)), None)


def _sites(sources: Sequence[SourceCount], reports: Sequence[object], portals: Sequence[str]) -> dict[str, Site]:
    """Both stores' numbers per site; a portal's subdomains (inmuebles.sareb.es) count as the portal."""
    merged: dict[str, dict[str, int]] = {}  # the numbers per site (``unverified`` is a 0/1 flag)
    for report in reports:
        host = str(getattr(report, "host", ""))
        row = merged.setdefault(_portal_of(host, portals) or host, {})
        for name in ("links", "read", "from_search", "refused", "queries", "results"):
            row[name] = row.get(name, 0) + int(getattr(report, name, 0) or 0)
        row["unverified"] = row.get("unverified", 0) or int(bool(getattr(report, "unverified", False)))
    for source in sources:
        if source.platform != "website":
            continue
        row = merged.setdefault(_portal_of(source.name, portals) or source.name, {})
        for name, value in (("posts", source.posts), ("sent", source.sent), ("held", source.held)):
            row[name] = row.get(name, 0) + value
    return {host: Site(host, **{**row, "unverified": bool(row.get("unverified"))}) for host, row in merged.items()}


def site_stats(sources: Sequence[SourceCount], reports: Sequence[object], portals: Sequence[str] = ()) -> dict[str, Site]:
    """Both stores' numbers per site (a portal's subdomains count as the portal): ``links``, ``read``, ``refused``,
    ``sent``, ``held`` ... See ``Site``."""
    return _sites(sources, reports, portals)


def _site_line(site: Site) -> str:
    if site.unverified:  # human verification: the site asked for a person's check and nobody passed it
        done = f" · прочитано {site.read + site.from_search}" if site.read or site.from_search else ""
        cards = f" {_cards(site.sent, site.held)}" if site.sent or site.held else ""
        return f"{site_name(site.host)} — проверку никто не прошёл{done}{cards}"
    parts = [plural(site.links, "ссылка", "ссылки", "ссылок") + " в поиске"] if site.links else []
    if site.read:
        parts.append(f"прочитано {site.read}")
    if site.from_search:
        parts.append(f"по описанию из поиска {site.from_search} (сайт не пускает ботов)")
    refused = site.refused
    if refused and not site.read and not site.from_search:
        parts.append("сайт не дал прочитать страницы")
    if not parts and site.posts:
        parts.append(plural(site.posts, "страница", "страницы", "страниц"))
    return f"{site_name(site.host)} — {' · '.join(parts) or 'ничего'} {_cards(site.sent, site.held)}"


def _nothing_line(site: Site) -> str:
    """A portal with no links: why, from its ``site:`` queries."""
    if site.queries and not site.results:
        why = f"поиск ничего не вернул ({plural(site.queries, 'запрос', 'запроса', 'запросов')})"
    elif site.queries:
        why = "в поиске были только чужие или уже прочитанные ссылки"
    else:
        why = "до него не дошла очередь запросов"
    return f"{site_name(site.host)} — {why}"


def site_lines(sources: Sequence[SourceCount], reports: Sequence[object],
               portals: Sequence[str] = ()) -> tuple[list[str], list[Site]]:
    """One line per site (the known portals in priority order, then the busiest others, the rest summed) and the
    portals the search found nothing on. Shared by the owners' summary and the user's final report."""
    lines: list[str] = []
    sites = _sites(sources, reports, portals)
    shown: set[str] = set()
    nothing: list[Site] = []
    for portal in portals:  # in priority order: Idealista, Fotocasa, ...
        site = sites.get(portal, Site(portal))
        if site.links or site.posts:
            lines.append(_site_line(site))
            shown.add(portal)
        elif portal in ALWAYS_SHOWN:
            lines.append(_nothing_line(site))
            shown.add(portal)
        else:
            nothing.append(site)
    others = sorted((s for h, s in sites.items() if h not in shown and h not in portals and (s.links or s.posts)),
                    key=lambda s: (-s.sent, -s.held, -(s.read + s.from_search), s.host))
    for site in others[:MAX_SITE_LINES]:
        lines.append(_site_line(site))
    rest = others[MAX_SITE_LINES:]
    if rest:
        pages = sum(s.read + s.from_search for s in rest)
        lines.append(f"Другие сайты ({len(rest)}) — прочитано {pages} "
                     f"{_cards(sum(s.sent for s in rest), sum(s.held for s in rest))}")
    return lines, nothing


def summary_text(goal: str, sources: Sequence[SourceCount], reports: Sequence[object],
                 portals: Sequence[str] = ()) -> str:
    """The Russian summary message (see the module notes)."""
    lines = ["📊 Итог поиска", f"🎯 {goal}"]
    sent = sum(s.sent for s in sources)
    held = sum(s.held for s in sources)
    total = f"Отправлено объявлений: {sent}"
    if held:
        total += f" · ещё похожих вариантов: {held}"
    lines += [total, ""]

    for platform in ("facebook", "instagram", "tiktok", "linkedin", "telegram"):
        rows = [s for s in sources if s.platform == platform]
        if not rows:
            continue
        posts, cards, waiting = sum(r.posts for r in rows), sum(r.sent for r in rows), sum(r.held for r in rows)
        groups = sum(r.sources for r in rows)
        read = plural(posts, "пост", "поста", "постов")
        where = f"{plural(groups, 'группа', 'группы', 'групп')}, " if platform == "facebook" else ""
        lines.append(f"{PLATFORM_NAMES[platform]} — {where}{read} {_cards(cards, waiting)}")

    site_rows, nothing = site_lines(sources, reports, portals)
    lines += site_rows
    if nothing:
        lines += ["", "Не нашлось в поиске: " + ", ".join(site_name(s.host) for s in nothing)]
    if len(lines) == 4:
        lines.append("Источники ничего не дали.")
    return "\n".join(lines).strip()
