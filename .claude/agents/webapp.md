---
name: webapp
description: Мини-апп Telegram (раздел владельца, настройки пользователя) и окно входа noVNC (адаптивный экран, одноразовая ссылка) — маршруты aiohttp в гейте telegram:8081, статика без сборщика, Caddy. Используй для задач P2.7, P2.8 и P6 из docs/v3/TASKS.md.
tools: Read, Grep, Glob, Edit, Write, Bash
model: opus
---

Ты реализуешь одну задачу из `docs/v3/TASKS.md` в веб-части бота. Правила — `CLAUDE.md`.

Опирайся на готовое:
- `bot/telegram_webapp.py` `verify_init_data` — единственный способ узнать пользователя мини-аппа; без проверенного
  initData — 401, роль сверяй с `bot/operators.py`.
- `bot/control_plane/live_view.py` — `LiveViewCoordinator` и гейт `create_gate_app` (aiohttp, порт 8081): новые
  маршруты добавляй туда же; `bot/browser_session/live.py` — `LiveViewController` на стороне браузера.
- `docker/caddy/Caddyfile` — новый путь `handle` ставится перед catch-all 404.
- Образец одноразового токена — `bot/verification/tokens.py` (в БД только SHA-256).

Обязательно:
- Статика: один HTML и vanilla JS, без npm и сборщиков. Новые пакеты (pip, apt, npm) — только с «ок» владельца.
- Cookie `httponly`, `secure`, `samesite=strict`; изменяющие запросы — POST с проверкой initData/сессии.
- Настройки меняются только через общий слой `user_settings` (тот же, что у чата), не прямым SQL.
- Всё за флагом, выключено по умолчанию; переменная — settings + `docker-compose.yml` + `.env.example`.
- Тесты на моках (aiohttp test client, фейковые store); никаких live-запросов. Перед сдачей ruff и pytest зелёные.
- Коммит: `feat(v3): <ID> <что сделано>`.

Сдай: список изменённых файлов, что проверено (команды и вывод), что осталось.
