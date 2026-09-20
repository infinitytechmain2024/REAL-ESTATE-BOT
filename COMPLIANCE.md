# Personal data in this system — what it holds, and what is still undecided

**This is an engineering record, not legal advice.** I am not qualified to tell you what
your lawful basis is, and this document does not try to. What it does is state accurately
what the software collects, where it puts it, how long it keeps it and who it sends it to,
because a lawful-basis assessment cannot be made without those facts, and they are
currently written down nowhere.

The decisions in §4 belong to the client, in writing. `PLAN.md` §3 Stage 7 is the task
this implements.

---

## 1. Why this matters more than it looks

The product's stated purpose is to find real-estate opportunities **and the people behind
them**. In `INVESTORS` mode that is the whole point: the output is a list of identifiable
people, their profile links, something they wrote, and an AI judgement about their
intentions as investors.

Those people are mostly Spanish residents. They never interacted with the bot, never
consented to being profiled, and in most cases will never know it happened. That is
materially different from a search engine returning a listing, and it is what makes this
worth documenting rather than assuming.

---

## 2. What is stored today

This is already true of the search-only product, before any Facebook code runs.

### 2.1 People who use the bot

`users` — `telegram_id`, `username`, `first_name`, `language_code`, mode, timestamps.
Written on **every** incoming update by `UserMiddleware`. The bot is open to any Telegram
user, so this grows without an approval step.

### 2.2 People who did not use the bot

This is the part that tends to be overlooked:

| Column | What actually lands there |
|---|---|
| `results.raw` | The **entire** `StructuredResult` as JSON — including `contacts`, which is phone numbers, e-mail addresses and contact URLs the LLM pulled off the page |
| `results.content` | The **full extracted text** of the fetched page, which routinely contains names, phone numbers and addresses |
| `results.title` / `summary` | LLM-written text about the listing and, in investor mode, about a person |

So the database already holds contact details of third parties — estate agents, private
sellers — scraped from arbitrary pages. Nobody asked them.

### 2.3 Once Facebook group reading is switched on

Additionally: the **displayed author name** of a post (`SearchHit.author`), the **full text
of posts**, and — once Stage 4.5/4.6 are built — **commenter names, their profile URLs, and
their comment text**, which is the input to the investor-lead classification.

### 2.4 What the operator's machine holds

The Chrome profile in `./data` contains live Facebook session cookies. `DEPLOYMENT.md` §3
covers it; it is personal data too, of the account holder.

---

## 3. Where it goes

- **Supabase** (Postgres, hosted). Region is whatever the project was created in — **worth
  checking**; a US region makes every write an international transfer.
- **The LLM provider.** Page text and, in investor mode, people's posts and comments are
  sent to whichever model `LLM_PROVIDER` selects — OpenRouter routes onward to a provider
  you have not individually chosen. A locally-hosted model via `openai_compatible` is the
  one configuration where this text never leaves the machine.
- **Telegram.** Results are delivered as messages, so anything reported about a person
  passes through and is retained by Telegram.
- **A vision model, if the fallback in `PLAN.md` Stage 4 is ever built.** That would send
  *screenshots of group feeds* — faces, names, comments — to a third-party API. It is a
  different and larger transfer than sending extracted text, and should be decided
  separately rather than inherited from the decision about text.

---

## 4. Open decisions — the client's, in writing

These are not engineering choices and should not be made by default or by omission.

1. **Lawful basis for profiling non-users.** Building a lead list of identifiable people
   from their public comments, and classifying their investment intent, is automated
   processing for what functions as direct marketing. Under GDPR that needs a stated basis
   and, if legitimate interests is the answer, a documented balancing assessment.
2. **Whether to auto-post at those people at all.** See §5 — the engineering default is
   already the conservative one, but the decision is not ours.
3. **Transparency and data-subject rights.** People profiled here have rights of access
   and erasure that they cannot exercise, because they do not know the system exists.
   Someone has to decide what happens when one of them asks.
4. **Supabase region**, per §3.
5. **Whose exposure this is.** Scraping Facebook and auto-commenting breaches Meta's
   terms. The account-ban risk has been accepted explicitly and repeatedly — that part is
   settled. The regulatory exposure is a separate question with a different owner, and it
   has never been raised with the client.

---

## 5. What the software already does about it

Not everything here is open. Several decisions are already made and enforced in code:

- **No CAPTCHA-solving, no 2FA bypass, no anti-detection tooling.** A checkpoint is
  resolved by a human, every time. This is a hard non-goal, not a backlog item.
- **Nothing is auto-posted.** The draft-approve-post workflow (Stage 4.7) is unbuilt, and
  the plan specifies per-comment human approval with **no bulk-approve path** when it is.
- **No credentials on disk by default** — `DEPLOYMENT.md` §6.
- **A failed read is never reported as an absence.** `UNKNOWN_ERROR` stays distinct from
  "nothing found", so the system does not quietly invent conclusions about people or
  places it could not actually read.

---

## 6. The gap this document found — now closed

Writing §2 surfaced something nobody had noticed: **there was no retention limit and no
erasure path.** Nothing expired, and while `users` cascades to everything else, no code
ever deleted a user. Both are now implemented.

### Retention

`SUPABASE_RETENTION_DAYS`, **defaulting to 90**. A daily pass deletes `results` and then
`searches` older than the window — children first, so an interrupted purge leaves orphaned
parents rather than results whose search has vanished.

Setting it to `0` keeps everything. That is a deliberate opt-out rather than the default,
and the bot logs a start-up warning naming what is being kept and why it matters.

90 is a starting number, not an answer. **If the client has a view, change it** — it is one
line in `.env`.

### Erasure

`/forget` erases everything held about the user who asks. One delete is enough: searches,
results and feedback all reference `users` with `on delete cascade`.

Two properties of it are worth stating, because both are easy to get wrong:

- **It confirms first.** A single tap must not wipe a history that cannot be recovered.
- **A failed delete says so.** Telling someone their data is gone when it is not would be
  worse than not offering the command at all, so the repository returns whether the delete
  succeeded and the handler reports it honestly.

### What this does not cover

`/forget` serves **users of the bot**. It does nothing for the people described in §2.2 —
the ones whose contact details and comments are stored without their knowledge, who cannot
ask because they do not know the system exists. Retention now bounds how long their data
is held; nothing gives them access or erasure on request. That remains open decision §4.3,
and no amount of code closes it.

## 7. What would change the answers

Two configurations materially reduce the exposure described above, and both are already
supported:

- **A local LLM** (`LLM_PROVIDER=openai_compatible` pointed at Ollama) keeps every page,
  post and comment on the operator's own machine. No third-party processor for the text.
- **Running without Supabase.** The bot works without it — results are not persisted, and
  cross-session de-duplication is lost. That is a real feature trade, not a free win, but
  it is the difference between holding a database of profiled people and holding none.

Neither is a recommendation on its own. They are the levers that exist if the answer to §4
turns out to be restrictive.
