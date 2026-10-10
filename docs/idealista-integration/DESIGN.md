# DESIGN — интеграция Idealista, фаза 0

Дата: 2026-10-10. Ветка: `claude/optimistic-feynman-y4ayed`.
Основание: HANDOFF, SCRAPING_ANALYSIS, TECHNICAL_SPECIFICATION, PLAN, CHECKLIST,
AGENTS и `.claude/skills/real-estate-pipeline/SKILL.md`.
Владелец ответом «yes» утвердил `axlymxp/idealista-scraper` и Scrape.do как резерв
и разрешил переход к дизайну и фазе 1. Это разрешение на разработку; платные вызовы,
покупка подписок и изменения VPS этим ответом не разрешены. После фазы 1 — остановка.

## Решение и границы

Добавить структурированный источник до HTML-поиска в первом раунде. Результат источника
становится обычным `collected_posts` через WebStore. Analysis, tolerance, дедуп объектов,
карточки и Recorder-outbox получают прежнюю цепочку данных; API не присваивает `exact`.
Успех получения данных не равен подтверждённой релевантности.

Apify выключен по умолчанию; supports первоначально ограничен проверяемым
`ES / real_estate / land / sale`, доступным порталом idealista.com и точной географией.
Для квартир, аренды, инвесторов и других стран работает существующий поиск. Поддержку
других комбинаций расширять только с fixture и проверенной схемой актора.

Scrape.do — последний существующий HTML-слой `scrape`, без нового клиента и без
изменения 20 обязательных порталов. Успех Fotocasa пока не подтверждён. Собственный
обход DataDome/капч, stealth-патчи, решатели, Facebook discovery/SAFETY_* не добавляются.

## Точки интеграции в текущем коде

Номера строк относятся к базе до реализации фаз 1–5, а не к будущему коду.

| Файл:строка → функция | Изменение и фаза |
|---|---|
| `bot/web_search/queries.py:173` → QueryTask; `worker.py:181` → query_task | Источник читает готовые constraints, country, place_level, blocked_hosts и search_plan; отдельного parser задачи нет |
| `bot/web_search/worker.py:191` → __init__ | Опциональная коллекция ListingSource, по умолчанию пустая (фаза 2) |
| `bot/web_search/worker.py:337` → _new_round, ветка `used_count == 0` на 339 | После `_with_search_plan`, до `_enqueue_portal_urls` вызвать новый `_from_sources`; обычные запросы создаются и после ошибки/пустого ответа источника (фаза 2) |
| `bot/campaign/runner.py:1352` → _web_stage; сборка worker на 1402, closers на 1405 | Settings создаёт включённые источники, передаёт worker; каждый aclose добавляется в shutdown (фаза 2) |
| `bot/web_search/models.py:150` → PageResult | Дополнить via и layer значением `api`, остальные defaults прежние (фаза 1) |
| `bot/web_search/store.py:86,97` → WebStore.enqueue/begin_fetch; Postgres на 340/427; Memory на 996/1084 | Фаза 3: явный API-контекст постановки/claim, разрешающий повтор failed и обход HTML-блока; источник paused/deleted остаётся недоступен |
| `bot/web_search/store.py:474,1107` → finish_fetch | Фаза 3: успешный via=api учитывать как чтение; тот же transactional post/run/claim/queue путь |
| `bot/web_search/structured.py:104` → facts_block | Существующий сериализатор одной строки JSON-LD для API-поста |
| `bot/web_search/worker.py:708` → _scrape; `scrape_api.py:26` → ScrapeApiClient; `settings.py:151` → scraper | Фаза 4: query auth token, geo/render/super, billing metadata; сохранить bearer режим |
| `bot/web_search/worker.py:710` → _set_layer(api); `models.py:18` → WebProgress | Фаза 3: убрать коллизию status API, см. ниже |
| `bot/campaign/metrics.py:21,58,83,150`; `summary.py:28,56,67,91,120`; `final_report.py:63,440,543` | Фаза 5: pages_api, явное происхождение в строке сайта, расходы по api/scrape |
| `bot/services/db/migrations/039_campaign_metrics.sql:16` | CHECK слоя расширяет только новая миграция 043; 039 не редактировать |

## Контракт источника (фаза 1)

`bot/web_search/sources/__init__.py` экспортирует оба контракта; реализацию держать в
`base.py`. Зависимости: stdlib и QueryTask; импорт не создаёт сетевые клиенты.

```python
from dataclasses import dataclass
from typing import Protocol
from bot.web_search.queries import QueryTask

@dataclass(frozen=True, slots=True)
class SourceListing:
    url: str
    title: str
    price: float | None = None
    currency: str | None = None
    area_m2: float | None = None
    rooms: int | None = None
    address: str | None = None
    property_type: str | None = None
    deal: str | None = None
    description: str = ""
    plot_m2: float | None = None

class ListingSource(Protocol):
    name: str
    hosts: frozenset[str]
    def supports(self, task: QueryTask) -> bool: ...
    async def search(self, task: QueryTask, *, limit: int) -> list[SourceListing]: ...
    async def aclose(self) -> None: ...
```

`plot_m2` добавлен к ТЗ: площадь участка — отдельный факт. Поле в конце сохраняет порядок
аргументов исходного SourceListing. Контракт структурный (Protocol), без регистрации
глобальных singleton. `name` — стабильное имя источника для журнала, не название хоста.
`hosts` — явная область выдачи; URL в данных актора обязательно сверяется с ней.

SourceListing содержит факты ответа, не критерии запроса. Нормализатор не заполняет
неизвестную сделку, площадь или город из желаемых фильтров. Булевы значения, отрицательные
и не finite числа не становятся ценой/площадью; rooms — положительное целое.
В первой фазе это контракт, валидация внешнего payload — задача Apify-адаптера фазы 2.
`area_m2` следует существующей семантике structured: построенная/общая площадь,
для достоверно земельного объекта допустима площадь участка; `plot_m2` только участок.
Из `constructedArea`/`usableArea` нельзя делать plot_m2.

## Первый раунд, идемпотентность, лимиты (фазы 2–3)

1. `_with_search_plan` возвращает актуальный campaign; затем `_from_sources(campaign)`
   получает `query_task(campaign)` и проверяет supports/blocked_hosts/config/budget.
2. До запуска учитывать оставшиеся campaign/day/host лимиты из counts/usage/host_attempts;
   `limit` не больше APIFY-лимита и доступного остатка. Это лимит выдачи источника, не
   обещание, что Apify не создаст больше строк или не спишет больше.
3. Источник работает только в ветке первого раунда, раньше HTML. `used_count == 0`
   сам по себе не гарантирует один платный запуск: worker может упасть до add_queries,
   generator вернуть 0 или все queries попасть в conflict. Требуется долговечный claim
   `(campaign_id, source_name)` и run_id с возможностью возобновления существующего run.
   Claim хранить в БД, не в set worker. Конкретное добавление схемы подготовить фазой 2
   новой миграцией после 043; не менять уже применённые. In-progress claim не запускает
   нового актора; завершённый/failed claim означает обычный fallback, без второго запуска.
   Доказательство: рестарт и повтор `_new_round(..., 0)` не стартуют актор дважды.
4. Ограничить timeout внешнего запуска/ожидания: меньше campaign lease_seconds либо
   обновлять lease на долгом polling. Event loop не блокируется: HTTPX async или async
   SDK, синхронный SDK только через asyncio.to_thread. Shutdown закрывает клиента.
5. Каждую полученную строку нормализовать, URL проверить fetchable, hosts, blocked_hosts,
   listing path, сделку и страну. Не делать HTML-запрос для получения готовой API-карточки.
   Ошибки схемы/URL журналировать безопасным кодом и числом, без dump payload/токена.
6. Ставить Candidate(depth=0, kind=listing), затем брать QueuedUrl именно по url_key,
   `begin_fetch` с API-контекстом, затем `finish_fetch(ticket, PageResult(...,
   via="api", layer="api"))`. `finish_fetch(via=..., layer=...)` — неверная сигнатура.
   Не выбирать произвольный next_urls: там round-robin и могут оказаться HTML-URL.
   Внешний актор стартует без открытой DB-транзакции; ticket — короткая транзакция
   локального импорта готовой строки. BUSY оставляет строку для возобновления импорта
   сохранённого dataset/run; не терять её и не запускать актор вновь.
7. Вернуть обычную генерацию запросов и enqueue прямых index URL. API не заменяет
   остальные обязательные порталы. counts после импорта перечитать: исходные значения
   `_advance` уже устарели; обычные page/day лимиты должны учитывать API.

## Сохранение и JSON-LD

Пост строится через `facts_block` и начинается с одной законченной JSON-строки:

```text
JSON-LD: {"url": "https://www.idealista.com/inmueble/123456789/", "price": 180000, "currency": "EUR", "area_m2": 2500, "plot_m2": 2500, "address": "Madrid, España", "property_type": "landparcel", "deal": "sale"}
Terreno en venta en Madrid
Описание из ответа источника.
Ссылка: https://www.idealista.com/inmueble/123456789/
```

Это иллюстративные данные, не полученная карточка/fixture актора. Отсутствующие факты
не сериализовать как подтверждённые. Подтверждённый land маппить в `landparcel`, сделку
в sale/rent. JSON-строку сохранять полностью, обрезать сначала описание/текст до
max_post_chars; нельзя разрезать JSON посередине. Title/URL остаются видимыми.
`rooms` передаётся, если есть; для земли не выдумывать 0 комнат.

`prefilter._jsonld/listing_area` (`bot/analysis_pipeline/prefilter.py:87,120`) читают
первую строку и plot_m2; `OpenRouterAnalyzer`/`verify_facts` (`openrouter.py:313,406`)
понимают существующий facts format. Store пишет raw_payload.via и campaign_id
(`store.py:162`), normalised post, acquisition run, web_seen_urls и campaign URL.
Далее обычные analysis → findings → runner._judge (`runner.py:676`) → tolerance →
dedup (`runner.py:605`) → reporting. Напрямую findings/outbox не вставлять.

## Store и failed URL (фаза 3; не реализовывать в foundation)

Добавить keyword `layer` в enqueue и begin_fetch с прежним HTML-default; api явно
передаёт `layer="api"`. Один источник paused/deleted не разрешён даже через API.
HTML-отказы хоста и human verification не блокируют импорт структурированного API:
`_site_source` получает contact_site=False для api; это не снимает ручную паузу.

Для layer=api enqueue разрешает global `web_seen_urls.state='failed'`, а begin_fetch
атомарно перехватывает только failed или stale fetching; fetched остаётся duplicate,
свежий fetching не перехватывается. Кроме глобальной строки нужна обработка уже
существующей `(campaign_id,url_key)` failed/duplicate, иначе ON CONFLICT DO NOTHING
запретит повтор в той же кампании. Возвращать в queued разрешено только при failed
глобальном claim; не возрождать успешно прочитанные URL, capped/robots/операторскую паузу.
Memory и Postgres должны иметь одинаковые правила. failed → api → fetched не создаёт
два поста; content_hash/URL unique сохраняются. Fetched snippet (via=search), хотя
неполный, уже имеет state=fetched: его upgrade — отдельная задача, здесь не разрешён.

`finish_fetch` сейчас считает `fetched = ok and via == 'page'` (`store.py:488`),
`contacted` также привязан к via=page. Memory (`store.py:1125`) успешный via=api
отнесёт к failed хоста. Фаза 3 расширяет обе ветки: успешный api — read/fetched,
реальные api failures — failed; api не меняет http/render refusals и blocked_until.
`_count_refusal` уже игнорирует api (`store.py:683,1062`), но это проверить тестом,
включая исходно ненулевые отказ/блок. `_note_read` HTML-breaker не вызывать для
source-run errors; Apify 429 не означает отказ Idealista HTML.

## Имена слоёв и отчётность (фазы 3 и 5)

Persistent PageResult.layer: api = структурированный источник, scrape = HTML unlocker.
Status WebProgress.layer: api = Apify, unlocker = Scrape.do; `_scrape` сейчас ставит
api (`worker.py:710`), его переименовать в unlocker, обновить status mapping и тесты.
Не считать 'api' свободным. Stage costs.api и costs.scrape остаются раздельными.

Миграция 043 расширяет только CHECK слоя. В фазе 5 добавить новой аддитивной
миграцией `campaign_metrics.pages_api integer NOT NULL DEFAULT 0
CHECK (pages_api >= 0)`; номер выбрать как последний существующий + 1
(после возможной миграции durable source claim в фазе 2).
Фаза 5 добавляет счётчик во все части REFRESH_SQL, dataclass, metrics_of, pages_read
и технические строки отчёта. Слой scrape в технической строке назван unlocker,
api — Idealista API. SiteReport получает число read_api, агрегируемое в обоих stores;
Site/summary сохраняют host idealista.com и показывают «Idealista (API)», когда API
использован. Для смешанного источника — явное число API среди прочитанных, без
удвоения ссылок/карточек. Простое переименование host во всех случаях ошибочно.
Final_report использует общий site_lines; STAGE_NAMES уже разделяет API порталов /
Scrape API (`final_report.py:63`). Ошибки источника видны через costs.error('api', code).

## Провайдерские gates и стоимость (до фазы 2 / live)

Публичные основания и ссылки: [PROVIDERS.md](PROVIDERS.md), проверка 2026-10-10.
Дизайн не заявляет проверку живого input/output.

- Axly README явно называет country=es, propertyType=lands, operation=sale,
  locationName/locationId, но plot-поля нет. Перед окончательным нормализатором нужны
  текущая input-schema и обезличенный земельный fixture: price/currency, operation,
  propertyType, size, url, address, точность municipality. size в м² неизвестной
  семантики не превращать в plot_m2; до доказательства plot_m2=None. Не заполнять
  address городом запроса. Province Madrid не равно city Madrid.
- Пагинация: README одновременно обещает автоматическую и описывает numPage.
  Проверить где actor-input ограничивает выдачу, termination, pages, max results;
  не приписывать axly startUrl/maxItems других акторов. Dataset pagination отдельно
  ограничивать offset/limit и общее число результатов. Dataset limit ограничивает
  наш импорт, но не затраты актора.
- Первый live gate: сверить конкретную карточку terrenos sale Madrid с выводом,
  записать fixture, доказать plot/город и действующий тариф аккаунта/billable events.
  До этого adapter остаётся выключен и тестируется без сети; никакого обещания
  «все участки» на основе недоказанной пагинации.
- Для Apify terminal SUCCEEDED → dataset; FAILED/TIMED-OUT/ABORTED → безопасная ошибка
  с учётом расходов, 429/5xx/network → TemporaryApifyError, auth/input/schema →
  PermanentApifyError. Timeout локального polling не означает прекращение списаний:
  abort/статус существующего run требуется завершить/сверить. Не запускать новый
  paid run при retry POST с неизвестным исходом. Кампания продолжает обычный поиск.
- Перед каждым внешним вызовом costs.over_budget(). Apify: запись stage=api,
  provider=apify, item=actor, units/цена из run billing, включая unsuccessful run.
  usageTotalUsd после финала может уточниться: повторно сверить один run, не считать
  две записи независимыми расходами. Если цену получить нельзя — безопасная оценка
  с явной отметкой, а не фиктивный фактический $0. Run billing/cap контракт проверить
  по официальной API-схеме при реализации; provider maxTotalChargeUsd обязателен
  для запуска с денежным лимитом.
- Scrape.do GET: params token/url/geoCode=es/render/super, ключ repr=False; не выводить
  полный HTTPX request/exception URL или chain. Снимать Scrape.do-Request-Cost даже
  с ответов, отброшенных по статусу/type/size. Credits × подтверждённая ставка пакета
  = распределённая цена запроса; подписка $29 не равна pay-as-you-go. Header отсутствует
  → оценка WEB_SEARCH_SCRAPE_API_COST_USD с отметкой. Не считать estimate и фактический
  расход дважды; bearer-режим и существующие timeouts/byte caps сохранить.
- `costs.over_budget` (`costs.py:236`) читает прошлые затраты, возвращает False при
  ошибке БД и не резервирует стоимость. Поэтому CAMPAIGN_BUDGET_USD=1 сам по себе
  не доказывает smoke ≤$1. До шага 6 нужны DB-координация/резерв общего бюджета всех
  сервисов, upper bounds каждого вызова, fail-closed при недоступном budget state,
  provider-side cap и остаток на analysis/reviewer. Эти меры не входят в фазу 1;
  без них live smoke заблокирован. Default-off позволяет безопасно собрать foundation.

## Объём фазы 1 и критерии готовности

Только contracts в sources/base.py и __init__.py, Literal api в PageResult,
миграция `043_listing_sources.sql` (только расширение CHECK слоя), оба списка
`scripts/apply_migrations.sh`. При переносе схемы — расширить CHECK конкретного
layer, не удалить посторонние CHECK. Миграция идемпотентна и сохраняет прежние слои.
Новые settings/зависимости/network/worker/store retry не нужны в foundation.

Проверить контракты и frozen/default/plot_m2, поддерживаемые PageResult слои и
round-trip api через finish_fetch в Memory и реальном Postgres. Foundation-тест
не объявляет корректными счётчики хоста, исправляемые фазой 3: отдельно проверить
сохранение post/raw_payload/layer и отсутствие влияния на refusal/block. SQL-тест:
api допустим, неизвестный layer запрещён, старые http/render/scrape/none и NULL
допустимы, повтор 043 безопасен. Проверка default 0/неотрицательного pages_api —
задача фазы 5 вместе с соответствующей новой миграцией.
Использовать существующий tests/test_web_search_postgres.py и соответствующие
Memory-тесты, tests/test_project_foundation.py/test_image_imports.py для упаковки.

После фазы 0 и после фазы 1: полный pytest с Postgres без skipped, ruff check bot tests,
золотые тесты; зафиксировать реальные результаты в handoff. Отметка дизайна в PLAN
не означает реализацию фаз 1–5 или пройденный smoke. Финальный CHECKLIST остаётся
незакрытым до соответствующих фаз. После фазы 1 показать foundation и ждать «ок».

## Уточнение фаз 2–3 после разрешения владельца (2026-10-10)

Владелец ответом «ok» разрешил фазы 2–3 после приёмки foundation. Контрольная
остановка остаётся после фазы 3; платные вызовы и VPS не разрешены.

Публичная [input schema axly](https://apify.com/axlymxp/idealista-scraper/input-schema)
проверена повторно: обязательны country/locationName/locationId/propertyType/operation;
maxItems — 1–50 на страницу, numPage начинается с 1. Реальный земельный output не
получен. Адаптер ограничивает импорт одной выдачей/50 строками и не обещает полноту
поиска; size не попадает ни в area_m2, ни в plot_m2 без доказанной семантики. Город и
locationId задаются явно; пустой ID отключает поддержку задачи, ID не угадывается.

Подтверждённые интерфейсы:

- `SourceRunContext` и `source_run_scope` в `sources/apify.py` сохраняют Protocol
  search(task,limit). Контекст содержит campaign_id/source_name, run_id/dataset_id,
  launch_allowed, верхний денежный cap и callbacks save_run/reconcile_usage.
- `save_run(run_id,dataset_id)` сохраняет ответ POST сразу до polling. Только новый
  атомарный claim разрешает POST. Existing starting без run_id — неопределённый
  исход, безопасная ошибка/fallback без второго POST; known run возобновляется GET.
  Это at-most-once запуск, не обещание exactly-once внешней операции.
- Миграция 044 хранит `(campaign_id,name)` и состояние starting/running/ready/
  completed/failed, provider run/dataset, snapshot SourceListing и import_offset.
  SourceRun.listings — tuple[SourceListing,...]. WebStore методы claim_source,
  source_runs, save_source_run, source_ready, advance_source_import, finish_source
  одинаковы для Memory/Postgres. Никаких HTTP внутри SQL-транзакции.
- `_from_sources` создаёт claim только в первом раунде после planner. В `_advance`
  отдельная обработка возобновляет существующий run или готовый snapshot независимо
  от количества созданных запросов. BUSY оставляет offset, не запускает actor вновь
  и не позволяет HTML-worker забрать тот же queued URL. Сбой провайдера явно
  журналируется и оставляет обычный поиск работоспособным.
- `costs.record_unique(..., key=listing-source:campaign_id:name)` обновляет одну
  запись абсолютной стоимости вместо повторного суммирования на resume. Миграция
  044 добавляет nullable unique idempotency_key в campaign_costs. Обычный record
  не меняется. Фактическое usageTotalUsd заменяет явно помеченную оценку; неизвестный
  исход POST учитывается консервативной оценкой cap, без объявления её реальной ценой.
  Однозначный отказ POST 4xx — error, не фиктивный расход полного cap.
- Максимальная цена запуска ограничена настройкой и читаемым остатком бюджета;
  недоступность budget-state запрещает source launch. Это ещё не резерв общего
  бюджета всех сервисов; гарантия live ≤$1 остаётся отдельным gate. Cleanup GET/abort
  уже начатого run допускается для остановки и сверки списаний после budget hit.
- Phase2 использует настоящий via/layer=api, contact_site=False при импорте свежих
  URL. Accounting/retry семантика store меняется только отдельной фазой 3. Статус
  unlocker заменяет прежнюю scrape-метку api фазой 3; persistent scrape не переименован.

Новые настройки (Phase2): APIFY_IDEALISTA_ENABLED=false, APIFY_TOKEN (repr=False),
APIFY_IDEALISTA_ACTOR, APIFY_IDEALISTA_MAX_RESULTS<=50, APIFY_IDEALISTA_LOCATION_NAME,
APIFY_IDEALISTA_LOCATION_ID (пустой по умолчанию), APIFY_IDEALISTA_TIMEOUT_SECONDS<=120,
APIFY_IDEALISTA_MAX_CHARGE_USD>0. Все передаются только campaign-runner, задокументированы
и включены в .env.example с комментариями. Сеть тестируется HTTPX mock, fixtures
синтетические и не объявляются проверенными данными Idealista.

Уточнение lifecycle фазы 2: save_run callback предварительно записывает estimated_pending_run; это обеспечивает видимость расхода при crash/time_cap. При time_cap/cancel worker вызывает settle существующего run (GET/abort/billing, ≤90 s), не запускает actor и не импортирует новые данные. Недоступный/выключенный провайдер оставляет явную оценку и ошибку, а не обещание известного факта.
