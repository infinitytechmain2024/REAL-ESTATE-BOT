# REAL-ESTATE-BOT — delivery plan

Written after a full read of the merged tree (`claude/nifty-bardeen-ofvkos`), including
the Facebook stack. Every "verified" claim below was checked by running something, not
by reading. Every "unverified" claim is code that exists and has never met reality.

This plan supersedes nothing: `DELIVERY-PLAN.md` and `IMPLEMENTATION-PLAN.md` (written
elsewhere, not yet in this repo) still hold for scope and client commitments. This is the
engineering sequence to get there, ordered so that the cheapest way to be wrong comes first.

---

## 1. What the bot is for

A Telegram bot that finds Spanish real-estate opportunities and the people behind them,
and reports them to a user in chat.

**Two modes, chosen by button:**

| Mode | Question it answers | Sources |
|---|---|---|
| `LAND` | *What property matches my criteria and budget?* | Web search, listing portals, Facebook group posts |
| `INVESTORS` | *Who are the investors/agencies/developers active here?* | Same searches, but reporting **commenters and authors** rather than listings |

**Core behaviours:**

- Natural-language or voice request in → structured query (location, budget, area, type).
- Async execution with live progress edits, not one blocking reply.
- Results deduplicated per user, so a repeat search never resends the same thing.
- When nothing matches the budget, offer near misses with the real arithmetic
  ("at +45 000 EUR these exist") rather than an empty answer.
- Facebook groups read through one shared, operator-controlled browser session —
  never per end-user.
- When Facebook needs a human, the operator gets one Telegram message with one button
  that opens a live view of that browser, and one message when it recovers.

**Constraints that shape every decision:**

- No paid subscriptions; pay-per-use or self-hosted only.
- ~$30–60/month all-in (host + egress + model usage).
- Open to any Telegram user, but Facebook work serialises through one browser.
- Spain-only source list in v1, supplied by the operator, never auto-discovered.

**Hard non-goals — not "later", not at all:**

- No CAPTCHA-solving services, no 2FA bypass, no automated defeat of Facebook's
  human-verification. A checkpoint is resolved by a human, every time.
- No anti-detection / "human movement" evasion tooling.
- No auto-posting comments at people without explicit per-comment human approval.
- No storing of the Facebook password when the human-login path is the primary one.

---

## 2. Where the project actually stands

### Verified working

Checked by execution in this session:

- Full pipeline: query extraction → SearXNG → page fetch → LLM rank → Supabase persist.
- Budget near-miss handling (`bot/services/budget.py`), currency-safe by refusing to
  compare across currencies rather than applying a stale rate.
- Browser fallback for portals that block plain HTTP (`bot/services/parser/routing.py`),
  with a start-up preflight so a missing `playwright install` fails at boot, not mid-search.
- 61 modules import cleanly; ruff clean; all three requirement sets resolve together;
  `.env.example` validates through the real `Settings`.

### Written, never run against reality

The entire Facebook stack. It is coherent, well-documented code with real design
judgement in it — and not one line has met a live Facebook session:

- `bot/services/facebook/browser.py` — session, login-state machine, one automatic login attempt
- `bot/services/facebook/groups.py` — access classification, post extraction
- `bot/services/facebook/gate.py` + `tokens.py` — token-gated live view
- `bot/handlers/facebook_admin.py` — `/facebook`, recovery watcher
- `scripts/facebook_probe.py` — the validation gate itself

### Not built

Comment and nested-reply reading · commenter→profile matching · group join with screening
questions · pending-group recheck · canonical post links · draft-approve-post workflow ·
group activity sampling · proactive session alerts · any automated test suite.

---

## 3. Stages

Ordered by *cost of being wrong*. Stage 0 exists because without it Stage 1 produces
misleading results, and Stage 1 gates everything after it.

### Stage 0 — Unblock the probe · ~half a day · **DONE except 0.4 (operator)**

Three defects found in review that will make the first real validation run fail in ways
that look like Facebook's fault rather than ours.

| # | Task | Why it blocks | Done when |
|---|---|---|---|
| 0.1 | ✅ Split `check_state()` into a passive `observe_state()` (reads `page.url` + current DOM, never navigates) and a navigating `probe_state()`. Point the recovery watcher at the passive one. | `browser.py:205` calls `page.goto(FACEBOOK_HOME)`; `facebook_admin.py:50` polls it every 10s. In CDP mode that is the *same tab* the admin sees — the login form resets every 10 seconds for 15 minutes. Completing a login is impossible. | A watcher tick during a simulated human login leaves the page untouched; a pre-job check still navigates. |
| 0.2 | ✅ Hold `FacebookSession.lock` in the admin path. | `client.py:47` takes the lock; `facebook_admin.py:127,205,221` and the watcher do not. A status check during a group read navigates the job's page away. | Two concurrent callers serialise; a test proves the second waits. |
| 0.3 | ✅ Reorder challenge/access detection: URL path → DOM structure → localized text last. Add a Spanish string table beside the English one. | Six English-only matchers in `groups.py`; `locale="en-US"` only applies in local-dev launch mode, and Facebook renders in the *account's* language regardless. A Spanish inline challenge currently classifies as `HEALTHY` — jobs keep running against a challenge page and nobody is alerted. | A Spanish checkpoint fixture returns `HUMAN_REQUIRED`; a Spanish group page classifies correctly. |
| 0.4 | ⬜ **Operator:** set the Facebook account's own language to English in its settings. | Free, removes most of 0.3's risk surface immediately. | Operator confirms. |

**Skills:** write the fixtures first (`pytest` + fake `Page` objects) — this is exactly
where test-first pays, because the alternative is discovering it against a live account.
Run `/code-review` on the diff before it lands.

---

### Stage 1 — First contact · **the gate everything waits on**

Nothing after this stage means anything until this passes.

| # | Task | Done when |
|---|---|---|
| 1.1 | Run `scripts/facebook_probe.py` against one real, accessible group with a real logged-in session — locally on the Mac, with Chrome started under `--remote-debugging-port=9222`. No Docker, no Xvfb, no noVNC: the Mac has a screen. | The probe reports a real access classification and returns real post text. |
| 1.2 | Record every selector that missed, with a saved copy of the DOM around it. | A written list, not a memory. |
| 1.3 | Repair selectors against observed markup; prefer role/text/placeholder locators over class names. | Probe returns ≥5 posts from a real group with text and author populated. |
| 1.4 | Re-run twice more, once after a browser restart, to confirm the persistent profile keeps the session. | Three consecutive clean runs. |

**Skills:** the `run` skill to drive the app; **ultrathink before rewriting any selector
strategy** — a second failed approach costs another live session. Do *not* parallelise
this across subagents: there is one browser and one account, and concurrency here is the
thing most likely to get the account flagged.

**Expected outcome:** several selectors will be wrong. That is the point of the stage, not
a failure of it.

---

### Stage 2 — Make failure visible · ~1 day

Today, a dead Facebook session produces a normal-looking answer with no Facebook results
and no alert to anyone. That is the one behaviour the plan's own honesty rules forbid.

| # | Task | Done when |
|---|---|---|
| 2.1 | Session watchdog: observe state independently, transition the state machine, emit **exactly one** alert per incident and one on recovery (spec §112). | A fixture that flips the session to unhealthy produces one alert, not two, and not zero. |
| 2.2 | Surface `source_failure` vs `no_matches` to the *user*, not just the log. "Facebook is unavailable right now" ≠ "nothing matched". | Both paths produce distinguishable user-facing text. |
| 2.3 | Persist incident state to disk so a bot restart mid-incident doesn't re-alert (spec §188). | Restart during an incident sends no duplicate. |
| 2.4 | Freeze jobs while not `HEALTHY`; assert state at job start and abort cleanly if it flips mid-job (spec §114). | A job started against a healthy session and flipped mid-run aborts without partial writes. |

**Skills:** `pytest` with async fakes; `/code-review` at medium.

---

### Stage 3 — Harden the gate · ~half a day · **before any tunnel is opened**

The gate is currently loopback-only, which is why these are not yet urgent. They become
urgent the moment a Cloudflare/Tailscale tunnel points at it.

| # | Task | Finding |
|---|---|---|
| 3.1 | Bind PIN verification to a per-client cookie, not to the token. | `gate.py:119` — `pin_verified` is a set of *tokens*, so once the admin enters the PIN, anyone holding that link skips it. |
| 3.2 | Prune the rate-limiter and stop `is_blocked` inserting an entry per IP queried. | `gate.py:68` — unbounded `defaultdict` growth under scanning. |
| 3.3 | Constant-time PIN comparison. | `gate.py:118` uses `==` while the token correctly uses `compare_digest`. |
| 3.4 | `chmod 600` the token file. | `tokens.py:107` writes with default umask; the file is a live bearer token for a logged-in browser. |
| 3.5 | Make the PIN **mandatory**, not optional, once a public tunnel exists. | That link grants control of a logged-in Facebook account; today only phone possession stands in front of it. |
| 3.6 | Extend the "not reachable publicly" acceptance test to CDP `:9222`, not just noVNC `:6080`. | The DevTools protocol has **no authentication whatsoever**; reaching it means owning the session and its cookies. |

**Skills:** `/security-review` on the gate module specifically. This is the one place in
the codebase where a mistake is remotely exploitable.

---

### Stage 4 — Group capabilities · ~2–3 days · the real product work

Only after Stage 1 proves the foundation. Ordered easiest-first so momentum is real.

| # | Task | Difficulty | Notes |
|---|---|---|---|
| 4.1 | Canonical post link: use the `href` first, validate its shape, fall back to the `···` → "Copy link" menu only when it looks like a redirect/short link. | Medium | Avoids paying an extra click per post; clipboard reads are unreliable in containerised Chrome, so href-first also survives the move to the VM. |
| 4.2 | Pending-group recheck (scheduled re-run of the existing access check). | Low | Builds on code that already exists. |
| 4.3 | Group activity sampling (post frequency, to prioritise which groups to open). | Low-Medium | Builds on in-group search. |
| 4.4 | Join a group: click Join, answer screening questions (operator-supplied or LLM-drafted **with approval**), track `PENDING`. | Medium | End-to-end acceptance needs a real admin to approve — **not on our timeline**; do not schedule the acceptance test as a blocker. |
| 4.5 | **Comments and nested replies.** | **High** | The hardest remaining piece. Sort order changes, replies paginate behind "View more", and structure varies by post and account state. |
| 4.6 | Commenter → profile URL matching. | High | Bundled with 4.5; this is what `INVESTORS` mode actually needs. |
| 4.7 | Draft → admin approves in Telegram → post. | Medium-High | Typing is easy; *verifying it posted* (vs. an ambiguous timeout) and preventing duplicates is the real work. **Never auto-retry an ambiguous submission.** |

**Skills:** **ultrathink on 4.5 before writing code** — design the pagination/expansion
strategy against saved real DOM from Stage 1, not from imagination. Use an `Explore`
subagent to sweep the saved DOM snapshots for structural patterns; do **not** use subagents
to drive the live browser. TDD against saved fixtures throughout.

---

### Stage 5 — Portals · ~1 day

| # | Task | Done when |
|---|---|---|
| 5.1 | Pick **3–5** portals, not 20. Operator supplies the list. | List agreed in writing. |
| 5.2 | Add blocked domains to `PARSER_BROWSER_DOMAINS` so they skip the doomed HTTP attempt. | Each portal returns extracted text, not a 403. |
| 5.3 | Validate extraction quality per portal (price, area, location actually present). | Hand-check 10 listings per portal. |

**Skills:** `/code-review`; the existing routing behaviour test as the pattern to extend.

---

### Stage 6 — Deployment · ~1 day

Decision taken: **old home computer + Tailscale**, not a rented VPS. With 2FA switched
off, a familiar residential IP is one of the few signals left keeping the account
un-challenged — and a datacenter IP discards it at exactly the wrong moment. It also
removes the residential-proxy line item and the budget pressure of a GUI-capable VM.

| # | Task | Done when |
|---|---|---|
| 6.1 | Compose/systemd for Xvfb + system Chrome + x11vnc + websockify + gate + bot, restarting together. | Machine reboots and everything comes back. |
| 6.2 | CDP bound to loopback only. If bot and Chrome are separate containers, put them in one network namespace rather than widening the bind. | `:9222` unreachable from another host. |
| 6.3 | Tailscale. Prefer **tailnet-private** over Funnel if the admin's phone has the app — no public exposure at all. | Admin opens the Telegram button from a phone abroad and sees the browser. |
| 6.4 | Drop `FACEBOOK_EMAIL` / `FACEBOOK_PASSWORD` from the deployment. | Human login is the primary path; not storing them deletes a whole risk class *and* the auto-login branch. |
| 6.5 | Keep the portal fetcher's browser separate from the Facebook Chrome. | Routing portal fetches through the logged-in profile would put your Facebook identity behind every Idealista request. |

---

### Stage 7 — Compliance, explicitly · ongoing, decide before posting

Not a blocker to building; a blocker to *auto-posting*.

- The system profiles identifiable EU residents from group comments and classifies them
  as leads. Under GDPR that is automated processing for what is functionally direct
  marketing. It needs a stated lawful basis, and the decision belongs to the client, in
  writing — not arrived at by omission in a technical plan.
- Vision-model fallback (if built) transmits screenshots of group members to a third-party
  model provider. That is a separate transfer question from keeping text in your own DB.
- Scraping and auto-commenting is a Facebook ToS violation. The account risk is accepted;
  the legal exposure is the client's and should be acknowledged rather than assumed away.
- **Default posture: draft-only, per-comment approval, no bulk approve.** This is also the
  better engineering default, matching the existing rule against auto-retrying an
  ambiguous submission.

---

## 4. Testing — currently zero

There was no test framework in this repo at all until Stage 0. There are now 34 tests
covering session classification, the recovery watcher's invariants, group access in both
languages, and fetcher routing -- run with `make test`. Everything below T.2 is still
outstanding, and the gap that matters most is fixtures taken from real markup (T.3),
which Stage 1 is what produces.

| # | Task | Stage |
|---|---|---|
| T.1 | ✅ Added `pytest` + `pytest-asyncio`, a `tests/` tree and `make test`. | 0 |
| T.2 | ✅ Ported the fetcher-routing test into it. | 0 |
| T.3 | Save real DOM snapshots from Stage 1 as fixtures. | 1 |
| T.4 | Every selector gets a fixture test; a Facebook markup change must fail a test, not a user's search. | 4 |
| T.5 | State-machine tests: exactly-one-alert, timeout, restart-mid-incident, job-abort-on-flip. | 2 |
| T.6 | Gate tests: expired token, wrong PIN, rate limit, no public bind. | 3 |

**Rule:** anything that touches Facebook markup is written test-first against a saved
fixture. The live account is for discovering reality, not for regression testing.

---

## 5. Which skills to use where

| Skill / tool | Use it for | Do **not** use it for |
|---|---|---|
| **ultrathink** | Stage 4.5 comment/reply extraction design; any selector-strategy rewrite after a failed probe. | Routine edits — it slows the loop without improving it. |
| **`Explore` subagent** | Sweeping saved DOM snapshots for structural patterns; finding every call site before a refactor. | Driving the live browser. One browser, one account — concurrency here is what gets accounts flagged. |
| **`Plan` subagent** | Sequencing Stage 4 once Stage 1 has told us what the markup really looks like. | Anything before Stage 1 — planning against imagined markup is what produced the current unverified stack. |
| **`/code-review`** | Every diff before it lands. Medium by default; high for Stage 4. | A substitute for tests. |
| **`/security-review`** | Stage 3, the gate module. The only remotely exploitable surface. | Stages with no network exposure. |
| **`run` skill** | Stage 1 and any "does it actually work" moment. | — |
| **`/loop`** | Watching a long probe or a CI run. | Polling for something that sends a notification anyway. |
| **pytest (TDD)** | Everything touching Facebook markup, the state machine, and the gate. | — |

**Parallelism rule for this project:** fan out freely on *reading* (code search, DOM
analysis, fixture writing). Never fan out on *acting* — one browser, one Facebook session,
one job at a time. The single-owner rule in the spec is an invariant, not a style choice.

---

## 6. Schedule reality

The 2-day-demo / 4-day-full commitment does not fit the scope as written, and saying so
now is cheaper than shedding scope silently under pressure later.

- Stage 0+1 is the whole first day, and Stage 1 can force a selector rewrite.
- Stage 4.5 alone (comments and replies) is a day to a day and a half.
- Stage 4.4's acceptance test depends on a **real group admin approving a join request** —
  on nobody's timeline but theirs. Do not schedule it as a gate.

**Recommended trim, to take to the client:** 3–5 portals instead of 20; auto-commenting
deferred past day 4 and kept draft-only; Facebook group *reading* as the day-4 deliverable,
with joining and commenting as the following increment.

---

## 7. Open items on the operator's side

1. The Spain portal list (3–5) and the Facebook group URLs — groups you are already a
   member of, for the first pass.
2. Facebook account language set to English (Stage 0.4).
3. One real, accessible group to run Stage 1 against.
4. A written decision on Stage 7's lawful basis before any comment is posted.

---

## 8. Decisions already taken

| Decision | Rationale |
|---|---|
| Extend the existing SearXNG bot rather than start a Playwright-first codebase | Keeps a working pipeline, modes, dedupe, progress UX and ranking. |
| Home machine + Tailscale over rented VPS | Residential IP; human near the keyboard; fits budget. |
| One shared admin Facebook session, not an account pool | A pool multiplies unvalidated surface; revisit only after one account survives contact. |
| Two separate browsers | Facebook = persistent profile over CDP, single owner. Portals = throwaway, profile-less, concurrent. Never mix. |
| 2FA off on a dedicated bot account | Removes the code-entry branch; buys nothing against checkpoints, which is fine because the human-takeover path handles those. |
| Playwright owned in-house, no OpenCLI | No daemon, no extension, no third-party release cycle in the core of the product. |
| No CAPTCHA/2FA/anti-detect tooling, ever | Stated non-goal; the human-takeover flow is the answer. |
