---
name: new-migration
description: Создать SQL-миграцию PostgreSQL для бота — номер, добавляющие изменения, регистрация в apply_migrations.sh, откат, store, Postgres-тесты. Используй, когда задаче нужна новая таблица, колонка или constraint.
---

# new-migration

1. Номер: `ls bot/services/db/migrations | tail -1` + 1, имя `NNN_<что>.sql`.
2. Только добавляющие изменения, идемпотентно (`create table if not exists`, `add column if not exists`,
   `drop constraint if exists` → `add constraint`). Старый код должен работать на новой схеме.
3. Уважай миграцию 003: триггер переходов состояний и «один running item на батч».
4. Зарегистрируй файл в **обоих** списках `scripts/apply_migrations.sh` (`tests/test_project_foundation.py`).
5. Откат оператора: `scripts/rollback/NNN_<что>_down.sql`, отказывается, если есть данные.
6. Store: асинхронный класс на `asyncpg.Pool`, `set_config('app.actor', …)` в транзакции, как в
   `bot/facebook_collector/apify/store.py`.
7. Тесты: Postgres-тест с `SYSTEM_TEST_DATABASE_URL` (пропускается без него) + тест на памяти.
8. В PR: команды применения на VPS (делает `scripts/update.sh`) и проверки (`select filename from schema_migrations`).
