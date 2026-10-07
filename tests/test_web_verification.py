"""Human verification for the web search: a challenge page in the browser becomes a verification job for a person.

Fakes only: the search engine, the sites, the browser. Nothing here solves a challenge; a person does, and the
tests move the job's state the way the verification service does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from bot.campaign import MemoryCampaignStore
from bot.campaign.summary import site_lines, summary_text
from bot.verification.browser import judge_website
from bot.verification.classify import classify_website
from bot.verification.models import Job, Recovery
from bot.verification.service import FlowConfig, VerificationService
from bot.verification.store import MemoryVerificationStore
from bot.web_search.models import QueuedUrl
from bot.web_search.render import BrowserRenderer, ChallengeDetected, RenderedPage
from bot.web_search.settings import WebSearchSettings
from bot.web_search.store import MemoryWebStore
from bot.web_search.urls import url_key
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_live_view import TOKEN, init_data
from tests.test_verification_flow import (
    OPERATOR,
    OWNER,
    PUBLIC,
    FakeLive,
    FakeNotifier,
    FakeWatchdog,
    token_of,
)
from tests.test_web_search import FakeFetcher, FakeSearcher, ListGenerator, campaign

HIT = ("Terreno en venta en Boadilla del Monte - idealista",
       "Terreno urbanizable de 1.200 m² en Boadilla del Monte, Madrid. 480.000 €. Todos los servicios.")
IDEALISTA = [f"https://www.idealista.com/inmueble/{61000000 + n}/" for n in range(5)]
FOTOCASA = [f"https://www.fotocasa.es/es/comprar/terreno/boadilla-del-monte/sin-urbanizar/{183456000 + n}/d" for n in range(2)]
TEXT = "Terreno urbanizable de 1.200 m² en Boadilla del Monte, Madrid. 480.000 €. Todos los servicios. " * 3


class SiteRenderer:
    """The browser: a site in ``challenged`` shows a challenge page (raised as ``ChallengeDetected``, as the real
    renderer does when human verification is on); every other page is a listing."""

    def __init__(self, challenged: set[str] | None = None) -> None:
        self.challenged, self.calls = set(challenged or ()), []

    async def render(self, url: str) -> RenderedPage:
        self.calls.append(url)
        host = url.split("/")[2].removeprefix("www.")
        if host in self.challenged:
            raise ChallengeDetected("captcha", url, host)
        return RenderedPage(url, "Terreno", f"{url} {TEXT}")


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


async def make(urls: list[str], *, fetcher: FakeFetcher, renderer: SiteRenderer, clock: Clock | None = None,
               **config: object):
    clock = clock or Clock()
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns, now=clock)
    searcher = FakeSearcher(default=list(urls), texts={u: HIT for u in urls})
    settings = {"pages_per_tick": 4, "cover_portals": False, "human_verification": True, **config}
    worker = WebSearchWorker(campaigns, store, searcher, fetcher, ListGenerator(["terreno Boadilla Madrid"]),
                             renderer=renderer, config=WebSearchConfig(**settings), now=clock)
    return worker, store, cid, clock


def refused(*urls: str) -> FakeFetcher:
    return FakeFetcher(errors={u: "http_403" for u in urls})


async def ticks(worker: WebSearchWorker, n: int) -> None:
    for _ in range(n):
        await worker.tick()


def queued(store: MemoryWebStore, cid: str, host: str) -> list[str]:
    return [u.url for u in store.urls[cid].values() if u.host == host and u.state == "queued"]


# --- challenge -> one job, the site waits, the others go on ---------------------------------------------------------------


async def test_a_challenge_opens_one_job_pauses_the_site_and_the_other_sites_go_on() -> None:
    fetcher = refused(*IDEALISTA)
    renderer = SiteRenderer({"idealista.com"})
    worker, store, cid, _ = await make([*IDEALISTA[:3], *FOTOCASA], fetcher=fetcher, renderer=renderer)
    await ticks(worker, 12)

    [job] = store.verification_jobs
    assert job["host"] == "idealista.com" and job["state"] == "requested" and job["kind"] == "captcha"
    assert job["url"] in IDEALISTA
    assert len(renderer.calls) == 1  # the site was asked once; its other URLs were not tried
    # the paused site's URLs stay queued, nothing counted against it
    assert sorted(queued(store, cid, "idealista.com")) == sorted(IDEALISTA[:3])
    assert store.hosts["idealista.com"]["render_refusals"] == 0
    assert store.hosts["idealista.com"]["http_refusals"] == 0  # not even the HTTP 403 that led to the browser
    assert await store.renders_used(cid) == 0 and await worker.store.host_attempts(cid, "idealista.com") == 0
    assert store.deferred_fetches == [url_key(job["url"])]  # given back unread
    # the other site was read
    assert sorted(p["url"] for p in store.posts) == sorted(FOTOCASA)
    # the stage waits for the person instead of finishing
    assert (await store.get_run(cid)).state == "searching"
    status = await store.web_status(cid)
    assert status.active and status.verification == ("idealista.com",)


async def test_a_second_challenge_of_the_same_site_opens_no_second_job() -> None:
    fetcher = refused(*IDEALISTA)
    renderer = SiteRenderer({"idealista.com"})
    worker, store, _cid, _ = await make(IDEALISTA, fetcher=fetcher, renderer=renderer)
    await ticks(worker, 8)
    assert len(store.verification_jobs) == 1 and len(renderer.calls) == 1
    first = await store.open_verification("idealista.com", "captcha", IDEALISTA[1])  # another reader meets it too
    assert first == store.verification_jobs[0]["id"] and len(store.verification_jobs) == 1


async def test_without_human_verification_a_challenge_page_is_a_render_refusal_as_before() -> None:
    fetcher = refused(*IDEALISTA[:1])
    renderer = SiteRenderer({"idealista.com"})
    worker, store, cid, _ = await make(IDEALISTA[:1], fetcher=fetcher, renderer=renderer, human_verification=False)
    await ticks(worker, 12)
    assert store.verification_jobs == []
    assert store.hosts["idealista.com"]["http_refusals"] == 1
    assert [p["via"] for p in store.posts] == ["search"]  # the search-result card, the old behaviour
    assert (await store.get_run(cid)).state == "done"


# --- solved -> read through the profile, gently, within the page budget ---------------------------------------------------


async def test_after_a_passed_check_the_queue_is_read_through_the_browser_slowly_and_within_the_budget() -> None:
    fetcher = refused(*IDEALISTA)
    renderer = SiteRenderer({"idealista.com"})
    worker, store, cid, clock = await make(IDEALISTA, fetcher=fetcher, renderer=renderer,
                                           pages_per_verification=2, verified_host_interval_seconds=8)
    await ticks(worker, 4)
    assert len(store.verification_jobs) == 1 and store.posts == []
    renderer.challenged.clear()  # the person passed it
    store.set_job_state("idealista.com", "verified")

    fetched_before, renders_before = list(fetcher.fetched), len(renderer.calls)
    await worker.tick()
    assert renderer.calls[renders_before:] == [IDEALISTA[0]] and fetcher.fetched == fetched_before  # browser, no plain HTTP
    await worker.tick()  # the same moment: the site is not read again before the interval
    assert len(renderer.calls) == renders_before + 1 and fetcher.fetched == fetched_before
    clock.advance(9)
    await worker.tick()
    # the second page of the check goes through the browser as well; the check's budget (2) is then used up and the
    # rest of the queue is read as usual: plain HTTP first (refused here), then the browser
    assert renderer.calls[renders_before + 1] == IDEALISTA[1]
    assert fetcher.fetched == [*fetched_before, *IDEALISTA[2:]]
    assert len(store.posts) == 5
    # the verified pages were not on the campaign's own render budget; the others were
    assert await store.renders_used(cid) == 3


async def test_a_new_challenge_after_a_passed_one_pauses_the_site_again_with_a_new_job() -> None:
    fetcher = refused(*IDEALISTA)
    renderer = SiteRenderer({"idealista.com"})
    worker, store, cid, clock = await make(IDEALISTA, fetcher=fetcher, renderer=renderer)
    await ticks(worker, 3)
    renderer.challenged.clear()
    store.set_job_state("idealista.com", "verified")
    await worker.tick()
    assert len(store.posts) == 1
    renderer.challenged.add("idealista.com")  # the check came back
    clock.advance(9)
    await worker.tick()
    assert [j["state"] for j in store.verification_jobs] == ["verified", "requested"]
    assert len(store.posts) == 1
    assert (await store.host_verification("idealista.com", cid)).state == "open"


async def test_while_a_person_holds_the_browser_other_sites_do_not_use_it() -> None:
    fetcher = FakeFetcher(errors={FOTOCASA[0]: "http_403"})
    renderer = SiteRenderer()
    worker, store, _cid, _ = await make([IDEALISTA[0], FOTOCASA[0]], fetcher=fetcher, renderer=renderer)
    await store.open_verification("idealista.com", "captcha", IDEALISTA[0])
    store.set_job_state("idealista.com", "active")  # claimed: the live browser has the profile
    await ticks(worker, 6)
    assert renderer.calls == []  # fotocasa's refused page did not go to the busy browser (its card was kept)
    assert [p["via"] for p in store.posts] == ["search"]


# --- nobody passes it -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["expired", "cancelled", "rejected"])
async def test_an_unsolved_job_makes_the_site_unreadable_and_reports_it(state: str) -> None:
    fetcher = refused(*IDEALISTA[:3])
    renderer = SiteRenderer({"idealista.com"})
    worker, store, cid, _ = await make([*IDEALISTA[:3], FOTOCASA[0]], fetcher=fetcher, renderer=renderer)
    await ticks(worker, 8)
    assert (await store.get_run(cid)).state == "searching"
    store.set_job_state("idealista.com", state)
    for _ in range(12):
        await worker.tick()
        if (await store.get_run(cid)).state != "searching":
            break
    assert (await store.get_run(cid)).state == "done"
    assert queued(store, cid, "idealista.com") == []
    assert len(renderer.calls) == 1  # never asked again
    reports = {r.host: r for r in await store.site_report(cid)}
    assert reports["idealista.com"].unverified and reports["idealista.com"].refused == 3
    assert not reports["fotocasa.es"].unverified
    lines, _ = site_lines([], list(reports.values()), ("idealista.com", "fotocasa.es"))
    assert "Idealista — проверку никто не прошёл" in lines


async def test_the_stage_ending_while_a_check_is_pending_still_reports_it() -> None:
    fetcher = refused(*IDEALISTA[:2])
    renderer = SiteRenderer({"idealista.com"})
    worker, store, cid, _ = await make(IDEALISTA[:2], fetcher=fetcher, renderer=renderer, max_pages_per_campaign=1)
    await ticks(worker, 4)
    await worker._done(await worker.campaigns.get(cid), "time_cap")
    assert {r.host: r.unverified for r in await store.site_report(cid)} == {"idealista.com": True}


def test_the_report_line_for_an_unverified_site() -> None:
    from bot.web_search.models import SiteReport

    text = summary_text("Участок в Мадриде", [], [SiteReport("idealista.com", links=3, refused=3, unverified=True)],
                        portals=("idealista.com", "fotocasa.es"))
    assert "Idealista — проверку никто не прошёл" in text


# --- detection ----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("snapshot", "kind"), [
    ({"title": "Just a moment...", "text": "Checking your browser before accessing the site."}, "interstitial"),
    ({"title": "", "text": "Please solve the CAPTCHA to continue"}, "captcha"),
    ({"title": "Idealista", "text": "", "frames": ["https://geo.captcha-delivery.com/captcha/?initialCid=x"]}, "captcha"),
    ({"title": "", "text": "Verifying", "frames": ["https://challenges.cloudflare.com/turnstile/v0/api.js"]}, "interstitial"),
    ({"title": "Are you a robot?", "text": "Confirm that you are not a robot."}, "interstitial"),
    ({"title": "", "text": "Access denied"}, "access_denied"),
    ({"title": "403", "text": "Acceso denegado. Eres humano?"}, "interstitial"),
])
def test_challenge_pages_are_recognised(snapshot: dict, kind: str) -> None:
    assert classify_website(snapshot) == kind


@pytest.mark.parametrize("snapshot", [
    {"title": "Terreno", "text": TEXT * 3, "frames": ["https://www.google.com/recaptcha/api.js"]},  # a contact form's reCAPTCHA
    {"title": "Piso", "text": "captcha " + "x" * 900},
    {"title": "Piso", "text": "Terreno de 1.200 m2 en venta. Acceso denegado a vehículos: " + "y" * 300},
    {"title": "Entrar", "text": "Inicia sesión", "url": "https://www.example.com/login"},  # a login link is not a challenge
    {},
])
def test_ordinary_pages_are_not_challenges(snapshot: dict) -> None:
    assert classify_website(snapshot) is None


def test_the_watchdog_judges_a_website_by_its_own_signals_only() -> None:
    assert judge_website({"title": "Terreno", "text": TEXT, "url": "https://x.es/login"}) == Recovery(True)
    again = judge_website({"title": "Just a moment...", "text": "Checking your browser"})
    assert not again.clear and again.kind == "interstitial"


class _Browser:
    def __init__(self, page: dict) -> None:
        self.page, self.acquired = page, []

    async def acquire(self, profile_id, profile_name, persisted_state, *, platform="facebook"):
        self.acquired.append((profile_id, profile_name, platform))
        return object()

    async def snapshot(self, lease, url, timeout_ms):
        return self.page

    async def release(self, lease, next_state="READY"):
        return None


async def test_the_browser_renderer_raises_a_typed_challenge_only_when_asked_to() -> None:
    page = {"url": "https://www.idealista.com/x", "title": "Just a moment...", "text": "Checking your browser"}
    plain = BrowserRenderer(_Browser(page), profile_id="web-search-render")
    assert (await plain.render("https://www.idealista.com/x")).title == "Just a moment..."  # old behaviour: a page
    asked = BrowserRenderer(_Browser(page), detect_challenges=True)
    with pytest.raises(ChallengeDetected) as caught:
        await asked.render("https://www.idealista.com/x")
    assert (caught.value.kind, caught.value.url, caught.value.host) == ("interstitial", "https://www.idealista.com/x", "idealista.com")


async def test_the_renderer_uses_the_profile_row_the_verification_flow_opens() -> None:
    browser = _Browser({"url": "https://www.idealista.com/x", "title": "Terreno", "text": TEXT})
    store = MemoryWebStore()
    renderer = BrowserRenderer(browser, profile_source=store.render_profile, detect_challenges=True)
    await renderer.render("https://www.idealista.com/x")
    await renderer.render("https://www.idealista.com/y")
    profile_id, name = await store.render_profile()
    assert browser.acquired == [(profile_id, name, "website")] * 2


# --- the verification flow for a website job ----------------------------------------------------------------------------------


def web_job(**changes: object) -> Job:
    base = Job(
        id=str(uuid.uuid4()), state="requested", job_type="web_challenge", source_id=str(uuid.uuid4()),
        source_url="https://idealista.com/", platform="website", resolution_note="captcha on idealista.com",
        profile_id=str(uuid.uuid4()), profile_name="web-search-render", profile_state="ready", batch_id=None,
        target_url="https://www.idealista.com/inmueble/61000000/",
    )
    from dataclasses import replace

    return replace(base, **changes)


async def test_the_website_job_is_announced_in_russian_opened_on_the_challenged_page_and_solved_by_a_clean_page() -> None:
    store, notifier, live = MemoryVerificationStore(), FakeNotifier(), FakeLive()
    watchdog = FakeWatchdog(Recovery(False, kind="captcha"), Recovery(True))
    service = VerificationService(store, live, watchdog, notifier,
                                  FlowConfig(public_url=PUBLIC, operator_ids=frozenset({OWNER, OPERATOR}),
                                             owner_id=OWNER, bot_token=TOKEN))
    job = store.add_job(web_job())
    await service.tick()
    [_, text, button] = next(m for m in notifier.sent if m[0] == OPERATOR)
    assert "Сайт idealista.com просит пройти проверку. Откройте браузер, пройдите её и нажмите «Готово»." in text
    assert button is not None and button[0] == "Открыть проверку"
    session = (await service.open(token_of(button[1]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.view(session)
    assert live.started == [f"{job.profile_id}@https://www.idealista.com/inmueble/61000000/"]  # the challenged page
    assert await service.solve(session) is False  # the page still shows a challenge: not solved
    assert store.jobs[job.id].state == "active"
    assert await service.solve(session) is True  # a clean page marks it solved ...
    assert store.jobs[job.id].state == "verified" and store.jobs[job.id].resumed_at is not None  # ... and it goes on
    assert "Проверка сайта idealista.com пройдена" in notifier.sent[-1][1]


async def test_an_expired_website_job_tells_the_owner_in_russian() -> None:
    store, notifier = MemoryVerificationStore(), FakeNotifier()
    service = VerificationService(store, FakeLive(), FakeWatchdog(), notifier,
                                  FlowConfig(public_url=PUBLIC, operator_ids=frozenset({OWNER}), owner_id=OWNER,
                                             bot_token=TOKEN))
    job = store.add_job(web_job(expires_at=datetime.now(UTC) - timedelta(minutes=1), notified_at=datetime.now(UTC)))
    await service.tick()
    assert store.jobs[job.id].state == "expired"
    assert any("Проверку сайта idealista.com никто не прошёл" in m[1] for m in notifier.sent)


# --- settings ------------------------------------------------------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_has_gentle_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WEB_SEARCH_HUMAN_VERIFICATION", "WEB_SEARCH_VERIFIED_HOST_INTERVAL_SECONDS",
                 "WEB_SEARCH_PAGES_PER_VERIFICATION"):
        monkeypatch.delenv(name, raising=False)
    config = WebSearchSettings(_env_file=None).config()
    assert (config.human_verification, config.verified_host_interval_seconds, config.pages_per_verification) == (False, 8, 40)
    monkeypatch.setenv("WEB_SEARCH_HUMAN_VERIFICATION", "on")
    assert WebSearchSettings(_env_file=None).config().human_verification is True
    monkeypatch.setenv("WEB_SEARCH_RENDER_ENABLED", "false")  # no browser: nothing to verify with
    assert WebSearchSettings(_env_file=None).config().human_verification is False


def test_a_queued_url_type_has_no_verification_fields() -> None:
    assert not hasattr(QueuedUrl("https://a.es/x", url_key("https://a.es/x"), "a.es", 0, "unknown"), "verification")


# --- review fixes ------------------------------------------------------------------------------------------------------------


async def test_an_open_job_carries_an_expiry_from_the_configured_hours() -> None:
    store = MemoryWebStore(now=Clock(), job_hours=6)
    await store.open_verification("idealista.com", "captcha", IDEALISTA[0])
    [job] = store.verification_jobs
    assert job["expires_at"] == job["requested_at"] + timedelta(hours=6)


async def test_the_status_lists_no_waiting_sites_when_human_verification_is_off() -> None:
    worker, store, cid, _ = await make(IDEALISTA[:2], fetcher=refused(*IDEALISTA), renderer=SiteRenderer({"idealista.com"}))
    await ticks(worker, 6)
    assert (await store.web_status(cid)).verification == ("idealista.com",)
    store.human_verification = False
    assert (await store.web_status(cid)).verification == ()


async def test_a_finished_stage_cancels_the_jobs_of_sites_no_running_campaign_waits_for() -> None:
    cancelled: list[tuple[str, str]] = []

    async def cancel(job_id: str, actor: str) -> bool:
        cancelled.append((job_id, actor))
        return True

    fetcher, renderer = refused(*IDEALISTA), SiteRenderer({"idealista.com"})
    worker, store, cid, _ = await make(IDEALISTA[:2], fetcher=fetcher, renderer=renderer)
    worker.cancel_job = cancel
    await ticks(worker, 6)
    [job] = store.verification_jobs
    assert cancelled == []  # the campaign still has the site queued
    await worker._done(await worker.campaigns.get(cid), "time_cap")
    assert cancelled == [(job["id"], "web_search")]


async def test_a_website_job_of_another_type_is_not_a_web_challenge() -> None:
    store, notifier = MemoryVerificationStore(), FakeNotifier()
    service = VerificationService(store, FakeLive(), FakeWatchdog(), notifier,
                                  FlowConfig(public_url=PUBLIC, operator_ids=frozenset({OWNER, OPERATOR}),
                                             owner_id=OWNER, bot_token=TOKEN))
    job = store.add_job(web_job(job_type="manual_source_review", target_url=None))
    await service.tick()
    [_, text, _button] = next(m for m in notifier.sent if m[0] == OPERATOR)
    assert "Сайт" not in text  # the ordinary wording, not the website one
    button = next(m for m in notifier.sent if m[0] == OPERATOR)[2]
    session = (await service.open(token_of(button[1]), init_data(OPERATOR))).session
    await service.claim(session)
    await service.view(session)
    assert await service.solve(session) is True
    assert store.jobs[job.id].state == "verified" and store.jobs[job.id].resumed_at is None  # not resumed on its own
