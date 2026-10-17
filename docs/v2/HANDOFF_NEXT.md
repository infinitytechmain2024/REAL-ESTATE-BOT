# Передача работы: план после разбора G2 (Валенсия) и промпт для следующего ИИ

Дата: 2026-10-17. Основание: 35 файлов ответов бота по задаче «Валенсия · покупка · квартира · до 250 000 € ·
2–3 комнаты · обязательно терраса» (кампания `d7358ef7-ba48-41ea-b873-5370f66ab4e8`) и вывод `scripts/eval_campaign.sh`.

## 1. Факты, от которых отталкиваемся

| Факт | Цифры | Где в коде |
|---|---|---|
| Кампания стоила $0.83, не $5 | Sonnet-проверка $0.60 (53 вызова), Haiku-разбор $0.11 (109), Opus план+отчёт $0.07 (2), Apify $0.06 | `bot/utils/costs.py` |
| $7.07 ушли в теневой гибрид Claude→Jev (`reduction-worker`), вызовы без `campaign_id` | 24 + 260 вызовов Opus | `bot/agents/`, `docker-compose.yml` (`reduction-worker`), `docs/HYBRID_AGENTS.md` |
| Дубли не склеены между порталами | 7 пар из 28 карточек | `bot/campaign/dedup.py:282` (`same_object`) |
| Невалидный JSON от модели разбора теряет страницу без повтора | 19 из 128 (15 %) | `bot/analysis_pipeline/main.py:129`, `bot/analysis_pipeline/openrouter.py:454` |
| Аукцион / nuda propiedad показаны как обычные варианты | 5 карточек | `bot/analysis_pipeline/prefilter.py:121`, `bot/campaign/relevance.py:374` |
| 24 «похожих» при 3 точных; 4 карточки вообще без упоминания террасы | — | `bot/campaign/relevance.py:395-407`, `bot/campaign/offers.py:61` |
| Отчёт: «Idealista — не дошла очередь» при 0 объявлениях; лучшие по убыванию цены; совет «уточните цену»; бюджет +10 % при уже заданном ±10 % | — | `bot/campaign/final_report.py:244`, `:337`, `:384`, `:299` |

## 2. Решения владельца (17.10)

- «гибрид выкл»: `AGENT_REDUCTION_ENABLED=false` на VPS, гибрид не включать до сдачи.
- «без террасы не слать»: если обязательный критерий (терраса) в объявлении не упомянут, карточку не отправлять,
  только считать в отчёте.
- Модели: Haiku разбор, Sonnet проверка, Opus план и отчёт. Проверку на Haiku не переводить.
- Jev Router (`typesafe/jev-router`) не использовать: сам Jev — модель-решатель без генерации JSON.

## 3. План (по порядку, каждая задача = ветка `v2/<поток>`, PR, squash-merge при зелёном CI)

| Задача | Ветка | Суть | DoD |
|---|---|---|---|
| H7 | `v2/h7-unverified` (есть коммит `bba1ff2`, worktree `/home/user/wt/h7`) | «Не подтверждено: <критерии>» вместо «не удалось проверить автоматически» в карточках и в вопросе | PR, CI зелёный, тест `test_cards_and_question_name_the_unconfirmed_criteria` |
| H8 | `v2/h8` | Дедуп между порталами: одна цена (±1 %), площадь ±3 %, одно число комнат, один город → один объект, если ещё совпадает телефон **или** агентство **или** ≥2 общих особенности, даже когда районы названы по-разному (`L'Hort de Senabre` vs `Estación de Patraix`) | Тест на 7 парах из G2 (фикстуры из карточек ниже); старые тесты зелёные; ложных склеек в `tests/test_dedup*.py` нет |
| H9 | `v2/h9` | При `invalid_structured_model_response`: один повтор той же моделью, затем один вызов `OPENROUTER_REVIEW_MODEL`; флаг `ANALYSIS_INVALID_RETRY=true/false` (по умолчанию false), каждая попытка в `campaign_costs` | Тест на `httpx.MockTransport`: 1-й ответ мусор, 2-й валиден → находка; оба мусор → запасная модель; флаг выкл → старое поведение |
| H11 | `v2/h11` | Обязательный критерий (`must`/`required` в `TaskSpec`) с вердиктом `unknown` → bucket `similar`, state `held`, `hold_reason` «Не подтверждено: терраса», **не предлагать к показу**; отчёт: «Ещё N без упоминания: терраса» | Тест: unknown по обязательному → не отправлено, попадает в счётчик отчёта; unknown по необязательному → как раньше |
| H10 | `v2/h10` | (а) `subasta`/`aukcion`/`nuda propiedad`/`usufructo` в тексте → критерий `deal_kind: fail` с цитатой, карточка с «≈ Отличается: аукцион»; (б) отчёт: строка «Вне кампании за это время: $X (reduction-worker …)» по `campaign_costs` с `campaign_id is null` между `created_at` и `finished_at`; (в) «Idealista — 0 объявлений (1 невалидное)» вместо «не дошла очередь», когда `web_listing_source_runs` есть; (г) лучшие варианты: сначала точные, затем по цене по возрастанию; (д) убрать советы «уточните цену» и «поднять бюджет» при заданном отступлении; (е) `docs/v2/DEPLOY.md` §0a: `AGENT_REDUCTION_ENABLED=false` в списке флагов, которые остаются выключенными | Тесты на каждую букву; `ruff`, `pytest` зелёные |
| Прогон | — | Владелец: `BRANCH=main ./scripts/update.sh`, та же задача по Валенсии, `./scripts/eval_campaign.sh <id>`; сравнить с 17.10: дубли ≤ 1, потерянных страниц ≤ 3, карточек без террасы 0, расход ≤ $1 | `docs/v2/GOLDEN_SET.md` G2 заполнен |
| Дальше | — | D1 (G3–G9), cron (`DEPLOY.md` §6), E1 `/sources`, после сдачи E2 TikTok, E3 LinkedIn (`docs/v2/E2_E3_SOURCES.md`) | — |

## 4. Фикстуры для H8: пары из G2, которые должны склеиться

1. 215 000 € · 82 м² · 3 комн. · fotocasa `191129754` (L'Hort de Senabre, тел. +34936641101) ↔ milanuncios `620102371` (Estación de Patraix, ref LCV44955).
2. 177 000 € · 80 м² · 2 комн. · fotocasa `191128304` (Mont-Olivet, Tecnocasa Ruzafa) ↔ habitaclia `i53578000000071` (Quatre Carreres, Tecnocasa Ruzafa).
3. 81 700 € · 107 м² · 3 комн. · fotocasa `191129642` (Malvarrosa, Bhorus) ↔ milanuncios `620101497` (Valencia, Bhorus).
4. 129 000 € · 63 м² · 3 комн. · fotocasa `191123233` (Els Orriols, MOLMAR) ↔ habitaclia `i8142004023436` (Rascanya/Els Orriols, MOLMAR).
5. 145 000 € · 100 м² · 3 комн. · fotocasa `191120556` (Torrefiel, Prodeinmo) ↔ habitaclia `i11666000005131` (Rascanya/Torrefiel, Palanca Fontestad) — разные агентства, общие особенности `exterior`, «ремонт», «рендеры ИИ»; склеивать только при ≥2 общих особенностях.
6. 104 500 € · 56 м² · без комнат · fotocasa `191120381` (Torrefiel, PUNTO PUBILLA) ↔ habitaclia `i8478003872641` (Rascanya, LA CASA AGENCY) — `terraza`, этаж 0, «открытая планировка».
7. 249 000 € · 82 м² · 2 комн. · этаж 6 · fotocasa `191128580` (Torrefiel, La Casa Agency) ↔ habitaclia `i57144000001425` (Rascanya, LA CASA AGENCY ZORROZA) ↔ yaencontre `inmueble-41740-112836296` (Tormos, PUNTO LAS DELICIAS; этаж не указан).

Не склеивать: 253 000 € Nou Benicalap (новостройка, тип 2) с другими 2-комнатными 82 м² — другая цена.

## 5. Промпт для следующего ИИ (скопировать целиком)

```
Ты — главный архитектор проекта REAL-ESTATE-BOT (Telegram-бот, Испания: находит объекты недвижимости и
инвесторов, проверяет находки и присылает карточки). Репозиторий: github.com/infinitytechmain2024/real-estate-bot,
ветка main. Сначала прочитай CLAUDE.md (правила обязательны), docs/v2/HANDOFF_NEXT.md (этот план),
docs/v2/HANDOFF.md, docs/v2/tasks.json (доска) и docs/v2/CLIENT_DECISIONS.md.

Что нельзя: переписывать работающие части с нуля; включать что-либо по умолчанию; вставлять секреты куда-либо;
live-тесты в CI; обходить вход/закрытость групп FB, cookie-акторы, автоматизацию аккаунта LinkedIn; использовать
подписку Claude для продового трафика (только ANTHROPIC_API_KEY / OpenRouter).

Как работать: одна задача = ветка v2/<поток> от origin/main = один PR = squash-merge при зелёных проверках
`check` и `integration` (красный Cloudflare «Workers Builds» не относится к боту). Перед PR:
`.venv/bin/ruff check .` и `.venv/bin/python -m pytest -p no:cacheprovider -W ignore -o addopts=""`.
Тесты только на моках (httpx.MockTransport). Новая переменная окружения: settings + docker-compose.yml нужного
сервиса + .env.example (tests/test_deployment.py проверяет). Коммиты: `feat(v2): H8 cross-portal dedup by
price, area and rooms`. После каждого изменения статуса задачи: правь docs/v2/tasks.json, запусти
`python3 scripts/build_v2_board.py`, коммит `chore(v2): board` в ветку claude/jolly-cray-qgqw24, push.
Деплой делает владелец: `BRANCH=main ./scripts/update.sh` на VPS /opt/real-estate-bot; ты готовишь команды.

Текущее состояние: см. docs/v2/HANDOFF_NEXT.md §1–§4. Очередь: H7 (ветка v2/h7-unverified уже содержит
коммит bba1ff2, нужно открыть PR и смержить), затем H8, H9, H11, H10 в этом порядке, с DoD из §3 и
фикстурами из §4. Если PR по H8/H9 уже открыт предыдущей сессией — не дублируй, доведи его до мержа.

Решения владельца, которые не обсуждаются: гибрид Claude→Jev (reduction-worker) выключен до сдачи;
карточки, где обязательный критерий не упомянут, не отправляются; модели: Haiku разбор, Sonnet проверка,
Opus план и отчёт; Jev Router не используем.

Отвечай владельцу по-русски, коротко, со ссылками файл:строка. Вопросы владельцу — только когда разные ответы
ведут к разной работе; остальное решай сам и фиксируй в docs/v2/CLIENT_DECISIONS.md.
```
