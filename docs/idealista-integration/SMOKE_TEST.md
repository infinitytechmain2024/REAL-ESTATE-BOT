# Шаг 6 — живой smoke-тест

Статус: **завершён 2026-10-10 23:55 UTC**. Кампания
`efc59d3a-dbe6-4063-baca-08c2499b0321`, checkout `ffe5e03`;
расход этой кампании **$0.0317819**; дополнительная live-проверка динамического
актора обошлась в ~$0.04953. Итого около **$0.08131** при лимите $1.
После прогона сервисы возвращены к базовому Compose, Idealista снова default-off.

Обновление 2026-10-11: после исправления семантики площади и явного запроса
границ для «пригородов» выполнен отдельный ограниченный live-тест радиусного
актора. Run `Kb0DWIxF3kJly9inH` (`searchType=center`, Madrid, 25 км,
10 участков) завершился `SUCCEEDED`, стоимость `$0.00458186746`; выдача
содержала как Madrid, так и несколько пригородных муниципалитетов. Код
`1a2dcd5` развёрнут на VPS. `campaign-runner`, `telegram` и
`analysis-worker` перезапущены; Idealista включена в VPS `.env` с
`APIFY_IDEALISTA_MAX_CHARGE_USD=0.10` и `APIFY_IDEALISTA_MAX_RESULTS=20`.
Остальные кампании на момент включения завершены. Контейнерное разрешение
центра Madrid через Redis/Nominatim работает, `/healthz` ответил HTTP 200.
Этот тест проверяет актор и географию, но не является вторым полным прогоном
кампании; точность строительных признаков по-прежнему проверяет анализ.

2026-10-11 по местному времени владелец указал `root@186.241.24.89`;
SSH-доступ к `srv2000490` работает. Проект находится в `/opt/real-estate-bot`; при
первой проверке checkout был `main` @ `e4ba36e`, миграции были только до 039, активных кампаний не было
(`11 completed`, `1 cancelled`). При первом осмотре в `.env` отсутствовали
`APIFY_TOKEN` и `APIFY_IDEALISTA_LOCATION_ID`; токен позднее добавлен владельцем,
а ID задан только во временном override. Исходный ключ OpenRouter через
`/api/v1/key` показал дневной лимит `$50`, для smoke с пределом `$1` он не годится.
Владелец добавил `SMOKE_OPENROUTER_API_KEY`; API подтвердил лимит `$0.50` без сброса,
нулевой расход и остаток `$0.50` (значение ключа не выводилось).
На VPS создана резервная копия БД `/root/idealista-pre-smoke-20261011.dump` (права 0600,
список архива читается через `pg_restore -l`) и копия `.env` для отката. Затем VPS
переключён на `claude/optimistic-feynman-y4ayed` @ `3476c7b`, применены миграции 040–045
штатным скриптом, собраны и перезапущены Python-сервисы. `docker compose ps` показывает
все сервисы `running`, health-check сервисы `healthy`, `/healthz` вернул HTTP 200;
ошибок в логах перезапущенных сервисов за первые две минуты не найдено. До smoke
Idealista оставалась выключенной; платных вызовов не было.

## Условия до первого платного вызова

1. Подтвердить VPS, путь проекта, актуальную ветку и способ отката; прочитать текущую конфигурацию
   без вывода секретов. Проверить, что `APIFY_TOKEN` доступен и актор поддерживает
   текущий land/sale/Madrid input. Город в обычной работе берётся из задачи пользователя:
   с проверенным ID применяется утверждённый fixed-ID actor, иначе name-resolving actor
   `axlymxp/idealista-api-actor`. Первый smoke использует Madrid как город исходной задачи.
   Кандидат ID **города**, не провинции: `0-EU-ES-28-07-001-079` (указан в публичной
   [схеме другого Idealista-актора](https://apify.com/sian.agency/smart-idealista-scraper/input-schema));
   у выбранного fixed-ID актора опубликована только схема ID. Сверить точную географию по результату,
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

   `APIFY_TOKEN` и отдельный `SMOKE_OPENROUTER_API_KEY` должны существовать в
   защищённой среде; ID муниципалитета задан во временном override, значения ключей
   сюда не копировать.
   Для VPS подготовлен [временный Compose override](smoke.compose.yml): он подставляет
   `SMOKE_OPENROUTER_API_KEY` в campaign-runner, analysis-worker и Telegram,
   ограничивает Apify, отключает лишние платные пути и reduction-worker на время теста.
   Применять его только к этим четырём сервисам после проверки обоих ключей:

   ```bash
   docker compose --env-file .env -f docker-compose.yml -f docs/idealista-integration/smoke.compose.yml \
     up -d --no-deps --no-build --force-recreate campaign-runner analysis-worker telegram reduction-worker
   ```

   После фиксации результатов вернуть эти сервисы к обычному Compose-конфигу:

   ```bash
   docker compose --env-file .env -f docker-compose.yml \
     up -d --no-deps --no-build --force-recreate campaign-runner analysis-worker telegram reduction-worker
   ```
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

Фактический baseline из БД VPS — кампания `742d25e7-ea82-45c5-a44a-cc08138bb949`
от 2026-10-08: `80` запросов, `142` поисковых результата, `108` прочитанных HTTP-страниц,
`31` failed URL, `113` находок, `0` exact, `37` similar, `0` отправленных;
исключено: площадь `54`, сделка `14`, тип `3`, место `2`, критерии `2`, вид `1`.
URL: `157` host_cap, `17` no_readable_text, `8` not_a_listing, `6` host_blocked,
`4` HTTP 403, `2` HTTP 405. В `campaign_costs` для старой кампании `0` строк:
миграция 042 тогда ещё не существовала, поэтому ~$15 — оценка из прежнего отчёта,
а не сумма БД. Текст задачи просил **пригород Мадрида**, но сохранённый `TaskSpec`
имел `place.level=city`, `place.name=Madrid`, `radius_km=null` и замечание о пригороде.
Для smoke использована сохранённая структурированная география `city=Madrid`.
Поиск пригородов из свободного текста этим тестом не подтверждён.

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

## Результат живого прогона

Кампания шла с 23:50:20 до 23:55:03 UTC 2026-10-10. Проверялся этап сайтов:
Facebook discovery, browser render, платный scrape, social/reach и reduction были
отключены. Лимиты: 12 запросов, 40 страниц, 20 минут. Web завершился по `page_cap`
после 3 запросов и 40 чтений (20 API + 20 HTTP); кампания завершилась
`queue_exhausted`. Финальный отчёт отправлен в Telegram исходного пользователя;
карточек находок не отправлено.

| Метрика | Старый прогон | Smoke |
|---|---:|---:|
| Поисковые запросы / результаты | 80 / 142 | 3 / 34 |
| HTTP / API-страницы | 108 / 0 | 20 / 20 |
| Неудачные URL | 31 | 0 |
| Находки | 113 | 19 |
| Exact / similar / отправлено | 0 / 37 / 0 | 0 / 8 / 0 |
| Отсев: площадь | 54 | 8 |
| Отсев: сделка / тип / место / критерии / ИИ | 14 / 3 / 2 / 2 / 0 | 0 / 0 / 1 / 1 / 1 |
| Префильтр до ИИ: площадь | не измерялся | 10 (2 Idealista, 8 Kyero) |

Apify run `gEJZviXvsaZvGubYG` завершился `SUCCEEDED`; dataset
`cxFe7H1NnHqy4Sd4K`, 20 результатов и 20 импортированных API-страниц без ошибки.
В сыром ответе все 20 строк имеют `municipality=Madrid`, `propertyType=land`,
`operation=sale`; поле `size` есть у всех (94–22552, десять значений ≥2000),
но отдельного `plot_m2` нет. Адаптер намеренно не приравнивает `size` к площади
участка. Проверка площади и пригодности для застройки опиралась на описания;
точность и полнота этих признаков остаются открытыми.

После кампании отдельно запущен динамический `axlymxp/idealista-api-actor` для
`Barcelona` с `searchType=auto`, `maxProperties=10` и run cap $0.10. Run
`06rrGmWe2VeMGbe38` завершился `SUCCEEDED`, dataset `cd9o0cd2AorIT6lle`
содержит 10 строк; во всех `municipality=Barcelona`, `propertyType=land`,
`operation=sale`. Все 10 строк приняты текущим `normalize_listing`, и ни в одной
не выдуман `plot_m2`. Цена по Apify API после завершения ~$0.04953. Этот
диагностический вызов сделан отдельно от кампании и не входит в её `campaign_costs`.
Вместе с расходом кампании $0.0317819 общий live-расход ~$0.08131.

В URL-воронке: 20 Idealista/API, 19 Kyero/HTTP, 1 ThinkSpain/HTTP;
4 Idealista URL остановлены `robots_txt`, 146 Kyero URL — `page_cap`, 4 URL
помечены дубликатами. В `campaign_findings`: 11 excluded/held и 8 similar/held;
7 similar не подтвердили близость к метро или возможность застройки.

| Провайдер | Ограничение до вызова | Учтено в БД | Подтверждено API |
|---|---:|---:|---:|
| Apify | $0.10 на run | $0.020000 | $0.020000 |
| OpenRouter (`gpt-4o-mini`) | $0.50 lifetime на отдельном ключе | $0.011779 | $0.0117819 |
| Итого | < $1 с запасом на пограничный запрос | $0.031779 | $0.0317819 |

В старой кампании нет строк `campaign_costs`; около $15 — прежняя оценка, а не
сопоставимая сумма из БД. После smoke исходный OpenRouter-ключ снова установлен
в контейнерах, `APIFY_IDEALISTA_ENABLED=false`; Telegram healthy.
