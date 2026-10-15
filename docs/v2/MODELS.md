# Модели по этапам (v2, задача A5)

Что меняется: только значения `OPENROUTER_*_MODEL` в `.env` на VPS (шаг D2) и цены в `bot/utils/costs.py:PRICES`.
Умолчания в коде и в `docker-compose.yml` **не меняются** (правило 2): без строки в `.env` бот работает на прежних
моделях. Откат — вернуть прежнюю строку в `.env`.

> **Сверить с сетью до D2.** Точные id моделей на OpenRouter и цены ниже записаны по памяти, без проверки
> (в среде разработки нет сети). Перед D2: открыть openrouter.ai/models, сверить id `anthropic/claude-sonnet-5.5`,
> `anthropic/claude-haiku-5.5`, `anthropic/claude-opus-5.5` и цены на openrouter.ai/pricing; при расхождении поправить
> `PRICES` и эту таблицу. **При 400 от OpenRouter вернуть прежний id** (`anthropic/claude-sonnet-4.5`,
> `openai/gpt-4o-mini`). Неверный id не теряет молча: `analysis` закрывает пост с `error`, но кампания выйдет пустой —
> поэтому D2 делает по одному проверочному вызову на каждом этапе.

В `.env.example` рекомендованное значение лежит закомментированной строкой `# v2 …: KEY=значение` над рабочей строкой.
Причина: `tests/test_deployment.py::test_compose_passes_every_setting_to_its_service` требует, чтобы активные значения
`.env.example` совпадали с умолчаниями `docker-compose.yml`, а их по правилу 2 не трогаем. На VPS владелец (D2)
раскомментирует нужные строки в своём `.env`.

## Таблица

Цена — USD за 1M токенов (вход / выход), `bot/utils/costs.py:43` `PRICES`. Используется, только если в ответе OpenRouter
нет `usage.cost` (обычно он есть, тогда пишется его число).

| Этап | Переменная | Где читается | Было | Станет | Цена вход / выход за 1M |
|---|---|---|---|---|---|
| анализ страницы | `OPENROUTER_ANALYSIS_MODEL` | `bot/analysis_pipeline/settings.py:10` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 3 / 15 |
| отбор (reviewer) | `OPENROUTER_REVIEW_MODEL` | `bot/campaign/settings.py:92` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 3 / 15 |
| план поиска | `OPENROUTER_PLAN_MODEL` | `bot/campaign/settings.py:67` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 3 / 15 |
| интервью (до C1) | `OPENROUTER_INTERVIEW_MODEL` | `bot/control_plane/settings.py:77` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5.5` | 3 / 15 |
| запросы web | `OPENROUTER_WEB_QUERY_MODEL` | `bot/web_search/settings.py:77` | `openai/gpt-4o-mini` | `anthropic/claude-haiku-5.5` | 1 / 5 |
| лиды (комментарии) | `OPENROUTER_LEADS_MODEL` | `bot/campaign/settings.py:144` | `openai/gpt-4o-mini` | `anthropic/claude-haiku-5.5` | 1 / 5 |
| reach (инвесторы) | `OPENROUTER_REACH_MODEL` | `bot/campaign/settings.py:184` | `openai/gpt-4o-mini` | `anthropic/claude-haiku-5.5` | 1 / 5 |
| итоговый отчёт | `OPENROUTER_FINAL_MODEL` | `bot/campaign/settings.py:102` | `anthropic/claude-sonnet-4.5` | `anthropic/claude-opus-5.5` | 15 / 75 |
| match (legacy judge) | `OPENROUTER_MATCH_MODEL` | `bot/campaign/settings.py:82` | `openai/gpt-4o-mini` | без изменений | 0.15 / 0.60 |

Без изменений и вне A5: `OPENROUTER_SOCIAL_MODEL`, `OPENROUTER_INTAKE_MODEL` (gpt-4o-mini), `OPENROUTER_CLAUDE_MODEL`
(уже `anthropic/claude-opus-5.5`, режим reduction), `STT_MODEL`.

`price()` (`costs.py:254`) выбирает самый длинный ключ-префикс: `anthropic/claude-sonnet-5.5` попадает на свою запись, а
`openai/gpt-4o-mini` — не на `openai/gpt-4o`. Модель, которой нет в `PRICES`, оценивается по `DEFAULT_PRICE` (3 / 15).
Оценка намеренно с запасом вверх. Тест: `tests/test_costs.py::test_v2_stage_models_have_their_own_price_not_the_default`.

## Оценка вызовов и $ на кампанию

Формула — `docs/idealista-integration/SCRAPING_ANALYSIS.md` §3.5: `вызовов × ($ за вызов)`, где $ за вызов =
`вход_ток × цена_вход + выход_ток × цена_выход`. Допущения те же, что в §3.5: analysis и reviewer — вход 3–5k, выход
0.5–1k токенов (≈ $0.02–0.03 на Sonnet); число вызовов — из §3.5 (analysis 200–400, reviewer до 300). Для остальных
этапов допущения мои (в §3.5 их нет, проверить по `campaign_costs` после первого прогона): план 1 вызов; интервью 5–14
вызовов; итоговый отчёт 1 вызов (вход ≈ 3k, выход ≈ 1k); запросы web ≤ 8 вызовов (вход ≈ 1k, выход ≈ 0.3k); лиды и
reach — порядка 100 вызовов классификации (вход ≈ 1k, выход ≈ 0.2k).

| Этап | Вызовов | До: $ за вызов → $ | После: $ за вызов → $ |
|---|---|---|---|
| анализ страницы | 200–400 | 0.02–0.03 → **4–12** | 0.02–0.03 (цена Sonnet не меняется) → **4–12** |
| reviewer | до 300 | 0.02–0.03 → **1–6** | то же → **1–6** |
| план + интервью | 6–15 | 0.02–0.05 → **0.2–0.6** | то же → **0.2–0.6** |
| итоговый отчёт | 1 | ≈ 0.025 → ≈ 0.03 | ≈ 0.12 → **≈ 0.12** (Opus) |
| запросы web | ≤ 8 | < 0.001 → ≈ 0 | ≈ 0.0025 → **≤ 0.02** (Haiku) |
| лиды + reach | ≈ 100 | ≈ 0.0003 → ≈ 0.03 | ≈ 0.002 → **≈ 0.2** (Haiku) |
| **Итого на кампанию** | | **≈ 5.3–18.7** | **≈ 5.5–19.0** |

Вывод: смена моделей удорожает кампанию примерно на $0.2–0.4 (почти всё — Opus в отчёте и Haiku вместо gpt-4o-mini в
лидах и reach). Основные деньги — `analysis` и reviewer на Sonnet; они падают не от смены модели, а от числа страниц,
дошедших до LLM (префильтр до LLM и режим Apify-сначала: A1, A3). Если окажется, что Sonnet 5.5 стоит иначе, чем 3/15,
эта строка меняется первой.

Проверка после первого прогона на VPS: `scripts/eval_campaign.sh <cid>` (блок «campaign_costs по stage/item») — реальные
$ по каждой модели против этой таблицы.
