# Кампании: инструкция оператора

Кампания — одна цель («что и где искать»), которую бот сам доводит до конца:
находит группы Facebook, читает их окнами по 20 и присылает найденное в чат.

## Как запустить

- **Текстом:** `/campaign квартиры в аренду в Мадриде` → бот просит
  `confirm <token>` → ответьте ровно этой строкой.
- **Голосом:** в авто-режиме (ниже) авто-оператор просто говорит цель —
  «квартиры в аренду в Мадриде»; бот расшифровывает, показывает текст и ставит
  кампанию. Без авто-режима голос понимает только короткие команды вроде
  «pause everything». Голос — только для операторов и владельцев, с лимитами
  размера и длительности; каждая расшифровка платная (OpenRouter).
- **Авто-режим:** если владелец включил `/auto on`, авто-операторам не нужно
  подтверждение для `/campaign`, `/run`, `/pause`, `/resume`, а обычный текст
  или голос без команды («квартиры в аренду в Мадриде») сразу становится
  кампанией. Бот отвечает «Авто: /campaign поставлена в очередь …». Если цель
  непонятна (нет города или не ясно, что искать), бот объясняет, что уточнить.
- Статус: `/campaign status`. Остановить: `/campaign cancel <id>` (всегда с
  подтверждением).

Города: Мадрид, Барселона, Валенсия, Малага, Аликанте, Севилья, Марбелья, Киев.
Одна кампания — один город. Языки цели: ES/EN/RU/UK.

## Авто-режим

- `/auto on`, `/auto off`, `/auto status` — только владельцы. Переключатель
  хранится в PostgreSQL (`control_settings`) и переживает перезапуск; пока его
  не трогали, действует `AUTO_MODE` из `.env` (по умолчанию `off`).
- Авто-операторы: владельцы (`TELEGRAM_OPERATOR_IDS`) плюс
  `TELEGRAM_AUTO_OPERATOR_IDS`, и только пока у них есть права оператора.
  Помощник (helper) или посторонний никогда не становится авто-оператором.
- Всегда вручную: `/cancel` (включая `/cancel all` и `/campaign cancel`),
  `/login`, кнопки verification, выпуск профиля из карантина, `/role`,
  `/revoke`, `/operators`.
- В аудите такие команды видны с актором `telegram:<id>:auto`
  (`orchestration_audit_log`), `confirmation_id` у них пустой.

## Что видно в чате

- Одно сообщение статуса на кампанию, оно редактируется по ходу:
  `🎯 real_estate · Madrid · rent` и строка вроде «Сейчас: поиск групп
  Facebook», «Сейчас: Facebook · <группа> · ищу дальше», «Сейчас: анализ»,
  «Пауза: …», «Нужна verification», «Кампания завершена · найдено N».
- Находки приходят по одной, каждая ровно один раз, с хвостом
  «🔎 Найдено: N · ищу дальше». В общий дайджест они не попадают.

## Окна, пауза между ними и лимиты

- Найденные группы читаются окнами по **20** групп; каждое окно — обычный
  Facebook-batch, его запускает `facebook-runner`.
- Между окнами пауза `CAMPAIGN_WINDOW_COOLDOWN_SECONDS` (120 с). Окон не
  больше 10. Одновременно Facebook использует только одна кампания.
- Каждое окно проходит те же квоты и предохранители, что и `/run`, кто бы его
  ни запросил (и в авто-режиме тоже): `SAFETY_MAX_FACEBOOK_BATCHES_PER_DAY`,
  `SAFETY_MAX_FACEBOOK_GROUPS_PER_DAY`, `SAFETY_MAX_RUNS_PER_DAY`, а после
  `SAFETY_BREAKER_CHALLENGES` проверок или `SAFETY_BREAKER_FAILURES` сбоев за
  `SAFETY_BREAKER_WINDOW_HOURS` предохранитель открывается.
- **Пауза** («Пауза: daily quota reached …» / «safety breaker open …») значит:
  новое окно сейчас не создаётся; кампания повторит попытку через
  `CAMPAIGN_REFUSAL_RETRY_SECONDS` и продолжит сама, когда квота освободится
  или окно предохранителя пройдёт. Ничего не обходится.

## Verification (проверка Facebook)

Если Facebook показал проверку, окно останавливается, кампания получает
статус «Нужна verification», операторам приходит сообщение с кнопкой.

1. Кнопка открывает Mini App в Telegram (или скопируйте ссылку в Safari).
2. **Claim** — берёте задачу себе.
3. **Open live browser** — браузер профиля; решите проверку руками.
4. **Solved** — бот проверяет страницу.
5. **Resume the run** — окно продолжается с той же группы, кампания идёт дальше.

Пароли и коды вводятся только в браузере сервера; бот их не видит.

## Что всегда делает человек

- **Первый вход** в Facebook-профиль: `/login facebook facebook-main` в личном
  чате с ботом (или `bash scripts/browser_login.sh facebook facebook-main`).
- **Проверки (checkpoint/CAPTCHA)** — через кнопку verification выше.
- **Карантин:** проверка личности, новая 2FA или ограничение аккаунта —
  профиль уходит в карантин, владелец получает одноразовую ссылку. Вернуть
  профиль в работу можно только вручную: владелец разбирается с аккаунтом и
  заново входит через `/login`.

## Настройки `.env`

| Ключ | Смысл |
|---|---|
| `AUTO_MODE` | `on`/`off`, по умолчанию `off`; `/auto` меняет его во время работы |
| `TELEGRAM_AUTO_OPERATOR_IDS` | числовые ID одобренных операторов, кроме владельцев |
| `CAMPAIGN_POLL_SECONDS` | как часто runner проверяет кампании (10) |
| `CAMPAIGN_WINDOW_COOLDOWN_SECONDS` | пауза между окнами (120) |
| `CAMPAIGN_ANALYSIS_GRACE_SECONDS` | сколько ждать анализ после последнего окна (600) |
| `CAMPAIGN_REFUSAL_RETRY_SECONDS` | повтор после отказа квоты/предохранителя (300) |
| `SAFETY_*` | квоты и предохранители, общие для `/run` и кампаний |

## Обновление на сервере

```sh
git pull --ff-only
./scripts/apply_migrations.sh
docker compose --env-file .env up -d --build
```

## Быстрые проверки

```sh
docker compose logs --tail 30 campaign-runner
docker compose logs --tail 30 telegram | grep -E 'auto_queued|auto_switched'
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

```sql
select id, state, stop_reason, plan->>'goal' as goal, created_at
  from campaigns order by created_at desc limit 5;
select state, count(*) from campaign_groups
 where campaign_id = '<id>' group by state;
select state, count(*) from campaign_findings
 where campaign_id = '<id>' group by state;
select key, value, updated_by, updated_at from control_settings;
```
