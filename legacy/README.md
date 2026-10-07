# legacy/ -- the retired standalone bot

This directory holds the first generation of the project: a single-process
Telegram bot that parsed a request with an LLM, searched through a vendored
SearXNG, fetched pages, ranked them with an LLM and stored results in Supabase.
The campaign stack (`docker-compose.yml`: control plane, campaign runner,
web search, social search, analysis pipeline, verification, collectors) replaced
it. **Nothing here is deployed, imported by a live service, linted, or collected
by pytest.** It is kept, with git history (everything was moved with `git mv`),
for reference and so it can be revived.

The layout mirrors the original repository paths under `legacy/`:

| Path | What it was |
|---|---|
| `bot/main.py`, `states.py`, `handlers/`, `keyboards/`, `middlewares/` | aiogram bot (polling/webhook), `/facebook` admin, voice |
| `bot/config.py`, `exceptions.py`, `logging_conf.py`, `models/`, `prompts/` | settings, models and prompts of that bot |
| `bot/services/pipeline.py`, `relevance.py`, `facts.py`, `budget.py`, `retention.py` | request -> search -> fetch -> rank pipeline |
| `bot/services/search/` | `SearXNGClient`, `QueryBuilder`, Google Maps source |
| `bot/services/parser/` | page fetch/extract (HTTP, Scrapling, browser fallback) |
| `bot/services/llm/`, `stt/` | provider registry for LLMs and speech-to-text |
| `bot/services/db/` | Supabase repository (the SQL migrations stay in `bot/services/db/migrations/`) |
| `bot/services/facebook/` | in-process Facebook group reader, noVNC gate, token store, watchdog, recheck, public-source discovery |
| `bot/utils/places.py`, `text.py` | helpers used only by the above (`bot/utils/urls.py` stays live) |
| `searxng/` | vendored SearXNG snapshot and its second settings file (the live stack uses the `searxng/searxng` image with `docker/searxng/settings.yml`) |
| `Dockerfile`, `docker/entrypoint.sh`, `render.yaml` | one-container image (SearXNG + bot + Xvfb/Chrome/noVNC) and its Render blueprint |
| `scripts/` | `maps_probe`, `pipeline_probe`, `nvidia_probe`, `portal_probe`, `facebook_probe`, `gate_probe`, `check_vendor`, `run_llm.sh` |
| `tests/` | the tests of all of the above, plus `test_deployment_legacy.py` |
| `requirements-legacy.txt` | dependencies only this code needed (`openai`, `anthropic`, `supabase`, `trafilatura`, ...) |

Still live and therefore **not** moved: `bot/services/facebook/activity.py`
(used by `bot/campaign/discovery.py`), `bot/utils/urls.py`, `bot/operators.py`,
`bot/telegram_webapp.py`, and the SQL migrations.

## Reviving it

`git mv` the needed paths back to the repository root (`legacy/bot/x` -> `bot/x`,
`legacy/searxng` -> `searxng`, ...), `pip install -r requirements-legacy.txt`,
restore the old Makefile targets from history (`git log -- Makefile`), and run
`legacy/tests` with `pytest legacy/tests` once the code is back in place. The
legacy environment variables are listed at the end of `.env.example`.
