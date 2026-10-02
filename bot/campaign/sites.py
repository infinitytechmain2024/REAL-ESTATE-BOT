"""The person approves the search sites before the web stage reads them (migration 029).

The web stage first runs every pending search query of a round, so it knows all
the sites the task would read. Then, before reading a page of a site this person
has not decided on, it sends one numbered list -- each site with the query it
came from -- and asks «Все сайты подтверждены?». Pages are read only from
approved sites; a rejected site is never read for this task and never offered
again to this person (``search_sites``). A site approved earlier is read at once
and is searched first next time (``site:<host>`` queries); only new sites are
asked about. Facebook groups have their own stage and are not part of this.

The control plane (another process) records the answer: the buttons «✅ Все» and
«✏️ Убрать некоторые», or a text such as «да», «кроме 3 и 5», «убери olx»,
«только 1, 2». The two processes share nothing but PostgreSQL.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger(__name__)

CALLBACK_KIND = "sites"
ALL_BUTTON, EDIT_BUTTON = "✅ Все", "✏️ Убрать некоторые"
MAX_MESSAGE = 3800  # Telegram allows 4096; room for the header
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

QUESTION = "Все сайты подтверждены? Если нет, укажите, какие не подтверждены: номера или названия."
EDIT_HINT = ("Напишите номера или названия сайтов, которые убрать, например «3, 5» или «убери olx». "
             "Чтобы оставить только некоторые: «только 1, 2».")
REMINDER = "Жду подтверждения сайтов, чтобы начать поиск по ним. Ответьте «да» или укажите, какие убрать."

State = Literal["pending", "approved", "rejected"]


@dataclass(frozen=True, slots=True)
class Site:
    host: str
    state: State = "pending"
    number: int | None = None
    query: str | None = None
    language: str | None = None
    source: str = "asked"  # asked | saved (an earlier decision applied) | unasked (past the question cap)


@dataclass(frozen=True, slots=True)
class Answer:
    """``all``: approve every listed site; ``reject``: these hosts out, the rest in; ``only``: these in."""

    kind: Literal["all", "reject", "only"]
    hosts: frozenset[str] = frozenset()
    unknown: tuple[str, ...] = ()  # numbers or names that matched nothing


# --- the answer ----------------------------------------------------------------------------------------

_YES = frozenset({"да", "так", "все", "всё", "всі", "усі", "yes", "ok", "ок", "окей", "ага", "+", "👍",
                  "подтверждаю", "подтверждены", "все подтверждены", "всё подтверждено", "да все", "да, все",
                  "все ок", "всё ок", "все сайты", "так, всі", "всі сайти", "підтверджую", "approve", "all"})
_ONLY = re.compile(r"\b(только|тільки|лише|лишь|оставь|залиш|only|keep)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?(?![\w.])")
_WORD = re.compile(r"[a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9]", re.I)
_FILLER = frozenset({"кроме", "крім", "окрім", "убери", "убрать", "прибери", "прибрати", "удали", "удалить",
                     "исключи", "виключи", "без", "except", "remove", "not", "нет", "ні", "и", "і", "та", "or",
                     "and", "сайт", "сайты", "сайти", "номер", "номера", "только", "тільки", "лише", "лишь",
                     "оставь", "залиш", "only", "keep", "все", "всі", "the", "site", "sites", "www", "com",
                     "да", "так", "пожалуйста", "будь", "ласка", "плиз", "please", "остальные", "інші", "решту",
                     "ок", "ok", "нужен", "нужны", "не", "надо", "треба", "этот", "этих", "эти", "ці"})
_TEXT_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _norm(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").strip(" .!,;").split())


def parse_answer(text: str, sites: Sequence[Site]) -> Answer | None:
    """The person's answer about ``sites`` (the listed ones, numbered), or None when it is not one.

    «да» / «все» -> all; numbers, ranges («3-5») or names («olx», «idealista.com») -> those out;
    «только 1, 2» -> only those in. Names match a host by its label (``olx`` -> ``olx.ua``).
    """
    folded = _norm(text)
    if not folded:
        return None
    if folded in _YES:
        return Answer("all")
    by_number = {s.number: s.host for s in sites if s.number is not None}
    hosts: set[str] = set()
    unknown: list[str] = []
    for match in _NUMBER.finditer(folded):
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if last < first or last - first > 100:
            unknown.append(match.group(0))
            continue
        for number in range(first, last + 1):
            if number in by_number:
                hosts.add(by_number[number])
            else:
                unknown.append(str(number))
    rest = _NUMBER.sub(" ", folded)
    names = {s.host for s in sites} | {s.host.split(".")[0] for s in sites}
    strangers = [w for w in _TEXT_WORD.findall(rest) if w not in _FILLER and w not in names
                 and not any(len(w) >= 4 and w in s.host for s in sites)
                 and w not in ("www", "com", "es", "ua", "net", "org", "ru")]
    if len(strangers) > 1:  # a sentence about something else (a new task with numbers), not an answer
        return None
    for word in _WORD.findall(rest):
        word = word.lower().removeprefix("www.")
        if word in _FILLER or word.isdigit() or len(word) < 2:
            continue
        matched = [s.host for s in sites if word == s.host or word == s.host.split(".")[0]
                   or (len(word) >= 4 and word in s.host)]
        if matched:
            hosts.update(matched)
        elif "." in word or len(word) >= 3:
            unknown.append(word)
    if not hosts:
        return None
    return Answer("only" if _ONLY.search(folded) else "reject", frozenset(hosts), tuple(dict.fromkeys(unknown)))


def apply_answer(answer: Answer, sites: Iterable[Site]) -> tuple[set[str], set[str]]:
    """(approved, rejected) hosts among the pending ``sites`` after ``answer``."""
    pending = {s.host for s in sites if s.state == "pending"}
    if answer.kind == "all":
        return pending, set()
    if answer.kind == "only":
        return pending & answer.hosts, pending - answer.hosts
    return pending - answer.hosts, pending & answer.hosts


# --- the question --------------------------------------------------------------------------------------


def question_messages(sites: Sequence[Site], *, saved: int = 0) -> list[str]:
    """The numbered list, grouped by query, split to fit Telegram; the question closes the last message."""
    head = "Сайты, на которых буду искать по вашей задаче:"
    if saved:
        head += f"\nУже одобрены вами раньше: {saved} (их не спрашиваю)."
    groups: dict[str, list[Site]] = {}
    for site in sorted(sites, key=lambda s: s.number or 0):
        groups.setdefault(site.query or "", []).append(site)
    blocks: list[str] = []
    for query, members in groups.items():
        lines = [f"По запросу «{query}»:" if query else "Другие:"]
        lines += [f"{s.number}. {s.host}" for s in members]
        blocks.append("\n".join(lines))
    messages: list[str] = []
    current = head
    for block in blocks:
        if len(current) + len(block) + 2 > MAX_MESSAGE:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"
    tail = f"\n\n{QUESTION}"
    if len(current) + len(tail) > MAX_MESSAGE:
        messages.append(current)
        current = QUESTION
    else:
        current += tail
    messages.append(current)
    return messages


def summary(approved: Sequence[Site], rejected: Sequence[Site], unknown: Sequence[str] = ()) -> str:
    """«Убрал: 3. olx.ua … Ищу по 7 сайтам: idealista.com, …»."""
    lines: list[str] = []
    if rejected:
        lines.append("Убрал: " + ", ".join(f"{s.number}. {s.host}" if s.number else s.host
                                          for s in sorted(rejected, key=lambda s: s.number or 0)))
    if approved:
        names = [s.host for s in sorted(approved, key=lambda s: s.number or 0)]
        shown = ", ".join(names[:15]) + (f" и ещё {len(names) - 15}" if len(names) > 15 else "")
        lines.append(f"Ищу по {len(names)} {_plural(len(names))}: {shown}.")
    else:
        lines.append("Ни один из новых сайтов не одобрен: по ним не ищу.")
    if unknown:
        lines.append("Не нашёл в списке: " + ", ".join(unknown[:10]) + ".")
    return "\n".join(lines)


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "сайту"
    return "сайтам"


def callback_data(action: str, campaign_id: str) -> str:
    """``sites:all|edit:<campaign uuid>`` (at most 51 bytes; Telegram allows 64)."""
    return f"{CALLBACK_KIND}:{action}:{campaign_id}"


def buttons(campaign_id: str) -> tuple[tuple[str, str], ...]:
    return ((ALL_BUTTON, callback_data("all", campaign_id)), (EDIT_BUTTON, callback_data("edit", campaign_id)))


def parse_callback(action: str, target: str) -> tuple[str, str] | None:
    if action not in ("all", "edit") or not _UUID.match(target):
        return None
    return action, target


# --- storage -------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Owner:
    """Whose decisions apply: the person who gave the task, in its mode and place."""

    campaign_id: str
    user_id: int
    chat_id: int
    vertical: str
    location: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    campaign_id: str
    batch: int
    requested_by: int
    chat_id: int
    asked_at: datetime
    reminded: bool
    sites: tuple[Site, ...]
    delivered: bool = True  # False: sending the list failed; it is sent again on the next check


class SiteStore(Protocol):
    async def saved(self, user_id: int, vertical: str, hosts: Sequence[str]) -> dict[str, str]: ...
    async def approved_hosts(self, user_id: int, vertical: str, country: str | None, limit: int) -> list[str]: ...
    async def campaign_sites(self, campaign_id: str) -> dict[str, Site]: ...
    async def add_sites(self, campaign_id: str, sites: Sequence[Site], batch: int) -> None: ...
    async def batches(self, campaign_id: str) -> int: ...
    async def open_question(self, campaign_id: str) -> OpenQuestion | None: ...
    async def ask(self, owner: Owner, batch: int, message_ids: Sequence[int]) -> None: ...
    async def reminded(self, campaign_id: str, batch: int) -> None: ...
    async def delivered(self, campaign_id: str, batch: int, message_ids: Sequence[int]) -> None: ...
    async def question_for(self, user_id: int, campaign_id: str | None = None) -> OpenQuestion | None: ...
    async def decide(self, question: OpenQuestion, owner: Owner, approved: set[str], rejected: set[str],
                     answered_by: int) -> bool: ...
    async def touch(self, user_id: int, vertical: str, hosts: Sequence[str]) -> None: ...
    async def owner(self, campaign_id: str) -> Owner | None: ...


class Sender(Protocol):
    async def send(self, chat_id: int, text: str) -> int: ...
    async def send_buttons(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> int: ...


@dataclass(frozen=True, slots=True)
class GateConfig:
    enabled: bool = True
    remind_after_minutes: int = 30
    max_batches: int = 3  # questions per campaign; later new sites are not read (source 'unasked')

    def __post_init__(self) -> None:
        if not (1 <= self.remind_after_minutes <= 24 * 60 and 1 <= self.max_batches <= 20):
            raise ValueError("unsafe site gate settings")


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the web stage may do now: read ``allowed`` hosts, skip ``rejected``; ``waiting`` for an answer."""

    allowed: frozenset[str]
    rejected: frozenset[str]
    waiting: bool


class SiteGate:
    def __init__(self, store: SiteStore, sender: Sender, *, config: GateConfig | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.store, self.sender, self.config, self.now = store, sender, config or GateConfig(), now

    async def preferred(self, user_id: int, vertical: str, country: str | None, limit: int = 8) -> tuple[str, ...]:
        """The person's approved sites in this mode (most recently used first): searched first."""
        if not self.config.enabled:
            return ()
        return tuple(await self.store.approved_hosts(user_id, vertical, country, limit))

    async def check(self, owner: Owner, queued: dict[str, tuple[str | None, str | None]]) -> Verdict:
        """Decide the queued hosts (host -> (query, language)) and ask about new ones once per batch."""
        known = await self.store.campaign_sites(owner.campaign_id)
        new = [host for host in queued if host not in known]
        if new:
            saved = await self.store.saved(owner.user_id, owner.vertical, new)
            batch = await self.store.batches(owner.campaign_id) + 1
            open_question = await self.store.open_question(owner.campaign_id)
            can_ask = open_question is None and batch <= self.config.max_batches
            numbered = max((s.number or 0 for s in known.values()), default=0)
            rows: list[Site] = []
            for host in new:
                query, language = queued[host]
                status = saved.get(host)
                if status in ("approved", "rejected"):
                    rows.append(Site(host, status, None, query, language, "saved"))  # type: ignore[arg-type]
                elif can_ask:
                    numbered += 1
                    rows.append(Site(host, "pending", numbered, query, language, "asked"))
                elif open_question is None:  # past the question cap: not read
                    rows.append(Site(host, "rejected", None, query, language, "unasked"))
                # else: asked in the next batch, once the open question is answered
            asked = [s for s in rows if s.source == "asked"]
            await self.store.add_sites(owner.campaign_id, rows, batch if asked else 0)
            if asked:
                await self._ask(owner, batch, asked, saved=sum(1 for s in [*known.values(), *rows]
                                                               if s.source == "saved" and s.state == "approved"))
            known = await self.store.campaign_sites(owner.campaign_id)
        open_question = await self.store.open_question(owner.campaign_id)
        if open_question is not None and not open_question.delivered:
            ids = await self._send(owner, list(open_question.sites), saved=0)
            if ids:
                await self.store.delivered(owner.campaign_id, open_question.batch, ids)
        elif open_question is not None and not open_question.reminded and \
                self.now() - open_question.asked_at > timedelta(minutes=self.config.remind_after_minutes):
            try:
                await self.sender.send(open_question.chat_id, REMINDER)
            except Exception:  # noqa: BLE001 - a lost reminder only delays the answer
                log.warning("web_search.sites_reminder_failed", extra={"campaign_id": owner.campaign_id})
            await self.store.reminded(owner.campaign_id, open_question.batch)
        allowed = frozenset(h for h, s in known.items() if s.state == "approved")
        rejected = frozenset(h for h, s in known.items() if s.state == "rejected")
        waiting = any(h not in allowed and h not in rejected for h in queued)
        if allowed:
            await self.store.touch(owner.user_id, owner.vertical, sorted(allowed & set(queued)))
        return Verdict(allowed, rejected, waiting)

    async def _ask(self, owner: Owner, batch: int, sites: list[Site], *, saved: int) -> None:
        ids = await self._send(owner, sites, saved=saved)
        await self.store.ask(owner, batch, ids)
        log.info("web_search.sites_asked", extra={"campaign_id": owner.campaign_id, "batch": batch,
                                                  "sites": len(sites), "delivered": bool(ids)})

    async def _send(self, owner: Owner, sites: list[Site], *, saved: int) -> list[int]:
        """The list (in parts when long), the buttons under the last part; [] when Telegram failed."""
        messages = question_messages(sites, saved=saved)
        ids: list[int] = []
        try:
            for text in messages[:-1]:
                ids.append(await self.sender.send(owner.chat_id, text))
            ids.append(await self.sender.send_buttons(owner.chat_id, messages[-1], buttons(owner.campaign_id)))
        except Exception:  # noqa: BLE001 - the question stays open and is sent again on the next check
            log.warning("web_search.sites_question_failed", extra={"campaign_id": owner.campaign_id})
            return []
        return ids


@dataclass(frozen=True, slots=True)
class Decision:
    reply: str
    done: bool  # the answer was taken


class SiteDesk:
    """The control plane's side: answers to the open question of a person's search."""

    def __init__(self, store: SiteStore) -> None:
        self.store = store

    async def waiting(self, user_id: int) -> bool:
        return await self.store.question_for(user_id) is not None

    async def on_text(self, user_id: int, text: str) -> Decision | None:
        """The text answer to the person's open question; None when there is none or the text is no answer."""
        question = await self.store.question_for(user_id)
        if question is None:
            return None
        answer = parse_answer(text, question.sites)
        if answer is None:
            return None
        return await self._decide(question, answer, user_id)

    async def on_button(self, user_id: int, action: str, campaign_id: str, *, owner: bool = False) -> Decision:
        question = await self.store.question_for(user_id, campaign_id)
        if question is None and owner:
            question = await self.store.open_question(campaign_id)
        if question is None:
            return Decision("Этот список уже подтверждён.", False)
        if action == "edit":
            return Decision(EDIT_HINT, False)
        return await self._decide(question, Answer("all"), user_id)

    async def _decide(self, question: OpenQuestion, answer: Answer, user_id: int) -> Decision:
        approved, rejected = apply_answer(answer, question.sites)
        owner = await self.store.owner(question.campaign_id)
        if owner is None:
            return Decision("Этот поиск уже завершён.", False)
        if not await self.store.decide(question, owner, approved, rejected, user_id):
            return Decision("Ответ уже учтён.", False)
        by_host = {s.host: s for s in question.sites}
        log.info("web_search.sites_answered", extra={"campaign_id": question.campaign_id,
                                                     "approved": len(approved), "rejected": len(rejected)})
        return Decision(summary([by_host[h] for h in approved], [by_host[h] for h in rejected], answer.unknown),
                        True)


# --- PostgreSQL ----------------------------------------------------------------------------------------


class PostgresSiteStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def saved(self, user_id: int, vertical: str, hosts: Sequence[str]) -> dict[str, str]:
        rows = await self.pool.fetch(
            """select host, status from search_sites
                where user_id = $1 and vertical = $2 and host = any($3::text[])""", user_id, vertical, list(hosts))
        return {r["host"]: r["status"] for r in rows}

    async def approved_hosts(self, user_id: int, vertical: str, country: str | None, limit: int) -> list[str]:
        rows = await self.pool.fetch(
            """select host from search_sites
                where user_id = $1 and vertical = $2 and status = 'approved'
                  and ($3::text is null or country is null or country = $3)
                order by last_used_at desc nulls last, decided_at desc limit $4""",
            user_id, vertical, country, limit)
        return [r["host"] for r in rows]

    async def campaign_sites(self, campaign_id: str) -> dict[str, Site]:
        rows = await self.pool.fetch(
            """select host, state, number, query_text, language, source from campaign_sites
                where campaign_id = $1::uuid""", campaign_id)
        return {r["host"]: Site(r["host"], r["state"], r["number"], r["query_text"], r["language"], r["source"])
                for r in rows}

    async def add_sites(self, campaign_id: str, sites: Sequence[Site], batch: int) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            for s in sites:
                await conn.execute(
                    """insert into campaign_sites (campaign_id, host, number, batch, query_text, language, state,
                                                   source, decided_at)
                       values ($1::uuid, $2, $3, $4, $5, $6, $7, $8,
                               case when $7 <> 'pending' then now() end)
                       on conflict (campaign_id, host) do nothing""",
                    campaign_id, s.host, s.number, batch if s.source == "asked" else 0,
                    (s.query or None) and s.query[:200], (s.language or None) and s.language[:10], s.state, s.source)

    async def batches(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select coalesce(max(batch), 0) from campaign_site_questions where campaign_id = $1::uuid", campaign_id))

    async def open_question(self, campaign_id: str) -> OpenQuestion | None:
        row = await self.pool.fetchrow(
            """select campaign_id::text, batch, requested_by, chat_id, asked_at, reminded_at,
                      cardinality(message_ids) > 0 as delivered
                 from campaign_site_questions where campaign_id = $1::uuid and state = 'asked'
                order by batch limit 1""", campaign_id)
        return await self._question(row)

    async def question_for(self, user_id: int, campaign_id: str | None = None) -> OpenQuestion | None:
        row = await self.pool.fetchrow(
            """select q.campaign_id::text, q.batch, q.requested_by, q.chat_id, q.asked_at, q.reminded_at,
                      cardinality(q.message_ids) > 0 as delivered
                 from campaign_site_questions q join campaigns c on c.id = q.campaign_id
                where q.requested_by = $1 and q.state = 'asked'
                  and ($2::uuid is null or q.campaign_id = $2::uuid)
                  and c.state not in ('completed', 'cancelled', 'failed')
                order by q.asked_at desc limit 1""", user_id, campaign_id)
        return await self._question(row)

    async def _question(self, row: Any) -> OpenQuestion | None:
        if row is None:
            return None
        sites = await self.pool.fetch(
            """select host, state, number, query_text, language, source from campaign_sites
                where campaign_id = $1::uuid and batch = $2 order by number""", row["campaign_id"], row["batch"])
        return OpenQuestion(row["campaign_id"], row["batch"], row["requested_by"], row["chat_id"], row["asked_at"],
                            row["reminded_at"] is not None,
                            tuple(Site(r["host"], r["state"], r["number"], r["query_text"], r["language"], r["source"])
                                  for r in sites), row["delivered"])

    async def ask(self, owner: Owner, batch: int, message_ids: Sequence[int]) -> None:
        await self.pool.execute(
            """insert into campaign_site_questions (campaign_id, batch, chat_id, requested_by, message_ids)
               values ($1::uuid, $2, $3, $4, $5::bigint[]) on conflict (campaign_id, batch) do nothing""",
            owner.campaign_id, batch, owner.chat_id, owner.user_id, list(message_ids))

    async def delivered(self, campaign_id: str, batch: int, message_ids: Sequence[int]) -> None:
        await self.pool.execute(
            """update campaign_site_questions set message_ids = $3::bigint[], asked_at = now()
                where campaign_id = $1::uuid and batch = $2""", campaign_id, batch, list(message_ids))

    async def reminded(self, campaign_id: str, batch: int) -> None:
        await self.pool.execute(
            """update campaign_site_questions set reminded_at = now()
                where campaign_id = $1::uuid and batch = $2 and reminded_at is null""", campaign_id, batch)

    async def decide(self, question: OpenQuestion, owner: Owner, approved: set[str], rejected: set[str],
                     answered_by: int) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            closed = await conn.fetchval(
                """update campaign_site_questions set state = 'answered', answered_at = now(), answered_by = $3
                    where campaign_id = $1::uuid and batch = $2 and state = 'asked' returning batch""",
                question.campaign_id, question.batch, answered_by)
            if closed is None:
                return False
            for hosts, state in ((approved, "approved"), (rejected, "rejected")):
                if not hosts:
                    continue
                await conn.execute(
                    """update campaign_sites set state = $3, decided_at = now()
                        where campaign_id = $1::uuid and host = any($2::text[]) and state = 'pending'""",
                    question.campaign_id, sorted(hosts), state)
                await conn.execute(
                    """insert into search_sites (user_id, host, vertical, status, location, country, query_text,
                                                 language, campaign_id, last_used_at)
                       select $1, s.host, $3, $4, $5, $6, s.query_text, s.language, $7::uuid,
                              case when $4 = 'approved' then now() end
                         from campaign_sites s where s.campaign_id = $7::uuid and s.host = any($2::text[])
                       on conflict (user_id, host, vertical) do update
                          set status = excluded.status, decided_at = now(), campaign_id = excluded.campaign_id,
                              location = excluded.location, country = excluded.country""",
                    owner.user_id, sorted(hosts), owner.vertical, state, owner.location, owner.country,
                    question.campaign_id)
            return True

    async def touch(self, user_id: int, vertical: str, hosts: Sequence[str]) -> None:
        if hosts:
            await self.pool.execute(
                """update search_sites set last_used_at = now()
                    where user_id = $1 and vertical = $2 and host = any($3::text[]) and status = 'approved'
                      and (last_used_at is null or last_used_at < now() - interval '1 hour')""",
                user_id, vertical, list(hosts))

    async def owner(self, campaign_id: str) -> Owner | None:
        row = await self.pool.fetchrow(
            """select id::text, requested_by, telegram_chat_id as chat_id, plan->>'vertical' as vertical, plan->>'location' as location,
                      plan->>'country' as country
                 from campaigns where id = $1::uuid and state not in ('completed', 'cancelled', 'failed')""",
            campaign_id)
        if row is None:
            return None
        return Owner(row["id"], row["requested_by"], row["chat_id"], row["vertical"] or "real_estate",
                     row["location"], row["country"])


# --- in memory (tests) ---------------------------------------------------------------------------------


@dataclass
class _Question:
    owner: Owner
    batch: int
    asked_at: datetime
    message_ids: list[int]
    state: str = "asked"
    reminded: bool = False


@dataclass
class MemorySiteStore:
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    owners: dict[str, Owner] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    decisions: dict[tuple[int, str, str], tuple[str, str | None, datetime | None]] = field(default_factory=dict)
    sites: dict[str, dict[str, tuple[Site, int]]] = field(default_factory=dict)
    questions: dict[tuple[str, int], _Question] = field(default_factory=dict)

    async def saved(self, user_id: int, vertical: str, hosts: Sequence[str]) -> dict[str, str]:
        return {h: self.decisions[(user_id, h, vertical)][0] for h in hosts if (user_id, h, vertical) in self.decisions}

    async def approved_hosts(self, user_id: int, vertical: str, country: str | None, limit: int) -> list[str]:
        rows = [(used or datetime.min.replace(tzinfo=UTC), h) for (u, h, v), (status, c, used) in self.decisions.items()
                if u == user_id and v == vertical and status == "approved" and (country is None or c in (None, country))]
        return [h for _, h in sorted(rows, reverse=True)][:limit]

    async def campaign_sites(self, campaign_id: str) -> dict[str, Site]:
        return {h: s for h, (s, _) in self.sites.get(campaign_id, {}).items()}

    async def add_sites(self, campaign_id: str, sites: Sequence[Site], batch: int) -> None:
        bucket = self.sites.setdefault(campaign_id, {})
        for s in sites:
            bucket.setdefault(s.host, (s, batch if s.source == "asked" else 0))

    async def batches(self, campaign_id: str) -> int:
        return max((b for (c, b) in self.questions if c == campaign_id), default=0)

    def _open(self, question: _Question) -> OpenQuestion:
        cid = question.owner.campaign_id
        members = sorted((s for s, b in self.sites.get(cid, {}).values() if b == question.batch),
                         key=lambda s: s.number or 0)
        return OpenQuestion(cid, question.batch, question.owner.user_id, question.owner.chat_id, question.asked_at,
                            question.reminded, tuple(members), bool(question.message_ids))

    async def open_question(self, campaign_id: str) -> OpenQuestion | None:
        for (c, _), q in sorted(self.questions.items()):
            if c == campaign_id and q.state == "asked":
                return self._open(q)
        return None

    async def question_for(self, user_id: int, campaign_id: str | None = None) -> OpenQuestion | None:
        open_ = [q for q in self.questions.values() if q.state == "asked" and q.owner.user_id == user_id
                 and q.owner.campaign_id not in self.finished
                 and (campaign_id is None or q.owner.campaign_id == campaign_id)]
        return self._open(max(open_, key=lambda q: q.asked_at)) if open_ else None

    async def ask(self, owner: Owner, batch: int, message_ids: Sequence[int]) -> None:
        self.owners[owner.campaign_id] = owner
        self.questions.setdefault((owner.campaign_id, batch), _Question(owner, batch, self.now(), list(message_ids)))

    async def reminded(self, campaign_id: str, batch: int) -> None:
        self.questions[(campaign_id, batch)].reminded = True

    async def delivered(self, campaign_id: str, batch: int, message_ids: Sequence[int]) -> None:
        question = self.questions[(campaign_id, batch)]
        question.message_ids, question.asked_at = list(message_ids), self.now()

    async def decide(self, question: OpenQuestion, owner: Owner, approved: set[str], rejected: set[str],
                     answered_by: int) -> bool:
        stored = self.questions.get((question.campaign_id, question.batch))
        if stored is None or stored.state != "asked":
            return False
        stored.state = "answered"
        bucket = self.sites[question.campaign_id]
        for hosts, state in ((approved, "approved"), (rejected, "rejected")):
            for host in hosts:
                site, batch = bucket[host]
                bucket[host] = (Site(site.host, state, site.number, site.query, site.language, site.source), batch)  # type: ignore[arg-type]
                self.decisions[(owner.user_id, host, owner.vertical)] = (
                    state, owner.country, self.now() if state == "approved" else None)
        return True

    async def touch(self, user_id: int, vertical: str, hosts: Sequence[str]) -> None:
        for host in hosts:
            key = (user_id, host, vertical)
            if key in self.decisions and self.decisions[key][0] == "approved":
                self.decisions[key] = ("approved", self.decisions[key][1], self.now())

    async def owner(self, campaign_id: str) -> Owner | None:
        return None if campaign_id in self.finished else self.owners.get(campaign_id)
