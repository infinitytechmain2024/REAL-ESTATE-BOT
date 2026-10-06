# Stage 2 — Make failure visible

> **Note:** `bot/main.py` referred to below has moved to `legacy/bot/main.py` (stage 6.1); this brief is historical.

**Self-contained brief.** Written so a session with no memory of the previous conversation
can execute it. Everything you need is here or in the repo; nothing depends on chat history.

- **Branch:** `claude/nifty-bardeen-ofvkos`
- **Prerequisite:** Stage 0 is done (commit `195bf8f`). Stage 1 (the live probe run) is
  **not** done and **is not a blocker for this stage** — nothing here touches real Facebook.
- **Overall plan:** `PLAN.md` in this repo. This file expands section 3, Stage 2.

Start by confirming the tree is healthy:

```bash
make install          # python3.11 venv, incl. requirements-dev.txt
make test             # expect 34 passing
ruff check bot/ scripts/ tests/
```

---

## 1. The problem this stage fixes

**Today a broken Facebook session is invisible to everyone.**

Trace it in the code:

1. `bot/services/facebook/client.py` — `FacebookSource.search()` probes the session; if it
   is not `HEALTHY` it logs `facebook.source.session_not_healthy` and **returns `[]`**.
2. The pipeline treats an empty source as a source with nothing to say.
3. The user gets a normal-looking answer with no Facebook results and no indication that a
   source was down.
4. The operator is never told. The only alerting that exists
   (`bot/handlers/facebook_admin.py::_watch_for_recovery`) starts **only when the admin
   taps "Войти" themselves** — nothing watches the session on its own.

So the system's answer to "Facebook is logged out" is silence in both directions: the user
thinks nothing matched, the operator thinks everything is fine.

That is precisely the `source_failure` vs `no_matches` distinction the project's own design
rules insist on — `GroupAccess.UNKNOWN_ERROR` exists specifically so a failed read is never
reported as an empty or inactive group. Stage 2 applies the same honesty one level up.

**Definition of done for the stage:** a dead session produces (a) exactly one Telegram alert
to the operator, unprompted, and (b) user-facing text that distinguishes "a source was
unavailable" from "nothing matched" — and neither duplicates across a bot restart.

---

## 2. What already exists — build on it, do not rebuild it

| Thing | Where | State |
|---|---|---|
| Session states `HEALTHY` / `LOGIN_NEEDED` / `AUTO_LOGIN_ATTEMPT` / `HUMAN_REQUIRED` | `bot/services/facebook/browser.py::SessionState` | Done |
| `observe_state()` — passive, never navigates | `browser.py` | Done (Stage 0) |
| `probe_state()` — navigates, for between jobs | `browser.py` | Done (Stage 0) |
| `FacebookSession.lock` — the one-owner-of-the-browser invariant | `browser.py` | Done |
| Recovery watcher, exactly-one-message semantics, token invalidation | `bot/handlers/facebook_admin.py::_watch_for_recovery` | Done, but **only admin-triggered** |
| Token-gated live view + Telegram button | `bot/services/facebook/gate.py`, `tokens.py`, `_open_button()` | Done |
| `facebook_session_incidents` table — append-only, `resolved_at IS NULL` = open | `bot/services/db/migrations/002_facebook.sql:33` | **Schema exists, nothing writes to it** |
| `PipelineOutcome` — what the handler reports on | `bot/services/pipeline.py` | Done; needs a new field (task 2.2) |
| Test suite + fakes | `tests/` | 34 tests; `tests/conftest.py` has `FakePage`/`FakeContext` |

Note `FacebookSource` is **not yet wired into `ResearchPipeline`** — see the docstring in
`client.py`. Stage 2 does not wire it in either (that waits for Stage 1). Design 2.2 so it
works the moment it *is* wired, and test it against a fake source.

---

## 3. Tasks

### 2.1 — A watchdog that notices without being asked

**Build:** a background task, started in `bot/main.py::build_services` when
`settings.facebook.enabled`, that periodically calls `observe_state()` **under the session
lock** and drives the incident lifecycle.

Design constraints, all of which have bitten already:

- **`observe_state()`, never `probe_state()`.** The watchdog must not navigate. A human may
  be mid-login in that browser; in CDP-attach mode it is the same tab they are looking at.
- **Take `FacebookSession.lock`** around every read. A group job may be driving the page.
- **Do not start the browser.** If the session was never started, the watchdog stays idle —
  launching a browser window because a timer fired is not acceptable. Check
  `FacebookSession` has a live context before observing; skip the tick otherwise.
- **Poll interval:** reuse a settings value (`FACEBOOK_WATCHDOG_INTERVAL_SECONDS`, default
  60s) rather than a literal. Much slower than the 10s recovery watcher — this one runs for
  the process lifetime, not for one incident.
- **Exactly one alert per incident.** Transitioning `HEALTHY → not HEALTHY` opens an
  incident and sends one message. Staying unhealthy sends nothing more. Recovering closes
  the incident and sends one message.

**Reuse** `_open_button()` so the alert carries the same live-view button as the manual
path, and reuse `_watch_for_recovery`'s message copy. Consider moving both into a small
`bot/services/facebook/alerts.py` so the watchdog and the admin handler share one
implementation rather than drifting apart.

**Acceptance:** a fake session flipping `HEALTHY → LOGIN_NEEDED → LOGIN_NEEDED → HEALTHY`
produces exactly two messages, in that order, and never navigates.

---

### 2.2 — Tell the user a source failed, not that nothing matched

**Build:** carry source failures through the pipeline to the user.

1. Add to `PipelineOutcome` (in `bot/services/pipeline.py`):
   ```python
   failed_sources: list[str] = Field(default_factory=list)
   """Sources that could not be consulted at all, e.g. ["facebook"]. Distinct from a
   source that was consulted and returned nothing."""
   ```
2. `FacebookSource.search()` must make the difference expressible. Returning `[]` cannot
   mean two things. Either return a small result object (`hits` + `failed: bool`), or raise
   a dedicated `SourceUnavailable` the caller catches. Prefer the explicit return type —
   the pipeline's existing style is to degrade, not to raise.
3. `bot/handlers/search.py` — when `failed_sources` is non-empty, say so. Russian, matching
   the surrounding copy. Two distinct user-facing outcomes:
   - results found, a source failed → append a line noting Facebook was unavailable
   - **no** results **and** a source failed → this must not read like "nothing matched".
     Say the source was unavailable and suggest retrying.

**Acceptance:** with a fake Facebook source that fails, a search returning zero results
produces text that mentions unavailability; with a fake source that returns nothing (but
works), the text is the ordinary "nothing found" message. Two different strings.

---

### 2.3 — Survive a restart without re-alerting

**Build:** persist the open incident so a bot restart mid-incident does not send a second
alert for the same one.

The schema is already there and documents this exact intent:

```sql
-- bot/services/db/migrations/002_facebook.sql:33
create table if not exists public.facebook_session_incidents (
    id uuid primary key default gen_random_uuid(),
    state text not null,
    detected_at timestamptz not null default now(),
    resolved_at timestamptz,
    notes text
);
```

Add repository methods alongside the existing ones in
`bot/services/db/supabase_repo.py` — follow the `_execute()` pattern there, which swallows
and logs failures rather than raising:

- `open_facebook_incident(state) -> UUID | None`
- `current_facebook_incident() -> row | None` (the newest with `resolved_at IS NULL`)
- `resolve_facebook_incident(id)`

**Supabase is optional in this project** (`settings.supabase.configured`). When it is not
configured the watchdog must still work — fall back to in-process state and accept that a
restart may re-alert. Do not make Facebook alerting depend on a database that may not
exist; say so in a docstring.

**Acceptance:** with a fake repo reporting an open incident at startup, the watchdog
observing an unhealthy session sends **no** message. With no open incident, it sends one.

---

### 2.4 — Freeze jobs when the session is not healthy

**Build:** jobs assert state at start and abort cleanly if it flips mid-run.

- `FacebookSource.search()` already probes before starting — keep that.
- Add a re-check between groups in its loop: if the state flips while iterating, stop,
  return what was gathered so far, and mark the source as failed (2.2's mechanism).
- **No partial writes.** If a job aborts, nothing half-extracted reaches the database.

**Acceptance:** a fake session that turns unhealthy after the first group yields the first
group's hits, marks `failed_sources`, and does not read the second group.

---

## 4. Invariants — breaking any of these is a regression

Tests exist for all of them; `make test` will catch you.

1. **Observing never navigates.** `tests/test_facebook_session.py::test_observe_state_never_navigates`
2. **The lock is held around every page read.** `tests/test_facebook_admin.py::test_watcher_holds_the_lock_while_reading`
3. **Exactly one alert per incident, one on recovery, never both.**
4. **Unknown layout → `HUMAN_REQUIRED`.** Never guess `HEALTHY`.
5. **A failed read is never reported as an empty or inactive group.**
6. **No CAPTCHA/2FA/anti-detect work.** A checkpoint is resolved by a human. This is a hard
   project non-goal, not a backlog item.

---

## 5. Test-harness notes that will save you an hour

These are all learned the hard way in Stage 0.

- **`tests/` is a real package** (`tests/__init__.py`) because the vendored SearXNG ships
  its own `searxng/tests/` on the same `PYTHONPATH`. Without it, `from tests.conftest import …`
  resolves into SearXNG's suite. Don't delete that file.
- **Never set a poll interval to `0` in a test.** The watchers count elapsed time by
  *adding* the interval, so `0` never reaches the timeout and the suite hangs forever
  instead of failing. Use `0.01` with a `0.05` timeout — see
  `tests/test_facebook_admin.py::_fast_watcher`.
- **If pytest hangs, get a stack** with `-o faulthandler_timeout=8` before theorising. A
  sample taken inside an infinite loop will point at whatever that loop was doing (last
  time: structlog) and send you after the wrong bug.
- **`Settings()` requires `TELEGRAM_TOKEN`.** `tests/conftest.py` sets a placeholder via an
  autouse fixture.
- **Fakes live in `tests/conftest.py`**: `FakePage` (records `goto_calls`, answers
  `selectors` / `texts` / `placeholders` by dict), `FakeContext` (cookies), `FakeLocator`.
  Extend these rather than writing new ones.
- **Module-level globals in `facebook_admin.py`** (`_watcher_task`, `_last_known_state`)
  persist between tests; reset them in a fixture.

---

## 6. Done means

```bash
make test                                # all green, new tests included
ruff check bot/ scripts/ tests/          # clean
make check-imports check-config          # clean
```

Plus: `PLAN.md` Stage 2 rows ticked, and a commit message that says what changed and why —
the repo's commits explain reasoning, not just contents.

Do **not** run the live probe as part of this stage; Stage 1 owns that, and it needs a human
with a real Facebook session.

---

## 7. Explicitly out of scope for Stage 2

Comment/reply reading · commenter→profile matching · group join flow · canonical post links ·
draft-approve-post · group activity sampling · gate hardening (that is Stage 3) · wiring
`FacebookSource` into `ResearchPipeline` (Stage 1 must prove group reading first).

---

## 8. Open question for the operator

The watchdog alerts `FACEBOOK_ADMIN_TELEGRAM_IDS`. If that list is empty, alerting is
silently impossible — decide whether an empty list should be a start-up warning (recommended)
or a hard failure when `FACEBOOK_ENABLED=true`.
