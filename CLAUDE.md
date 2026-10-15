# REAL-ESTATE-BOT — правила проекта

Telegram-бот (Испания): по задаче пользователя находит объекты недвижимости и/или инвесторов, проверяет находки
и присылает карточки. Python 3.11, asyncio, asyncpg, pydantic v2, httpx, PostgreSQL, Docker Compose на VPS.
План v2: `docs/v2/MASTER_PLAN.md`. Разбивка задач: `docs/v2/TASKS.md`.

## Карта кода
| Где | Что |
|---|---|
| `bot/control_plane/` | Telegram, интервью, голос→текст (`stt.py`), понимание задачи (`understanding.py`, `intake.py`) |
| `bot/campaign/` | кампании, отбор (`tolerance.py`, `relevance.py`), дедуп, отчёт, инвесторы (`reach.py`, `leads.py`) |
| `bot/web_search/` | сайтовый этап; `sources/apify.py` (`ApifyListingSource`), `sources/portals.py` (порталы) |
| `bot/facebook_collector/` | группы FB: браузер или Apify (`apify/`) |
| `bot/acquisition/` | `apify_client.py` (общий Apify HTTP), `contracts.py` (`AcquiredPost`) |
| `bot/analysis_pipeline/` | префильтр → LLM → факты с цитатами |
| `bot/utils/costs.py` | учёт денег (`campaign_costs`) и бюджет кампании |
| `bot/services/db/migrations/` | SQL-миграции, номер = последний + 1; список в `scripts/apply_migrations.sh` |

## Правила (обязательны)
1. **Не трогай то, что не просили.** Нужна правка вне задачи — предложи, не делай.
2. **Всё новое за флагом, выключено по умолчанию.** Откат — одна строка `.env`.
3. **Секреты** только через settings/`.env`; в `.env.example` — пустые ключи; никогда в коде, тестах, логах, фикстурах.
4. **Тесты только на моках** (`httpx.MockTransport`). Live-запросы (Apify, LLM) — руками, вне CI, с «ок» владельца
   и лимитом денег.
5. **Один шаг = один коммит**: `feat(v2): A1 acquisition mode flag`. Ветка на поток `v2/<поток>`, PR, мерж только
   при зелёном CI (`check`, `integration`). Красный «Workers Builds» (Cloudflare) к боту не относится.
6. **Зависимости** (`requirements*.txt`, `pyproject.toml`) — только с обоснованием и «ок» владельца.
7. Новые переменные: в settings, в `docker-compose.yml` нужного сервиса и в `.env.example`
   (`tests/test_deployment.py` это проверяет). Новый пакет в образе — в его Dockerfile (`tests/test_image_imports.py`).
8. Перед PR: `ruff check .` и `pytest` зелёные. Postgres-тесты — с `SYSTEM_TEST_DATABASE_URL`.
9. **Факты не угадывать**: нет числа — `None`; цена без валюты — не цена; площадь участка ≠ площадь дома.
10. **Facebook**: закрытые группы — только через вступление человеком; без cookie-акторов и обхода входа.
    LinkedIn — без автоматизации аккаунта.
11. **Деньги**: каждый платный вызов пишется в `campaign_costs`; запуск актора всегда с `maxTotalChargeUsd`.
12. Ответы владельцу — по-русски, коротко, со ссылками `файл:строка`.

## Команды
```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
BRANCH=main ./scripts/update.sh          # деплой на VPS (/opt/real-estate-bot), запускает владелец
```
