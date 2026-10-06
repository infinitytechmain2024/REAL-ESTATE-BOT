# Поиск по сайтам (веб-этап кампаний)

Каждая кампания, кроме групп Facebook, параллельно ищет объявления на сайтах.
Этап работает внутри сервиса `campaign-runner` отдельным циклом
(`bot/web_search`), поэтому не задерживает Facebook и не зависит от него.

## Как это работает

1. **Запросы.** ИИ (OpenRouter, тот же `OPENROUTER_API_KEY`, модель
   `OPENROUTER_WEB_QUERY_MODEL`) по задаче кампании (цель, город, требования)
   пишет раунд из `WEB_SEARCH_QUERIES_PER_ROUND` разных запросов на es/en/ru/uk:
   разные формулировки, синонимы, пригороды, запросы по порталам
   (`site:idealista.com …`, fotocasa, pisos.com, milanuncios, habitaclia …).
   В каждый следующий раунд модели передаются все уже использованные запросы.
   Похожие по смыслу запросы отбрасываются (регистр, ударения, порядок слов,
   окончания, служебные слова; совпадение слов ≥ 75 %). Всего не больше
   `WEB_SEARCH_MAX_QUERIES_PER_CAMPAIGN`. Без ключа или при сбое модели
   запросы строятся по шаблонам.
2. **Поиск.** Запросы уходят во внутренний SearXNG (сервис `searxng`,
   официальный образ, настройки `docker/searxng/settings.yml`: только JSON,
   limiter выключен, наружу порт не открыт).
3. **Дедупликация** (миграция `021_campaign_web_search.sql`):
   - `web_seen_urls` — каждая страница, которую бот когда-либо брал в работу,
     по `url_key` (SHA-256 нормализованного URL: без `www.`, `#…`, `utm_*` и
     т. п.). Страница читается **один раз навсегда**: тот же URL из другого
     запроса или другой кампании пропускается (`duplicate`).
   - `web_campaign_urls` — очередь и журнал кампании: `queued`, `fetched`,
     `failed`, `duplicate`, `capped`, `robots`, `skipped`.
   - `web_hosts` — каждый сайт: его источник, счётчики и блокировка
     слоёв чтения (HTTP, браузер) после трёх отказов подряд (403/429/503,
     капча) на 12 часов, отдельно по каждому слою.
   - `web_search_queries` — запросы кампании (уникальны по смыслу); другая
     кампания может повторить запрос; при `WEB_SEARCH_QUERY_REUSE_HOURS` > 0
     запрос, искавшийся другой кампанией за это время, пропускается.
4. **Чтение страниц.** Только публичные GET-страницы: без логинов, форм и
   cookies, только публичные адреса, `robots.txt` соблюдается (и его
   `Crawl-delay`), не чаще одного запроса к сайту в
   `WEB_SEARCH_HOST_INTERVAL_SECONDS`, таймауты и лимит размера. Страница
   поиска портала (список объявлений) не сохраняется: из неё берутся ссылки на
   конкретные объявления того же сайта (не больше
   `WEB_SEARCH_MAX_LINKS_PER_INDEX`), и читаются они.
5. **Scrapling: данные сайта в JSON** (`bot/web_search/structured.py`).
   Порталы кладут объявления в разметку schema.org JSON-LD
   (`RealEstateListing`, `Offer`, `Apartment`, `House`, `ItemList` …) и
   OpenGraph. Парсер Scrapling (`Selector`, без запросов и браузера) достаёт их
   в плоский JSON: `title`, `url`, `price`, `currency`, `area_m2`, `rooms`,
   `address`, `property_type`, `deal`, `description`. Строка
   «JSON-LD: {…}» ставится в начало текста объявления, и анализ
   сверяет точные цифры сайта с задачей. Ссылки из `ItemList` на странице
   поиска портала идут в очередь первыми.
6. **Слои чтения: HTTP → браузер → (необязательно) scrape API**
   (`bot/web_search/render.py`, `scrape_api.py`, путь Agent Reach).
   - Слой HTTP — обычный GET (имитация браузера, см. ниже).
   - Слой браузера: страница читается ещё раз в сервисе `browser` (отдельный
     профиль `web-search-render` без входа в аккаунты, только публичные адреса,
     одна страница на аренду, таймаут `WEB_SEARCH_RENDER_TIMEOUT_SECONDS`, не
     больше `WEB_SEARCH_MAX_RENDERS_PER_CAMPAIGN` страниц на кампанию). Берутся
     текст, ссылки и JSON-LD. В браузер страница идёт в двух случаях: сайт
     отдал HTTP 200, но текста нет (рисуется JavaScript), или сайт **отказал**
     (403/429/503) либо отдал капчу (200 с текстом «captcha», «datadome», «are
     you a robot», «access denied» короче 400 знаков) — тогда страница
     пробуется в браузере один раз. Отключить второе:
     `WEB_SEARCH_RENDER_ON_REFUSAL=false`; весь браузер:
     `WEB_SEARCH_RENDER_ENABLED=false`. Запрет в `robots.txt` соблюдается
     всегда: такая страница в браузере не открывается.
   - Слой scrape API (последний, по умолчанию выключен): если заданы
     `WEB_SEARCH_SCRAPE_API_URL` (+ `WEB_SEARCH_SCRAPE_API_KEY`) и отказали и
     HTTP, и браузер, делается `GET <URL>?url=<адрес страницы>` с заголовком
     `Authorization: Bearer <ключ>` (общий вид для Zyte / ScraperAPI / Bright
     Data); ответ проверяется как обычная страница (только HTML, лимит размера,
     таймаут, без редиректов). Не больше
     `WEB_SEARCH_MAX_SCRAPE_API_PER_CAMPAIGN` страниц на кампанию (считается в
     БД, `web_campaign_urls.scraped`). Ключ нигде не логируется.
   - Если отказали все слои, остаётся карточка из результата поиска / списка.
   - **Блокировка по слоям** (миграция `034_web_fetch_layers.sql`): три отказа
     подряд на слое блокируют на 12 часов только этот слой сайта
     (`web_hosts.http_blocked_until`, `render_blocked_until`). Пока HTTP
     заблокирован, страницы сайта сразу идут в браузер, без запроса HTTP; когда
     заблокированы все включённые слои, сайт не опрашивается вообще, остаются
     карточки (`blocked_until` — «все слои заблокированы»). Без браузера
     (`WEB_SEARCH_RENDER_ENABLED=false` или нет сервиса `browser`) единственный
     слой HTTP, и поведение прежнее.
7. **Объявление → находка.** Текст объявления записывается в
   `collected_posts` так же, как пост из Facebook: сайт — это
   `monitoring_sources` (platform `website`), чтение — `acquisition_runs`
   внутри веб-пакета кампании (`acquisition_batches.campaign_id`). Дальше всё
   штатно: анализ, карточки (точные / похожие / другие / исключённые) и ссылка
   на конкретное объявление.

Статус для пользователей: «Ищу в интернете…» или «Ищу на сайте fotocasa.es…».
Владельцы видят техническую строку («сайты: запросов 12/40 · страниц 7/60 ·
сайт fotocasa.es»). Кампания не завершается, пока веб-этап ещё ищет.

## План поиска (SearchPlan)

Перед первым раундом запросов модель (`OPENROUTER_PLAN_MODEL`, по умолчанию
`anthropic/claude-sonnet-4.5`, для лучшего качества рекомендуется opus; таймаут
`OPENROUTER_PLAN_TIMEOUT_SECONDS`, 45 с, температура 0.3, один вызов на кампанию)
по ТЗ (`TaskSpec`) пишет `SearchPlan` (`bot/campaign/search_plan.py`):
`sites` (порталы с приоритетом 1-3), `queries` (5-15 запросов на язык es/en, плюс
ru/uk, если человек писал на них; в каждом тип объекта, сделка, место со страной,
бюджет и комнаты), `portal_urls` (прямые страницы поиска портала с фильтрами:
idealista, fotocasa, pisos.com, habitaclia; только шаблоны, в которых модель
уверена), `stop` и `notes`. Код проверяет план: хост должен быть в списке портов
страны (`urls.SPAIN_PORTALS_BY_KIND`, `UKRAINE_PORTALS`) или в `spec.sources`,
иначе отбрасывается (с записью в лог); запросы без повторов по смыслу, не больше
15 на язык и 60 всего; хосты из `spec.sources.blocked` вычищаются.

План сохраняется в `campaigns.plan.search_plan` (jsonb, `set_search_plan`) один раз
и дальше используется веб-этапом:

- первый раунд берёт запросы плана вперёд (и следующие раунды, пока они не
  кончились), остаток раунда дописывают модель запросов и шаблоны;
- `portal_urls` ставятся в очередь как index-страницы (глубина 0) до первого поиска;
- порядок обязательных порталов: сначала `sites` плана по приоритету, потом список по виду объекта;
- сайты из `spec.sources.blocked` не ищутся, не читаются и не попадают в очередь.

Без ключа, при сбое или пустом ответе модели плана нет (`None`) и этап работает как раньше.
Планировщик подключается параметром `planner=` у `WebSearchWorker`
(`CampaignRunnerSettings.search_planner()`); `CAMPAIGN_SEARCH_PLAN_ENABLED=false` отключает его.

## Поисковые бэкенды

По умолчанию ищет только SearXNG. `WEB_SEARCH_BACKENDS` — список через запятую из
`searxng`, `google_cse`, `serpapi`; несколько бэкендов опрашиваются параллельно, выдача
сливается по кругу (по одному результату от каждого) без дублей по `url_key`. Сбой одного
бэкенда логируется (`web_search.backend_failed`) и не мешает остальным. Google CSE
(`GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_CX`; 100 бесплатных запросов в сутки) и SerpAPI
(`SERPAPI_API_KEY`; платный) ограничены `WEB_SEARCH_GOOGLE_CSE_DAILY_CAP` /
`WEB_SEARCH_SERPAPI_DAILY_CAP` (по 90). Счётчик хранится в памяти процесса по дням UTC
(сбрасывается при перезапуске; счётчик в БД — отдельная задача). Бэкенд без ключа пропускается.

## Лимиты

| Переменная | По умолчанию | Что ограничивает |
|---|---|---|
| `WEB_SEARCH_MAX_QUERIES_PER_CAMPAIGN` | 80 | запросов на кампанию |
| `WEB_SEARCH_QUERIES_PER_ROUND` | 12 | запросов в раунде |
| `WEB_SEARCH_RESULTS_PER_QUERY` | 30 | результатов с одного запроса (после слияния страниц) |
| `WEB_SEARCH_PAGES_PER_QUERY` | 2 | страниц выдачи SearXNG на запрос (1-5; остановка, если страница не дала новых ссылок) |
| `WEB_SEARCH_QUERY_REUSE_HOURS` | 0 | часов, в течение которых запрос, уже выполненный другой кампанией, пропускается (0: не пропускается; свои повторы кампания не делает никогда) |
| `WEB_SEARCH_MAX_PAGES_PER_CAMPAIGN` | 400 | страниц на кампанию |
| `WEB_SEARCH_MAX_PAGES_PER_HOST` | 100 | страниц одного сайта на кампанию |
| `WEB_SEARCH_MAX_LINKS_PER_INDEX` | 40 | ссылок со страницы поиска портала |
| `WEB_SEARCH_INDEX_TTL_DAYS` | 7 | через сколько дней страница поиска портала (`index`) читается заново (0: не читается; объявления не читаются повторно никогда) |
| `WEB_SEARCH_MAX_RENDERS_PER_CAMPAIGN` | 60 | чтений в браузере на кампанию (считается в БД) |
| `WEB_SEARCH_RENDER_ON_REFUSAL` | true | отказанную (403/429/503, капча) страницу пробовать в браузере |
| `WEB_SEARCH_MAX_SCRAPE_API_PER_CAMPAIGN` | 40 | чтений через scrape API на кампанию (если задан `WEB_SEARCH_SCRAPE_API_URL`) |
| `WEB_SEARCH_MAX_PAGES_PER_DAY` | 3000 | страниц за 24 ч на всю систему |
| `WEB_SEARCH_MAX_QUERIES_PER_DAY` | 300 | запросов за 24 ч на всю систему |
| `WEB_SEARCH_MAX_MINUTES_PER_CAMPAIGN` | 240 | длительность веб-этапа кампании |

`WEB_SEARCH_ENABLED=false` выключает этап. Сайт можно запретить навсегда
(`WEB_SEARCH_BLOCKED_HOSTS`) или поставить на паузу его источник
(`/pause source:<id>`, id — в `monitoring_sources`, `canonical_url =
https://<сайт>/`).

## Прокси / VPN

`WEB_SEARCH_PROXY_URL` (`http://host:port` или `socks5://host:port`) — через
него идут и чтение страниц, и исходящие запросы SearXNG. Пусто — напрямую.

Подключить WireGuard через [gluetun](https://github.com/qdm12/gluetun):

1. В `docker-compose.yml` раскомментируйте сервис `gluetun` (пример рядом с
   `searxng`).
2. В `.env` задайте `GLUETUN_VPN_PROVIDER` (или `custom`),
   `GLUETUN_WIREGUARD_PRIVATE_KEY`, `GLUETUN_WIREGUARD_ADDRESSES`,
   `GLUETUN_WIREGUARD_PUBLIC_KEY`, `GLUETUN_WIREGUARD_ENDPOINT_IP`,
   `GLUETUN_WIREGUARD_ENDPOINT_PORT` — из конфигурации вашего VPN.
3. `WEB_SEARCH_PROXY_URL=http://gluetun:8888` (HTTP-прокси gluetun).
4. `docker compose up -d gluetun searxng campaign-runner`.

Значение прокси не пишется в логи.

Можно перечислить несколько прокси через запятую: каждый сайт закреплён за
одним (`crc32(host) % n`), чтобы видеть один IP; следующий берётся только если
к выбранному не удалось подключиться.

## Имитация браузера

`WEB_SEARCH_IMPERSONATE` (`chrome` по умолчанию; `off|chrome|safari|firefox`
или точный профиль curl_cffi, например `chrome124`) — страницы читаются через
`curl_cffi` с TLS/HTTP2-отпечатком браузера, UA Chrome 124/Windows
(`WEB_SEARCH_BROWSER_USER_AGENT`), `Accept-Language` по стране (ES, UA),
`Sec-Fetch-*`, `Upgrade-Insecure-Requests`. `off` или отсутствие `curl_cffi` —
обычный httpx с объявленным UA бота. `robots.txt` всегда проверяется по
объявленному `WEB_SEARCH_USER_AGENT`. Правила те же: публичные адреса на
каждом переходе, не больше 4 редиректов (вручную), только HTML, лимит размера,
без cookies. Коды ошибок (`http_403` и т.д.) не меняются.

`robots.txt` проверяется с объявленным UA бота (применяются правила для `*`),
тогда как сами страницы читаются с браузерным отпечатком; на переходах по
редиректам `robots.txt` повторно не проверяется. Неизвестное значение
`WEB_SEARCH_IMPERSONATE` при загрузке даёт предупреждение и заменяется на
`chrome`. После отказа браузер и scrape API пробуют только объявления и ссылки
глубины 1; отказанная индексная страница глубины 0 получает максимум браузер
(`WEB_SEARCH_RENDER_INDEX_ON_REFUSAL`), scrape API — никогда.

## Развёртывание

```sh
git pull
./scripts/apply_migrations.sh                       # применит 021_campaign_web_search.sql ... 032_web_seen_urls_ttl.sql
docker compose pull searxng
docker compose up -d --build searxng campaign-runner
docker compose logs -f campaign-runner | grep web_search
```

## Риски

- Крупные порталы (idealista, fotocasa) защищены от ботов (DataDome и т. п.) и
  часто отвечают 403 на обычный HTTP-клиент. Отказанная страница пробуется один
  раз в браузере (и, если настроен, через scrape API); после трёх отказов слой
  блокируется на 12 часов, когда отказали все слои, остаются карточки из
  результатов поиска. `robots.txt` не обходится никогда; объявления с агентских сайтов и
  классифайдов читаются лучше. Прокси/VPN может помочь, но не гарантирует.
- Поисковики внутри SearXNG иногда отдают CAPTCHA или лимит — тогда запрос
  возвращает мало результатов (в логе `web_search.engines_silent`).
- Объявление, прочитанное для одной кампании, другой кампании уже не
  показывается (так задано: «никогда не читать повторно»). Исключение: страница
  поиска портала (`index`) читается заново через `WEB_SEARCH_INDEX_TTL_DAYS`,
  а найденные на ней ссылки проходят обычную дедупликацию.
