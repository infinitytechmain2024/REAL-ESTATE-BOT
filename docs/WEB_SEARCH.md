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
   - `web_hosts` — каждый сайт: его источник, счётчики и временная блокировка
     после трёх отказов подряд (403/429/503) на 12 часов.
   - `web_search_queries` — запросы кампании (уникальны по смыслу); запрос,
     который другая кампания искала за последние 72 часа, не ищется снова.
4. **Чтение страниц.** Только публичные GET-страницы: без логинов, форм и
   cookies, только публичные адреса, `robots.txt` соблюдается (и его
   `Crawl-delay`), не чаще одного запроса к сайту в
   `WEB_SEARCH_HOST_INTERVAL_SECONDS`, таймауты и лимит размера. Страница
   поиска портала (список объявлений) не сохраняется: из неё берутся ссылки на
   конкретные объявления того же сайта (не больше
   `WEB_SEARCH_MAX_LINKS_PER_INDEX`), и читаются они.
5. **Объявление → находка.** Текст объявления записывается в
   `collected_posts` так же, как пост из Facebook: сайт — это
   `monitoring_sources` (platform `website`), чтение — `acquisition_runs`
   внутри веб-пакета кампании (`acquisition_batches.campaign_id`). Дальше всё
   штатно: анализ, карточки (точные / похожие / другие / исключённые) и ссылка
   на конкретное объявление.

Статус для пользователей: «Ищу в интернете…» или «Ищу на сайте fotocasa.es…».
Владельцы видят техническую строку («сайты: запросов 12/40 · страниц 7/60 ·
сайт fotocasa.es»). Кампания не завершается, пока веб-этап ещё ищет.

## Лимиты

| Переменная | По умолчанию | Что ограничивает |
|---|---|---|
| `WEB_SEARCH_MAX_QUERIES_PER_CAMPAIGN` | 40 | запросов на кампанию |
| `WEB_SEARCH_QUERIES_PER_ROUND` | 12 | запросов в раунде |
| `WEB_SEARCH_RESULTS_PER_QUERY` | 10 | результатов с одного запроса |
| `WEB_SEARCH_MAX_PAGES_PER_CAMPAIGN` | 60 | страниц на кампанию |
| `WEB_SEARCH_MAX_PAGES_PER_HOST` | 12 | страниц одного сайта на кампанию |
| `WEB_SEARCH_MAX_LINKS_PER_INDEX` | 10 | ссылок со страницы поиска портала |
| `WEB_SEARCH_MAX_PAGES_PER_DAY` | 400 | страниц за 24 ч на всю систему |
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

## Развёртывание

```sh
git pull
./scripts/apply_migrations.sh                       # применит 021_campaign_web_search.sql
docker compose pull searxng
docker compose up -d --build searxng campaign-runner
docker compose logs -f campaign-runner | grep web_search
```

## Риски

- Крупные порталы (idealista, fotocasa) защищены от ботов (DataDome и т. п.) и
  часто отвечают 403 на обычный HTTP-клиент. Бот это не обходит: после трёх
  отказов сайт блокируется на 12 часов; объявления с агентских сайтов и
  классифайдов читаются лучше. Прокси/VPN может помочь, но не гарантирует.
- Поисковики внутри SearXNG иногда отдают CAPTCHA или лимит — тогда запрос
  возвращает мало результатов (в логе `web_search.engines_silent`).
- Страница, прочитанная для одной кампании, другой кампании уже не
  показывается (так задано: «никогда не читать повторно»).
