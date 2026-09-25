# Системный тест и запуск в работу

Документ для оператора: что проверено автоматически, как пройти ручную
проверку на VPS и что по-прежнему делается руками. Все команды выполняются
на сервере из папки проекта:

```sh
cd /opt/real-estate-bot
```

SQL-проверки ниже запускаются так (подставьте запрос):

```sh
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "<запрос>"'
```

## 1. Как устроена цепочка

```
Telegram (текст или голос) -> подтверждение "confirm <token>" -> Orchestra (PostgreSQL)
   |- /run facebook-groups -> пакет + заявка на запуск -> facebook-runner -> посты
   |- /run website         -> запуск scrapling  -> scrapling-worker   -> пост
   '- /run instagram|tiktok|facebook -> запуск agent_ridge -> agent-reach-worker -> пост
посты (normalised) -> analysis-worker (фильтры -> OpenRouter) -> находки -> дайджест в Telegram
проверка Facebook/Instagram -> verification job -> Mini App: Claim/Solved/Resume -> повторный запуск
```

Каждый шаг пишет состояние в PostgreSQL. Повторная доставка любого шага
ничего не дублирует: у Telegram-сообщения, команды, пакета, заявки на запуск,
поста, находки и дайджеста есть свой ключ идемпотентности.

## 2. Что исправлено при интеграции

| Разрыв | Что было | Что стало |
| --- | --- | --- |
| Посты Facebook не доходили до анализа | сохранялись как `discovered`, анализ берёт только `normalised` | сохраняются как `normalised` |
| Источники из `/run` не анализировались | создаются с `vertical='both'`, анализ выбирал только `real_estate`/`investors` | `both` анализируется по обеим вертикалям, итог поста один |
| Дата поста ломала чтение группы | строка из `<time datetime>` передавалась в `timestamptz`, группа падала целиком | строка разбирается в дату, непонятная отбрасывается |
| Два воркера анализа могли платить за один пост | блокировка снималась до вызова OpenRouter | надёжный захват с истечением (миграция 013) |
| `/run` только ставил план | сборщики и анализ запускались руками | постоянные воркеры `facebook-runner`, `scrapling-worker`, `agent-reach-worker`, `analysis-worker` |
| Agent Reach ничего не сохранял | принимал JSON и печатал результат | берёт запуски из базы, сохраняет посты, при проверке открывает verification job |
| Дайджест мог потеряться | обрезался до 4000 символов; при ошибке Telegram не отправлялся больше никогда | делится на сообщения; неотправленный уходит в следующем цикле, находки становятся `delivered` |
| Квот и «автоматов» не было | `/run` ограничивался только 20 группами на пакет | дневные квоты и автоматы по проверкам и сбоям, см. README |
| Зависший воркер Agent Reach держал профиль | профиль оставался `in_use` | dispatcher через `ORCHESTRA_STALE_BATCH_SECONDS` закрывает запуск и освобождает профиль |

## 3. Автоматические тесты

`tests/test_system_integration.py` на настоящем PostgreSQL со всеми
миграциями. Подменены только Facebook (браузер и чтение групп), OpenRouter
(анализ и распознавание речи) и отправка в Telegram.

| Сценарий | Проверяет |
| --- | --- |
| Текстовая команда → пакет из 2 групп → анализ → дайджест | подтверждение, заявка на запуск, группы по порядку под одной арендой профиля, `published_at`, фильтры до модели, по одному дайджесту на вертикаль, структура обоих форматов, повторы ничего не дублируют, сообщение заказчику |
| Голосовая команда | распознавание → «Understood as: /pause all» → подтверждение → источники на паузе, аудит с автором; не-оператор до провайдера не доходит |
| Проверка посреди пакета | пакет и профиль остановлены, прочитанная группа сохранена, Mini App: Claim → Solved → Resume, повтор только с проблемной группы, два уведомления заказчику, дайджест |
| Сайт и Instagram | каждая команда попадает в свой воркер, профиль Instagram возвращается в `ready`, оба поста анализируются |
| Проверка в Agent Reach | запуск и профиль остановлены, verification job, Resume возвращает запуск в очередь, повтор успешен |
| Квоты и автоматы | лишний пакет, лишние запуски, автомат по сбоям и по проверкам отказывают до создания работы, команда `precondition_failed` |
| Два воркера анализа | не делят посты; захват умершего воркера забирается, его старый токен ничего не пишет |
| Telegram недоступен | дайджест остаётся `queued`, уходит в следующем цикле ровно один раз |

Запуск локально:

```sh
createdb monitoring_test
SYSTEM_TEST_DATABASE_URL=postgresql://localhost/monitoring_test python -m pytest tests/test_system_integration.py
```

В CI это отдельная задача `integration` с сервисом PostgreSQL. Она гоняет
этот файл и остальные тесты на базе: верификацию, Orchestra и доступ.

## 4. Чек-лист запуска на VPS

1. **Код и миграции**
   ```sh
   git pull --ff-only
   ./scripts/apply_migrations.sh      # последняя строка: Applying: 013_analysis_claims.sql
   ```
2. **`.env`** (секреты не пересылать в чат):
   - `TELEGRAM_OPERATOR_IDS`: ваш ID и ID остальных владельцев.
   - `LIVE_VIEW_PUBLIC_URL`: HTTPS-адрес для `/login` и страницы проверки.
   - `OPENROUTER_API_KEY`: тот же ключ, что для голоса. Без него `analysis-worker` простаивает.
   - `ANALYSIS_TELEGRAM_CHAT_ID`: чат, куда идут дайджесты (ваш ID или ID группы). Без него находки сохраняются, но не отправляются.
   - По желанию `SAFETY_*` (квоты) и `*_POLL_SECONDS`. Значения по умолчанию описаны в README.
3. **Запуск**
   ```sh
   docker compose --env-file .env up -d --build
   docker compose ps
   ```
   В логах каждого воркера должна быть строка готовности:
   ```sh
   docker compose logs --tail 20 facebook-runner agent-reach-worker scrapling-worker analysis-worker verification
   ```
   Ожидается `facebook_runner.ready`, `agent_reach.ready`, `scrapling.ready`,
   `analysis.started` (или `analysis.disabled`, если нет ключа) и
   `verification.started`.
4. **Профили браузера.** Один раз войдите в каждый аккаунт через `/login`
   (`/login facebook`, `/login instagram`, `/login tiktok`) и нажмите Done.
   Проверка:
   ```sql
   select profile_name, platform, state from browser_profiles;
   ```
   Для работы нужен `ready`.

## 5. Ручная проверка на VPS

Каждый шаг делайте в личном чате с ботом.

| # | Действие | Ожидаемый результат |
| --- | --- | --- |
| 1 | `/status` | Сводка по источникам и командам |
| 2 | `/run website https://<публичная страница с объявлением>`, затем `confirm <token>` | «queued run …; it starts automatically». Через 15–30 с запуск в `succeeded` (SQL ниже) |
| 3 | `/run facebook-groups <1–2 группы>`, затем `confirm` | «queued Facebook batch …», потом «Batch … started automatically.» и итог «finished: all its groups were read» (эти два сообщения шлёт сервис `verification`, ему нужен `LIVE_VIEW_PUBLIC_URL`) |
| 4 | Подождать до 1–2 минут | Если есть подходящие посты, дайджест «🏠 Real Estate proposition» и/или «📈 Investor lead» в `ANALYSIS_TELEGRAM_CHAT_ID` |
| 5 | Отправить тот же `confirm <token>` ещё раз | «Confirmation is invalid, expired…»: второй пакет не создаётся |
| 6 | Голосовое «статус» или «пауза все» | Текст расшифровки, «Understood as: …», для паузы запрос подтверждения |
| 7 | Если Facebook покажет проверку | Кнопка «Open verification page» → Claim → View → Solved → Resume, затем сообщения о повторном запуске и итоге |

SQL для проверки:

```sql
-- запуски и пакеты за последний час
select acquisition_method, state, error_code, created_at from acquisition_runs where created_at > now() - interval '1 hour' order by created_at;
select id, state, max_items, created_at from acquisition_batches order by created_at desc limit 5;
select batch_id, state, result, error from collector_launch_requests order by requested_at desc limit 5;
-- посты и анализ
select state, count(*) from collected_posts group by state;
select vertical, state, count(*) from findings group by vertical, state;
select vertical, state, created_at from analysis_digests order by created_at desc limit 5;
-- отказы по квотам и автоматам
select arguments, error_detail from orchestration_commands where error_code = 'precondition_failed' order by created_at desc limit 5;
```

**Квоту безопасно проверить так:** временно поставьте
`SAFETY_MAX_RUNS_PER_DAY=1` и выполните
`docker compose up -d telegram`. Затем два раза подряд
`/run website …`: второй ответит «cannot run: daily quota reached». После
проверки верните значение.

## 6. Что по-прежнему делается руками

- **Первый вход в аккаунты** Facebook, Instagram и TikTok через `/login`.
  Повторный вход нужен, если сессия истекла.
- **Решение проверок** (CAPTCHA, checkpoint) в Mini App. Подтверждение
  личности, новая 2FA и блокировка аккаунта сразу останавливают работу и
  уходят только владельцу.
- **Секреты в `.env`** и их ротация, deploy key для `git pull`.
- **Старые посты Facebook.** Всё, что собрано до этого обновления, лежит в
  `discovered` и в анализ не попадает. Если нужно их проанализировать
  (это расход OpenRouter; посты с известной датой старше 90 дней отбрасываются фильтром):
  ```sql
  update collected_posts set state='normalised' where state='discovered' and coalesce(body_text,'') <> '';
  ```
- **Supabase** подключается владельцем отдельно; эта интеграция его не трогает.

## 7. Известные ограничения

- **Автотесты не видят настоящий Facebook.** Разметка Facebook меняется без
  предупреждения, поэтому чтение групп проверяется только вживую (шаг 3).
  Если группа вернула 0 постов, в `browser_screenshots` сохраняется снимок
  экрана.
- **Страница проверки с телефона** на настоящей проверке ещё не проходилась
  от начала до конца.
- **Instagram и TikTok** читаются как одна публичная страница (текст), без
  комментариев. Нужен свой профиль браузера в `ready`.
- **Один профиль на платформу.** Пакет Facebook ждёт, пока Agent Reach держит
  профиль Facebook. При редком одновременном старте один из двух упадёт на
  аренде браузера и будет отмечен `failed`.
- **Доставка дайджеста** — «как минимум один раз». Если сервис упадёт ровно
  между успешной отправкой в Telegram и записью в базу, дайджест придёт
  повторно. Окно — доли секунды.
- **Квоты считаются по последним 24 часам,** а не по календарным суткам.
  Автомат по сбоям считает только отказы после последнего успеха.
- **Анализ** берёт посты только активных источников. Посты источника на
  паузе ждут `/resume`.
