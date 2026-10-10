# Шаг 6 — живой smoke-тест

Статус: **не запущен**. 2026-10-10 локально нет `.env` проекта и рабочего SSH-доступа к VPS;
проверка `root@187.7.65.233` завершилась `Permission denied (publickey,password)`.
Это лишь адрес из локальной истории: принадлежность нужному VPS ещё должен подтвердить владелец.
Миграции и настройки на VPS не менялись, платные вызовы не выполнялись.

## Условия до первого платного вызова

1. Подтвердить VPS, путь проекта, актуальную ветку и способ отката; прочитать текущую конфигурацию
   без вывода секретов. Проверить, что отдельные `APIFY_TOKEN` и валидный
   `APIFY_IDEALISTA_LOCATION_ID` доступны, а актор поддерживает текущий land/sale/Madrid input.
   Кандидат ID **города**, не провинции: `0-EU-ES-28-07-001-079` (указан в публичной
   [схеме другого Idealista-актора](https://apify.com/sian.agency/smart-idealista-scraper/input-schema));
   у выбранного axly опубликована только схема ID. Сверить точную географию по результату,
   не считать ID и семантику `size` окончательно подтверждёнными до fixture.
2. Применить штатным скриптом все миграции до 045 **до** развёртывания нового кода.
   Проверить миграции и кампании read-only SQL, сохранить прежний `.env` и состояние сервиса.
3. Для первого прогона ограничить платные источники: один Apify run с
   `APIFY_IDEALISTA_MAX_CHARGE_USD=0.10`, `APIFY_IDEALISTA_MAX_RESULTS=20`;
   `WEB_SEARCH_SCRAPE_API_URL=` и платные поисковые бэкенды выключены.
   Системный `CAMPAIGN_BUDGET_USD=1` остаётся дополнительной остановкой, но сам не даёт
   жёсткой гарантии из-за отсутствия атомарного резервирования.
4. Для всех OpenRouter-вызовов этой кампании нужен **новый отдельный API-ключ** с
   lifetime limit не выше `$0.50` и `include_byok_in_limit=true`; его `limit_remaining`
   проверить через `/api/v1/key` без печати ключа. Все модели кампании и анализа —
   `openai/gpt-4o-mini`, агентные reductions, платный social/reach и другие внешние
   платные интеграции выключены. До запуска проверить актуальную цену модели/лимиты
   запросов в OpenRouter. Ключ выделить только процессам smoke, проверить отсутствие
   других кампаний и поставить `ANALYSIS_BATCH_SIZE=1`, чтобы ограничить число
   одновременных запросов. При недоступной отдельной квоте запуск отменяется.

   Изолированная конфигурация smoke (значения для проверки, не правка рабочего `.env` вслепую):

   ```dotenv
   CAMPAIGN_BUDGET_USD=1
   APIFY_IDEALISTA_ENABLED=true
   APIFY_IDEALISTA_MAX_CHARGE_USD=0.10
   APIFY_IDEALISTA_MAX_RESULTS=20
   WEB_SEARCH_BACKENDS=searxng
   WEB_SEARCH_SCRAPE_API_URL=
   CAMPAIGN_SEARCH_PLAN_ENABLED=false
   ANALYSIS_BATCH_SIZE=1
   SOCIAL_SEARCH_PLATFORMS=
   INVESTOR_REACH_ENABLED=false
   AGENT_REDUCTION_ENABLED=false
   OPENROUTER_WEB_QUERY_MODEL=openai/gpt-4o-mini
   OPENROUTER_ANALYSIS_MODEL=openai/gpt-4o-mini
   OPENROUTER_REVIEW_MODEL=openai/gpt-4o-mini
   OPENROUTER_MATCH_MODEL=openai/gpt-4o-mini
   OPENROUTER_FINAL_MODEL=openai/gpt-4o-mini
   ```

   `APIFY_TOKEN`, `APIFY_IDEALISTA_LOCATION_ID` и отдельный `OPENROUTER_API_KEY`
   должны существовать в защищённой среде; их значения сюда не копировать.
5. Расчётный предел при подтверждённой изоляции и последовательных запросах: Apify
   `$0.10` + ключ OpenRouter `$0.50` + запас на один запрос, который пересечёт лимит ключа.
   Для `gpt-4o-mini`
   с максимумом 128k входных и 1800 выходных tokens и потолком цен `$0.15/$0.60`
   за 1M tokens этот запас около `$0.021`; суммарно около `$0.621`. Фактические
   цены/контекст, поведение лимита ключа и настройки надо подтвердить непосредственно
   перед вызовом. Это расчёт, а не уже проверенная гарантия: если верхнюю границу $1
   подтвердить нельзя, запуск отменяется. После первого прогона
   Scrape.do можно испытывать отдельной кампанией с собственным пересчитанным лимитом.

Цены и лимиты: [Apify run cap](https://docs.apify.com/api/v2/actors-runs-post),
[OpenRouter key limit](https://openrouter.ai/docs/api/api-reference/api-keys/create-keys),
[OpenRouter current key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key),
[Scrape.do request costs](https://scrape.do/documentation/request-costs/).

## Кампания и результаты

Запрос: «участок, Мадрид, покупка, ≥2000 м²». После запуска записать campaign ID,
время, commit, effective settings **без ключей**, run ID Apify, число импортированных
земельных объявлений и подтверждённых plot_m2. Сравнить с прошлым прогоном:
около `$15`, отправлено `0`, «не удалось проверить» `31/37`, «не та площадь» `54`,
«аренда» `14`. Итоговая таблица должна содержать найдено, отправлено, отсеяно до ИИ,
отклонено по каждой причине, URL-отказы и фактические расходы по стадиям и источникам.

```sql
-- Подставить campaign_id через psql \set cid '...'. Только чтение.
select stage, kind, provider, item, code, sum(units) units, sum(cost_usd) usd
  from campaign_costs where campaign_id = :'cid'::uuid
 group by 1,2,3,4,5 order by 1,2,3,4,5;
select queries, results, pages_http, pages_render, pages_scrape, pages_api,
       pages_failed, findings, exact, "similar", other, excluded, duplicates
  from campaign_metrics where campaign_id = :'cid'::uuid;
select host, state, layer, detail, count(*)
  from web_campaign_urls where campaign_id = :'cid'::uuid
 group by 1,2,3,4 order by 1,2,3,4;
select name, state, run_id, dataset_id, import_offset, error_code
  from web_listing_source_runs where campaign_id = :'cid'::uuid;
```

Все фактические результаты и выводы будут добавлены сюда после живого прогона.
