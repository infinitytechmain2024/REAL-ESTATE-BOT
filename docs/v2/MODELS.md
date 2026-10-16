# Модели по этапам (v2, задача A5)

Что меняется: только значения `OPENROUTER_*_MODEL` в `.env` на VPS (шаг D2) и цены в `bot/utils/costs.py:PRICES`.
Умолчания в коде и в `docker-compose.yml` **не меняются** (правило 2): без строки в `.env` бот работает на прежних
моделях. Откат — вернуть прежнюю строку в `.env`.

> **Цены Anthropic подтверждены 2026-10-16 по справочнику Claude API (первичный API); id и цены OpenRouter — сверить
> владельцу до D2.** В среде разработки нет сети, поэтому id `anthropic/claude-sonnet-5.5`, `anthropic/claude-haiku-5.5`,
> `anthropic/claude-opus-5.5` на openrouter.ai/models и цены на openrouter.ai/pricing не проверены (обычно цены равны
> первичным). При расхождении поправить `PRICES` и эту таблицу. **При 400 от OpenRouter вернуть прежний id**
> (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o-mini`). Неверный id не теряет молча: `analysis` закрывает пост с
> `error`, но кампания выйдет пустой — поэтому D2 делает по одному проверочному вызову на каждом этапе.

В `.env.example` рекомендованное значение лежит закомментированной строкой `# v2 …: KEY=значение` над рабочей строкой.
Причина: `tests/test_deployment.py::test_compose_passes_every_setting_to_its_service` требует, чтобы активные значения
`.env.example` совпадали с умолчаниями `docker-compose.yml`, а их по правилу 2 не трогаем. На VPS владелец (D2)
раскомментирует нужные строки в своём `.env`.

## Таблица

Цена — USD за 1M токенов (вход / выход; Haiku 5.5 — для промптов до 100K токенов, выше 0.5 / 2.5), `bot/utils/costs.py:43` `PRICES`. Используется, только если в ответе OpenRouter
нет `usage.cost` (обычно он есть, тогда пишется его число).

| Этап | Переменная | Где читается | Было | Станет | Цена вход / выход за 1M |
|---|---|---|---|---|---|
| анализ страницы | `OPENROUTER_ANALYSIS_MODEL` | `bot/analysis_pipeline/settings.py:10` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 2 / 10 |
| отбор (reviewer) | `OPENROUTER_REVIEW_MODEL` | `bot/campaign/settings.py:92` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 2 / 10 |
| план поиска | `OPENROUTER_PLAN_MODEL` | `bot/campaign/settings.py:67` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 2 / 10 |
| интервью (до C1) | `OPENROUTER_INTERVIEW_MODEL` | `bot/control_plane/settings.py:77` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 2 / 10 |
| запросы web | `OPENROUTER_WEB_QUERY_MODEL` | `bot/web_search/settings.py:77` | `openai/gpt-4o-mini` | `anthropic/claude-haiku-5.5` | 0.1 / 0.5 |
| лиды (комментарии) | `OPENROUTER_LEADS_MODEL` | `bot/campaign/settings.py:144` | `openai/gpt-4o-mini` | `anthropic/claude-haiku-5.5` | 0.1 / 0.5 |
| reach (инвесторы) | `OPENROUTER_REACH_MODEL` | `bot/campaign/settings.py:184` | `openai/gpt-4o-mini` | `anthropic/claude-haiku-5.5` | 0.1 / 0.5 |
| итоговый отчёт | `OPENROUTER_FINAL_MODEL` | `bot/campaign/settings.py:102` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-opus-5.5` | 4 / 20 |
| match (legacy judge) | `OPENROUTER_MATCH_MODEL` | `bot/campaign/settings.py:82` | `openai/gpt-4o-mini` | без изменений | 0.15 / 0.60 |

Без изменений и вне A5: `OPENROUTER_SOCIAL_MODEL`, `OPENROUTER_INTAKE_MODEL` (gpt-4o-mini), `OPENROUTER_CLAUDE_MODEL`
(уже `anthropic/claude-opus-5.5`, режим reduction), `STT_MODEL`.

`price()` (`costs.py:254`) выбирает самый длинный ключ-префикс: `anthropic/claude-sonnet-5.5` попадает на свою запись, а
`openai/gpt-4o-mini` — не на `openai/gpt-4o`. Старые модели Claude (4.x и ниже) остаются на общих префиксах (sonnet 3 / 15, haiku 1 / 5, opus 15 / 75); версионные префиксы `claude-*-5-5` — для id первичного API. Модель, которой нет в `PRICES`, оценивается по `DEFAULT_PRICE` (3 / 15).
Оценка намеренно с запасом вверх. Тест: `tests/test_costs.py::test_v2_stage_models_have_their_own_price_not_the_default`.

## Оценка вызовов и $ на кампанию

Формула — `docs/idealista-integration/SCRAPING_ANALYSIS.md` §3.5: `вызовов × ($ за вызов)`, где $ за вызов =
`вход_ток × цена_вход + выход_ток × цена_выход`. Допущения §3.5: analysis и reviewer — вход 3–5k, выход 0.5–1k токенов;
число вызовов — analysis 200–400, reviewer до 300. Остальные допущения мои (проверить по `campaign_costs` после первого
прогона): план 1 вызов; интервью 5–14; итоговый отчёт 1 вызов (вход ≈ 3k, выход ≈ 1k); запросы web ≤ 8 (вход ≈ 1k,
выход ≈ 0.3k); лиды и reach ≈ 100 вызовов (вход ≈ 1k, выход ≈ 0.2k).

| Этап | Вызовов | До (цены 3/15, 1/5 и gpt-4o-mini): $ | После (цены 2/10, 0.1/0.5, 4/20): $ за вызов → $ |
|---|---|---|---|
| анализ страницы | 200–400 | 4–12 | 0.011–0.02 → **2.2–8** |
| reviewer | до 300 | 1–6 | 0.011–0.02 → **0.7–4** |
| план + интервью | 6–15 | 0.2–0.6 | 0.011–0.03 → **0.1–0.4** |
| итоговый отчёт | 1 | ≈ 0.03 | ≈ 0.032 → **≈ 0.03** (Opus) |
| запросы web | ≤ 8 | ≈ 0 | 0.00025 → **≤ 0.002** (Haiku) |
| лиды + reach | ≈ 100 | ≈ 0.03 | 0.0002 → **≈ 0.02** (Haiku) |
| **Итого на кампанию** | | **≈ 5.3–18.7** | **≈ 3.1–12.5** |

Вывод: по подтверждённым ценам Sonnet 5.5 (2 / 10) дешевле Sonnet 4.5 (3 / 15) на треть, поэтому кампания дешевеет
примерно на $2–6; Opus 5.5 в отчёте и Haiku 5.5 в лидах и reach — копейки. Основные деньги по-прежнему `analysis` и
reviewer; их число зависит от страниц, дошедших до LLM (префильтр до LLM и режим Apify-сначала: A1, A3). Цены OpenRouter
сверить владельцу (см. выше).

Проверка после первого прогона на VPS: `scripts/eval_campaign.sh <cid>` (блок «campaign_costs по stage/item») — реальные
$ по каждой модели против этой таблицы.
