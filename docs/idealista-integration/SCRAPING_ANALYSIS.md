# SCRAPING_ANALYSIS — как устроен поиск по сайтам и почему провалился последний прогон

Дата: 2026-10-09. Ветка `claude/optimistic-feynman-y4ayed`, база — `main` @ `06988dc`.
Источник фактов — код репозитория. Доступа к БД и логам VPS нет, поэтому цифры прогона (≈$15, 0 отправлено,
31/37, 54, 14) взяты из описания задачи. Всё, что требует данных прогона, помечено **«нужны данные»**,
и к каждому такому пункту приложен SQL для проверки на VPS (раздел 5).

Проверка гипотез — скрипт `repro.py` в scratchpad. Он вызывает настоящие функции из `bot/` (без моков и
без сети). Вывод приведён в разделе 3. После шага 3 эти проверки станут unit-тестами.

---

## 1. Карта пайплайна

Сервисы: **campaign-runner** (`bot/campaign/runner.py`, в нём крутится web-worker, собирается в
`runner.py:1335-1393`) и **analysis-worker** (`bot/analysis_pipeline/main.py --serve`, `docker-compose.yml:343-348`).
Связь между ними только через БД: `collected_posts` → `findings` → `campaign_findings`.

| # | Этап | Где | Что происходит |
|---|------|-----|----------------|
| 0 | План поиска | `worker.py:304-338` `_new_round` → `_with_search_plan` (`architect.plan_with_model`, Sonnet, один раз) → `_enqueue_portal_urls` (`worker.py:340-348`) | В первом раунде ставит прямые URL порталов из плана в очередь (depth 0, index) |
| 1 | Запросы | `queries.py:717-736` `FallbackQueryGenerator`: план → LLM (`OpenRouterQueryGenerator`, `queries.py:492-551`, gpt-4o-mini) → шаблоны (`TemplateQueryGenerator`, `queries.py:646-687`); затем `localise` (`queries.py:375-395`) и `cover_portals` (`queries.py:469-489`) | 12 запросов за раунд, до 80 на кампанию (`web_search/settings.py:45-46`), до 8 раундов |
| 2 | Поиск ссылок | `worker.py:350-386` `_search` → `SearxngClient.search` (`searxng.py:53-90`), опционально `MergedSearcher` с Google CSE / SerpAPI (`search_backends.py:145-206`) | Фильтры выдачи: `fetchable` (`urls.py:88-103`), `deal_conflict` только для известных порталов (`worker.py:369`), `listing_evidence` для неизвестных (`worker.py:372-375`, `urls.py:350-360`), чужой TLD/страна (`worker.py:376-379`) → `store.enqueue` (`store.py:339-367`) |
| 3 | Fetch по слоям | `worker.py:390-469` `_read_pages` → `_read` (`worker.py:514-546`): **http** (`fetcher.py` + curl_cffi impersonate) → **render** (браузер, `_render_refused` `worker.py:597-625`) → **scrape** API (`_scrape` `worker.py:627-642`, `ScrapeApiClient` `scrape_api.py:26-60`) | Учёт отказов по слоям хоста: `store._count_refusal` (`store.py:682-707`), 3 отказа подряд блокируют слой на 12 ч (`store.py:45-46`). Если сайт не отдал страницу, вместо неё берётся сниппет из выдачи (`worker.search_result`, `worker.py:779-795`) |
| 4 | Сохранение | `store.begin_fetch` (`store.py:426-471`) / `finish_fetch` (`store.py:473-518`) | Пост попадает в `collected_posts`, слой — в `web_campaign_urls.layer` (CHECK `http/render/scrape/none`, `039_campaign_metrics.sql:16`). Глобальный дедуп URL через `web_seen_urls` (`url_key`) |
| 5 | Analysis (LLM) | `analysis_pipeline/main.py:43-86` `analyse_batch` → `pipeline.py:26-43` → дешёвый `filter_evidence` (`filters.py:102-121`, только ключевые слова) → `OpenRouterAnalyzer.analyze` (`openrouter.py:400-449`, **Sonnet 4.5**, текст до 12 000 символов) → `verify_facts` (`openrouter.py:307-336`) | Из страницы получается `findings.structured_payload`: цена, площадь, сделка, тип объекта, цитаты |
| 6 | Tolerance | `runner.py:668-699` `_judge` → `tolerance.classify` (`tolerance.py:308-380`) → AI-проверка `_relevance` (`runner.py:708-742`, reviewer на **Sonnet 4.5**, лимит 300 вызовов на кампанию) → `review_match` (`relevance.py:369-405`) | Корзины: exact / similar / other / excluded. Причина хранится в `campaign_findings.why` (`relevance.py:347-361`) |
| 7 | Dedup | `runner.py:597-635` `_deduplicate` → `campaign/dedup.py` `same_object` | Один объект с нескольких сайтов → одна карточка с «Также на» |
| 8 | Reporting | `runner.py:403-410`: `_metrics` (`metrics.py` `REFRESH_SQL`), `_summary` (`summary.py`), `_final_report` (`final_report.py:472-508`, рекомендации на Sonnet) | Отчёт пользователю: отправлено / похожие / отклонено по причинам / по сайтам |

---

## 2. Сверка ТЗ пакета с кодом

| Что предполагает ТЗ / PLAN | Как в коде | Статус |
|---|---|---|
| `QueryTask` | `queries.py:170-230`, frozen dataclass (`constraints`, `country_code`, `search_plan`, `blocked_hosts`) | ✅ совпадает |
| `WebSearchWorker._new_round` — точка входа «первого раунда» | `worker.py:304-324`; первый раунд — ветка `used_count == 0` (`worker.py:306-308`) | ✅ совпадает |
| `_from_sources` | Такого метода нет | 🆕 новый |
| `ListingSource` / `SourceListing`, `bot/web_search/sources/` | Нет | 🆕 новый |
| `ScrapeApiClient` «уже есть, используем его» | Есть, `scrape_api.py:26-60`, но протокол — `GET {url}?url=<page>` + `Authorization: Bearer`. Scrape.do принимает ключ параметром `token` (уточню на шаге 4) | ⚠️ расходится: нужна небольшая доработка (ключ в query или задание параметров через URL) |
| `store.finish_fetch(via="api", layer="api")` | Сигнатура — `finish_fetch(ticket, result: PageResult)` (`store.py:473`); `via` и `layer` — поля `PageResult` (`models.py:166,169`): `via ∈ page/search/index`, `layer ∈ http/render/scrape/none` | ⚠️ расходится: нужно расширить Literal-типы, а не добавлять kwargs |
| Миграция расширяет CHECK `layer` значением `'api'` | CHECK есть: `039_campaign_metrics.sql:16` (`http,render,scrape,none`) | ✅ верно, но номер не 042 (см. ниже) |
| Номер миграции `042` | Последняя миграция — `041_web_verification.sql`. Если на шаге 3 понадобится миграция под учёт расходов, Idealista получит **043** | ⚠️ уточняется по факту |
| `begin_fetch` повторно берёт `failed` URL при `layer="api"` | `begin_fetch` (`store.py:426`) не принимает `layer`. Claim в `web_seen_urls` перехватывается только для зависшего `fetching` или устаревшего index (`store.py:453-463`). Кроме того, `enqueue` уже помечает такой URL как `duplicate` (`store.py:358-362`) | ⚠️ нужно менять **и** `enqueue`, **и** `begin_fetch` (и `MemoryWebStore`) |
| «via=api не увеличивает счётчики отказов хоста» | `_count_refusal` считает только `http`/`render` (`store.py:688-702`), поэтому новый слой и так не блокирует хост. Но `pages_fetched/last_fetch_at` обновятся (`store.py:510-513`) | ✅ почти даром; нужен тест |
| Слово `"api"` свободно | Уже занято: `WebProgress.layer="api"` — это **scrape API** в статусе (`worker.py:629`, `models.py:21`) | ⚠️ конфликт имён: для Idealista нужна другая метка в статусе или переименование scrape → `unlocker` |
| В отчётах «Idealista (API)» | Имена сайтов в отчёте: `summary.site_name`/`site_lines`; учёта слоя в отчёте пользователю нет, только `metrics.py` (`pages_http/render/scrape`) | 🆕 новый |
| Акторы `azzouzana/idealista-scraper`, `dz_omar/idealista-scraper-api` | В коде нет. Проверю на шаге 4 (веб) | ❓ |
| Settings / `.env.example` | Настройки web-стадии — `bot/web_search/settings.py`, compose передаёт их из `.env.example` (тест `test_deployment.py`) | ✅ место понятно |
| `.claude/skills/real-estate-pipeline`: «следующая миграция — 031» | Устарело (уже есть 041) | ⚠️ поправить заодно |

---

## 3. Диагноз прогона «участок, Мадрид, покупка, ≥2000 м²» (~$15, отправлено 0)

### Вывод проверочного скрипта (настоящие функции `bot/`)

```
== H1: dictionary/science hits vs filters (fetchable, listing_evidence)
  dictionary.cambridge.org     fetchable=True known=False evidence=True
  es.pons.com                  fetchable=True known=False evidence=True
  www.ingles.com               fetchable=True known=False evidence=True
  www.fao.org                  fetchable=True known=False evidence=True
  www.mdpi.com                 fetchable=True known=False evidence=True
  land.copernicus.eu           fetchable=True known=False evidence=False
== H3: JSON-LD area: house with floorSize (built) + lotSize (plot)
  area_m2 from JSON-LD: 180 (lotSize was 2500)
  _area('2.000 m²') = 2000
  _area('2,5 ha') = 25000.0
  _area('2ha') = 2
  min_area_of: [2000.0, 2000.0, 2000.0, 2000.0, None, 2000.0]   # 5-й: «участок 2000 м² и больше»
== H2: unknown area against a minimum -> counted as 'unverified'
  classify -> similar area_unknown -> report category: unverified
  area_m2=2.0 (LLM read '2.000' as 2.0) -> excluded area
== H4: deal in URL paths
  deal_conflict(sale)=1 known=True   pisos.com/alquilar/...
  deal_conflict(sale)=1 known=True   milanuncios.com/alquiler-de-...
  deal_conflict(sale)=0 known=False  inmo-xyz.es/terrenos/arrendamiento-...
  deal_conflict(sale)=0 known=False  inmo-xyz.es/renta/terreno-...
```
Плюс отдельная проверка регулярки из `openrouter.py:297-304`: в тексте «parcela de 2.000 m2» площадь `2.0`
**проходит** `verify_facts`, потому что «2» из «m2» считается подтверждением.

### Гипотеза 1. В поиск попадают словари и научные сайты — ✅ ПОДТВЕРЖДЕНА (механизм в коде)

- **Allowlist доменов нет.** Неизвестный хост отсекается только если сниппет не похож на объявление:
  `worker.py:372-375` → `listing_evidence` (`urls.py:350-360`) = «слово-недвижимость» + (цифра площади/цены
  **или слово сделки**). Заголовок «Terreno en venta | Traductor … inglés.com» содержит и «terreno», и «venta»,
  поэтому проходит. FAO/MDPI проходят по «land/parcel» + «2000 m2».
- **Чёрный список неполный:** в `NON_LISTING_HOSTS` (`urls.py:31-46`) из словарей есть только rae/wordreference/linguee/reverso.
  Cambridge, PONS, ingles.com/SpanishDict, fao.org, mdpi.com, copernicus там нет.
- Такой сайт получает до **5 запросов страниц** (`max_pages_per_unknown_host`, `worker.py:406-410`), и каждая
  прочитанная страница идёт в **Sonnet** (`filter_evidence` пропускает её по слову «terreno», `filters.py:116`).
- **Как строятся запросы.** Тип сделки и площадь гарантированно есть только в шаблонных и `portal_query`
  (`queries.py:457-466`, `582-588`). Запросы LLM `localise` проверяет только на «место + слово-недвижимость»
  (`queries.py:385-393`); слово сделки там не обязательно. Промпт прямо просит треть запросов без `site:`
  (`queries.py:320`).
- **Нужны данные.** Почему поисковик вообще вернул словари. Две версии: (а) запросы LLM вида «parcela 2000 m2 Madrid»;
  (б) Google/Bing банят IP VPS, и отвечают только mojeek/qwant (`docker/searxng/settings.yml:13-20`), которые
  частично игнорируют `site:`. Проверка — лог `web_search.engines_silent` (`searxng.py:89`) и SQL №1.

### Гипотеза 2. 31 из 37 «не удалось проверить: ключ ИИ и лимиты» — ⚠️ ЧАСТИЧНО ОПРОВЕРГНУТА: сообщение вводит в заблуждение

- Плашка «⚠️ … проверьте ключ ИИ и лимиты» (`final_report.py:436-439`) появляется, когда больше 80 % находок
  имеют `why='unverified'` (`final_report.py:95-98`, `105-125`).
- Но `why='unverified'` записывается **в трёх разных случаях**:
  1. **Площадь неизвестна при заданном минимуме** (`tolerance.py:343-344` → `area_unknown` → категория
     `unverified`, `relevance.py:348`). К LLM это отношения не имеет. Для запроса «≥2000 м²» сюда попадает **каждая**
     карточка из сниппета (Idealista/Fotocasa отдают 403, а в сниппете обычно нет площади).
  2. Reviewer ответил «unknown» по критерию, например «площадь не подтверждена» (`relevance.py:400-402`). Это тоже не сбой.
  3. Настоящий сбой или лимит AI-проверки (`runner.py:691`, `UNVERIFIED_FAILED/CAP`, `runner.py:236-237`).
- В отчёте все три случая сливаются в одну цифру, и вывод «ключ ИИ» для случаев 1–2 ложный. Скорее всего, большая часть
  из 31 — случаи 1–2. **Нужны данные:** SQL №2 разделяет их по `hold_reason`: у случая 1 он NULL, у случая 3
  начинается с «Не проверено ИИ».
- **Отдельная тихая потеря в analysis-worker:** при ответе модели вне схемы или при 400/404 от OpenRouter
  (`openrouter.py:434-449` → `ValueError`) пост закрывается как `rejected` **без finding и без причины**
  (`main.py:69-72` → `store.finalize(..., accepted=False)` `store.py:121-132`). В отчёт он не попадает вообще.
  Ошибки 401/402/429 (ключ, баланс, лимит) и сетевые ошибки возвращают пост в очередь и повторяют **бесконечно**
  (`main.py:78-82`, `openrouter.py:25`), без счётчика и без сообщения в отчёте.

### Гипотеза 3. 54 отказа «не та площадь» на Pisos.com — ⚠️ ЧАСТИЧНО ПОДТВЕРЖДЕНА (3 дефекта найдены, доля — нужны данные)

1. **parcela vs construida: дефект.** `structured._listing` берёт площадь в порядке
   `floorSize, size, area, lotSize` (`structured.py:193`), то есть для дома с участком в `area_m2` попадёт **построенная**
   площадь (180 вместо 2500). В промпте LLM сказано, что JSON-LD — авторитетный источник, и «area_m2: plot or built area»
   (`openrouter.py:58,87,107-108`), без приоритета участка для land. Отсюда `excluded area`
   (`tolerance.py:236-238`).
2. **«2.000 m²»: разделитель тысяч.** В детерминированных парсерах всё верно (`_area('2.000 m²')=2000`, `_amount`,
   `min_area_of`). Но если LLM вернёт `2.0`, `verify_facts` его **не отбросит** из-за «m2» в тексте
   (`openrouter.py:303-304`), и находка уйдёт в `excluded area`.
3. **Гектары:** `_area('2ha') = 2` (`structured.py:288`, срабатывает только «… ha» с пробелом); `unitCode HAR` работает.
   `min_area_of('участок 2000 м² и больше') = None` (`tolerance.py:154-160`, нет формы «N м² и больше»), но для
   этого кейса `min_area` из плана перекрывает (`tolerance.py:279`).
- Часть из 54 — честные отказы: на Pisos много участков меньше 2000 м², а площадь в URL/запрос не передаётся
  (у pisos.com нет фильтра площади в пути). **Нужны данные:** SQL №3 (площадь, тип объекта, источник цифры).

### Гипотеза 4. 14 отказов «аренда вместо покупки» — ✅ ПОДТВЕРЖДЕНА частично (доходит не везде)

- В шаблонные и `site:` запросы сделка попадает (`queries.py:462,667`). В запросы LLM — только как данные
  (`queries.py:514`), промпт её не требует, `localise` не проверяет.
- Фильтр по URL `deal_conflict` работает **только для известных порталов** (`worker.py:369`) и только по словам
  из `_RENT_WORDS` (`urls.py:282`). Нет `arrendamiento`, `renta`, `alquilo`, `rental`, `lloguer`.
- Прямые URL порталов из плана ставятся в очередь **без** проверки сделки (`worker.py:345-346`).
- Сами 14 отказов — правильная работа tolerance (`tolerance.py:315-317`), но каждый из них сначала прошёл через
  Sonnet. Дешёвый фильтр сделки до LLM отсутствует.

### Гипотеза 5. Куда ушли деньги — ❌ УЧЁТА НЕТ

- В коде нет учёта стоимости ни LLM, ни поиска, ни fetch. Единственное исключение — голосовые (`control_plane/service.py:368`).
  Ответ OpenRouter `usage` не читается ни в `openrouter.py`, ни в `agents/llm.py`.
- Денежного бюджета на прогон нет. Есть только счётные лимиты: страницы 400 (`web_search/settings.py:53`), рендеры 60, scrape 40
  (`web_search/settings.py:92`), reviewer 300, relevance 2000 (`campaign/settings.py:79`).
- **Оценка по коду** (цены OpenRouter: Sonnet 4.5 $3/M вход, $15/M выход; gpt-4o-mini $0.15/$0.60):

| Этап | Модель / сервис | Вызовов за прогон | ≈ $ за вызов | ≈ $ |
|---|---|---|---|---|
| analysis (каждая прочитанная страница/сниппет) | Sonnet 4.5, вход ≈ 3–5k ток., выход ≈ 0.5–1k | 200–400 | 0.02–0.03 | **4–12** |
| reviewer (каждая находка, не отсечённая правилами) | Sonnet 4.5 | до 300 | 0.02–0.03 | **1–6** |
| план, финальный отчёт, интервью | Sonnet 4.5 | 5–15 | 0.02–0.05 | 0.2–0.6 |
| запросы | gpt-4o-mini | ≤ 8 | <0.001 | ~0 |
| поиск (SearXNG) | бесплатно; CSE/SerpAPI — только если включены | — | — | 0 / ? |
| scrape API | если `WEB_SEARCH_SCRAPE_API_URL` задан | до 40 | зависит от провайдера | 0 / ? |
| при `AGENT_REDUCTION_ENABLED=true` | Opus + Jev на **каждый** пост | до 1000/день | 0.05–0.1 | **может быть основным** |

  Вывод: ~$15 правдоподобно набираются за счёт **analysis на Sonnet по всем страницам**, включая словари, аренду и
  мелкие участки, плюс reviewer. **Нужны данные:** SQL №4 (сколько постов прошло analysis, по доменам) и выгрузка
  activity из OpenRouter за дату прогона. Отдельно проверить на VPS `AGENT_REDUCTION_ENABLED`.

### Гипотеза 6. Ретраи по заблокированным доменам (403) — ✅ ПОДТВЕРЖДЕНА, худший случай — scrape API

- На один URL: robots.txt (кэш 12 ч) → http → 403 → браузер (`worker.py:578-584`) → scrape API, если включён (`worker.py:585-589`).
- По хосту: 3 отказа подряд на слое блокируют этот слой на 12 ч (`store.py:45-46`, `682-707`). Без scrape API хост
  тратит **3 URL × 2 слоя = 6 попыток** (до 60 с на рендер каждая), дальше остаются только сниппеты. Это нормально.
- **Со scrape API circuit breaker'а нет:** «The scrape API has no block: with it on, never HOST_BLOCKED»
  (`store.py:431-433`, `731`). Каждый следующий URL DataDome-сайта (Idealista) снова идёт в **платный** unlocker,
  до 40 за кампанию (`worker.py:510-511`), даже если все предыдущие вернули 403/captcha. `layer="scrape"` в
  `_count_refusal` не считается (`store.py:701-702`).
- Включён ли scrape API на VPS — **нужны данные** (`.env`, SQL №5).

---

## 4. Что сломано, кратко (вход для шага 3)

| # | Дефект | Где | Подтверждено |
|---|---|---|---|
| D1 | Нет allowlist; неизвестные сайты проходят по «слово + сделка» | `worker.py:372-375`, `urls.py:350-360` | скрипт H1 |
| D2 | Плашка «ключ ИИ» считает `area_unknown` и reviewer-unknown как сбой ИИ | `final_report.py:95-98,436-439`, `relevance.py:348` | скрипт H2 + код |
| D3 | analysis-worker молча теряет пост при ValueError; бесконечные ретраи при 401/402/429 | `main.py:69-82` | код |
| D4 | JSON-LD: построенная площадь важнее участка | `structured.py:193` | скрипт H3 |
| D5 | `verify_facts` принимает число по «m2» | `openrouter.py:303-304` | regex-проверка |
| D6 | `_area('2ha')`; `min_area_of` не понимает «N м² и больше» | `structured.py:288`, `tolerance.py:154-160` | скрипт H3 |
| D7 | Сделка не обязательна в запросах LLM; `deal_conflict` только для известных порталов и узкий словарь | `queries.py:385-393`, `worker.py:369`, `urls.py:282` | скрипт H4 |
| D8 | Нет дешёвого префильтра до Sonnet (площадь/сделка/тип по сниппету и JSON-LD) | `filters.py:102-121` | код |
| D9 | Нет учёта денег и бюджета на прогон | — | код |
| D10 | Нет circuit breaker на слое scrape | `store.py:431-433,731` | код |

## 5. SQL для проверки на VPS (только чтение)

```sql
-- подставить id кампании
\set cid '00000000-0000-0000-0000-000000000000'
-- 1. Хосты из выдачи: известные порталы vs мусор
select host, count(*) links, count(*) filter (where state='fetched') read, min(search_title) example
  from web_campaign_urls where campaign_id=:'cid' group by host order by links desc limit 40;
-- 2. Из чего на самом деле состоит «unverified»
select cf.why, coalesce(left(cf.hold_reason,40),'<null = площадь не указана>') reason, count(*)
  from campaign_findings cf where cf.campaign_id=:'cid' group by 1,2 order by 3 desc;
-- 3. Отказы по площади: какая цифра и откуда
select p.canonical_url, f.structured_payload->>'area_m2' area, f.structured_payload->>'property_type' type,
       f.structured_payload->'evidence'->>'area' quote
  from campaign_findings cf join findings f on f.id=cf.finding_id join collected_posts p on p.id=f.post_id
 where cf.campaign_id=:'cid' and cf.why='area' limit 60;
-- 4. Сколько постов прошло через analysis, по доменам (≈ число вызовов Sonnet)
select u.host, count(*) posts, count(*) filter (where p.state='rejected') rejected
  from web_campaign_urls u join web_seen_urls s using (url_key) join collected_posts p on p.id=s.post_id
 where u.campaign_id=:'cid' group by 1 order by 2 desc;
-- 5. Слои и отказы
select layer, state, detail, count(*) from web_campaign_urls where campaign_id=:'cid' group by 1,2,3 order by 4 desc;
select host, http_refusals, render_refusals, http_blocked_until, render_blocked_until from web_hosts
 where host in ('idealista.com','fotocasa.es','pisos.com','habitaclia.com','milanuncios.com');
```
Дополнительно: `docker compose logs campaign-runner | grep -E "engines_silent|backend_failed|relevance_failed"` и
`docker compose logs analysis-worker | grep -E "request_refused|unreadable|schema_mismatch|model_unavailable"`.

## 6. Открытые вопросы к владельцу

1. «20 порталов клиента» — это `SPAIN_PORTALS` (`urls.py:165-168`, ровно 20 доменов)? Или у клиента свой список?
2. Allowlist строгий (только 20 порталов + `spec.sources`) или мягкий (неизвестные сайты разрешены, но без LLM, пока
   дешёвый фильтр не найдёт цену и площадь)?
3. Нужен ли бюджет и на analysis-worker? Это отдельный сервис, поэтому общий лимит потребует таблицы-журнала расходов в БД
   (новая миграция, тогда Idealista получит 043).
