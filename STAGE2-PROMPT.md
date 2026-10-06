# Stage 2 — execution prompt

> **Note:** `bot/main.py` referred to below has moved to `legacy/bot/main.py` (stage 6.1); this brief is historical.

Paste the block below into a fresh Claude Code session opened on this repository.
It is written to stand alone: it names the branch, the spec to read, the invariants,
and the commands that decide whether the work is done.

The detailed spec it refers to is `STAGE2.md`. The surrounding plan is `PLAN.md`.

---

```
## Objective

Implement Stage 2 of this repository: make a broken Facebook session visible to both
the operator and the user. Today it is invisible to both — `FacebookSource.search()`
logs a warning and returns `[]`, which the pipeline cannot distinguish from "nothing
matched", and nothing watches the session unless an admin taps a button first.

## Context

- Repo: REAL-ESTATE-BOT. Branch: `claude/nifty-bardeen-ofvkos`. Python 3.11, aiogram,
  pydantic-settings, Playwright, optional Supabase.
- READ FIRST, before writing any code: `STAGE2.md` (the full spec — tasks 2.1 to 2.4,
  acceptance criteria, and the test-harness traps) and section 3 of `PLAN.md`.
- Stage 0 is done: `observe_state()` (passive) and `probe_state()` (navigates) are split,
  the session lock is held on every page read, and detection handles English and Spanish.
- Stage 1 (the live Facebook probe run) is NOT done and is NOT a prerequisite. Nothing in
  Stage 2 touches a real Facebook session.
- The `facebook_session_incidents` table already exists in
  `bot/services/db/migrations/002_facebook.sql` and nothing writes to it yet. Use it;
  do not design a new one.
- 34 tests exist and pass. Fakes live in `tests/conftest.py` — extend them, do not write
  parallel ones.

## Target State

1. A watchdog started from `bot/main.py::build_services` observes the session on an
   interval and sends the operator exactly one Telegram alert per incident, plus exactly
   one on recovery, without being asked first.
2. `PipelineOutcome` carries `failed_sources`, and `bot/handlers/search.py` produces
   different user-facing text for "a source was unavailable" than for "nothing matched".
3. An open incident survives a bot restart without re-alerting, via the existing
   incidents table, and still works when Supabase is not configured.
4. A job aborts cleanly if the session state flips mid-run, with no partial writes.

## Method — test first, without exception

For each of the four tasks:
1. Write the test before the implementation. Run it. SHOW that it fails for the right
   reason — a test that has never failed has proven nothing.
2. Implement the smallest change that makes it pass.
3. Re-run the whole suite, not just the new test.

## Scope

- Work in: `bot/services/facebook/`, `bot/handlers/facebook_admin.py`,
  `bot/handlers/search.py`, `bot/services/pipeline.py`, `bot/services/db/supabase_repo.py`,
  `bot/main.py`, `bot/config.py`, `tests/`.
- Do NOT touch: `searxng/` (vendored upstream), `.env`, `bot/services/facebook/gate.py`
  and `tokens.py` (that is Stage 3), `bot/services/parser/`, the existing migration files.
- Do NOT wire `FacebookSource` into `ResearchPipeline` — Stage 1 must prove group reading
  first. Design task 2.2 so it works the moment it is wired, and test it against a fake.

## Invariants — breaking any of these is a regression, not a trade-off

- Observing the session MUST NOT navigate. A human may be mid-login in that browser; in
  CDP-attach mode it is the same tab they are looking at.
- Every page read MUST hold `FacebookSession.lock`.
- The watchdog MUST NOT start the browser. If no context is live, skip the tick.
- Exactly one alert per incident. One on recovery. Never both for the same incident.
- An unrecognised page state is `HUMAN_REQUIRED`. Never guess `HEALTHY`.
- A failed read is never reported as an empty or inactive group.
- Facebook alerting MUST work when Supabase is unconfigured — it is optional in this project.
- Do NOT write anything that defeats CAPTCHA, 2FA, or bot detection. A checkpoint is
  resolved by a human. This is a project non-goal, not a backlog item.

## Traps that have already cost time here

- `tests/__init__.py` must stay. The vendored SearXNG ships its own `tests/` on the same
  `PYTHONPATH` and otherwise wins the name.
- NEVER set a poll interval to `0` in a test. The watchers count elapsed time by adding
  the interval, so `0` never reaches the timeout and the suite hangs forever instead of
  failing. Use `0.01` with a `0.05` timeout.
- If the suite hangs, get a stack with `-o faulthandler_timeout=8` before forming a
  theory. A sample taken inside an infinite loop points at whatever that loop was doing.
- `Settings()` requires `TELEGRAM_TOKEN`; an autouse fixture in `tests/conftest.py`
  supplies a placeholder.
- User-facing strings in this codebase are Russian. Match the surrounding copy.

## Acceptance Criteria

- [ ] A fake session flipping HEALTHY → LOGIN_NEEDED → LOGIN_NEEDED → HEALTHY produces
      exactly two messages, in that order, and never navigates.
- [ ] With a fake repo reporting an open incident at startup, an unhealthy session
      produces NO message; with no open incident, exactly one.
- [ ] A search returning zero results with a failed Facebook source produces different
      user-facing text than a search returning zero results with a working one.
- [ ] A session that turns unhealthy after the first group returns the first group's
      hits, marks `failed_sources`, and does not read the second group.
- [ ] `make test` passes, including all 34 pre-existing tests.
- [ ] `ruff check bot/ scripts/ tests/` is clean.
- [ ] `make check-imports check-config` is clean.
- [ ] `PLAN.md` Stage 2 rows ticked.

## Action Boundaries

- Proceed without asking on: reading any file, writing tests, editing files in scope,
  running the test suite and linters, committing to the current branch.
- STOP and ask before: adding any dependency, changing or adding a database migration,
  deleting any file, editing anything outside the scope list, or pushing to a branch
  other than `claude/nifty-bardeen-ofvkos`.
- Do not run `scripts/facebook_probe.py` or launch a real browser. That is Stage 1 and
  needs a human with a real Facebook session.

## Progress Evidence

Ground every completion claim in a command output. "Tests pass" means paste the pytest
summary line. If something is skipped or left incomplete, say so explicitly rather than
reporting the stage as done. Report which of the four tasks are finished, and end with
the output of the four verification commands above.

## Finish

Commit in logical units with messages that explain the reasoning, not just the contents —
match the existing commit style in this repo. Push to
`claude/nifty-bardeen-ofvkos`. Only make the changes described above; do not add
features, abstractions, or refactors beyond them.
```

---

## Notes on using it

- Open the session **on this repository** so the paths resolve.
- Run `make install` first if the venv does not exist; the prompt assumes `make test` works.
- If the agent asks about the empty-`FACEBOOK_ADMIN_TELEGRAM_IDS` question raised in
  `STAGE2.md` §8, the recommended answer is: a start-up warning, not a hard failure.
