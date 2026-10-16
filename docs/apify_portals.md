# Apify-акторы для порталов

Сайтовый этап (сервис `campaign-runner`) может брать объявления с порталов через акторы Apify, а не через свой
загрузчик, который часто упирается в DataDome. Каждый портал включается своим флагом, по умолчанию всё выключено.
Токен общий: `APIFY_TOKEN`.

Код: `bot/web_search/sources/apify.py` (общий жизненный цикл `ApifyListingSource`) и
`bot/web_search/sources/portals.py` (порталы).

## Что поддерживается

| Портал | Флаг | Актор | Проверено |
|---|---|---|---|
| Fotocasa | `APIFY_FOTOCASA_ENABLED` | `igolaizola/fotocasa-scraper` | живой запуск 13.10 (5 участков, Мадрид, ≈ $0.006) |
| Yaencontre | `APIFY_YAENCONTRE_ENABLED` | `igolaizola/yaencontre-scraper` | живой запуск 16.10: вход `location`-текстом падает (HTTP 403 на autocomplete), **работают только координаты**: `latitude`, `longitude`, `radiusKm` (геокодер обязателен, иначе источник не работает; радиус: заданный пользователем, иначе 15 км для города, 60 для провинции). Круг заходит в соседние муниципалитеты (Getafe при поиске по Мадриду), поэтому для города строки фильтруются по последней части `address.qualifiedName`; для провинции работает только круг 60 км от центра одноимённого города-столицы (Guadalajara попадает; Toledo, Segovia, Estepona, Ronda — нет). Провинции без одноимённого города (Asturias, Cantabria, Navarra) не обслуживаются: геокодер центра не даст. При продолжении уже оплаченного прогона без геокодера город фильтруется только по `qualifiedName`, провинция и радиус — строки отбрасываются. `area` при `family=LAND` — площадь участка; `family`/`operation` не по задаче — отбрасываются; телефон `owner.virtualPhoneNumber` только при `phoneAvailable` не False. `owner.source=IDEALISTA` — объявление-агрегат, ждите дубли с Idealista (поля в `SourceListing` нет). Коммерческая недвижимость (`premises`) вживую не проверялась |
| Idealista | `APIFY_IDEALISTA_ENABLED` + `APIFY_IDEALISTA_PROVIDER=igolaizola` | `igolaizola/idealista-scraper` | живой запуск 16.10 (участки, Мадрид; получено 5 строк, в фикстурах 3); `size` участка = площадь участка, описания в выдаче нет |
| Pisos.com | `APIFY_PISOS_ENABLED` | `logiover/pisos-com-property-scraper` | живой запуск 16.10 (участки, Мадрид; получено 5 строк, в фикстурах 4); `areaSqm` часто пуст, площадь из описания: сначала с пометкой участка, иначе единственное число без построек (`m²t`, edificabilidad, nave и т. п. не считаются); `city` = «Madrid Capital»; без телефона. Города: Мадрид, Барселона, Валенсия, Севилья, Малага |
| Habitaclia | `APIFY_HABITACLIA_ENABLED` | `trev0n/habitaclia-com-spain-scraper` | живой запуск 16.10 (участки, Мадрид; получено 5 строк, в фикстурах 3); `areaSqm` пуст у всех, площадь только из текста; без телефона; портал отнёс к Мадриду объявление из Mijas (Málaga) — оставлено как дал портал |
| Milanuncios | `APIFY_MILANUNCIOS_ENABLED` | `rastriq/milanuncios-scraper` | живой запуск 16.10: вход `categorySlug` + `province` (slug: `madrid`, `barcelona`, `vizcaya`, `las_palmas`; неизвестный slug останавливает запуск); `searchUrl` не используется (с `/terrenos/` игнорирует `keywords`, отдаёт всю страну). Земля — только `solares` (urban); `venta-de-fincas` (rural) существует, но **не покрыта** (один запуск на кампанию), `venta-de-terrenos` не существует (404). Категории `venta-de-pisos`, `alquiler-de-pisos`, `venta-de-casas`, `alquiler-de-casas`, `venta-de-locales` **не проверены живьём** (жильё и коммерция; флаг держать выключенным). Обслуживаются только проверенные slug провинций: `madrid`, `barcelona`, `valencia`, `sevilla`, `malaga`, `vizcaya`, `las_palmas` (расширять после живой проверки); для города — только если он сам провинция (столицы), прочее не обслуживается. `price=0` — «precio a convenir», цена `None`; `tag_sqm` — участок; `sellType=demand` отбрасывается; `stripSellerPII=true` всегда: без телефона и имени продавца; нет аренды участков и локалов ($0.00125 за объявление) |

Задачи, которые берут порталы: Испания, недвижимость, продажа или аренда, участок, жильё или коммерция; город,
провинция или явный радиус. Комнаты, инвесторы и «только пригороды» идут обычным поиском.

Цена около $0.001 за объявление плюс $0.001–0.005 за запуск. Один запуск на кампанию и портал; потолок —
`APIFY_<ПОРТАЛ>_MAX_CHARGE_USD` (по умолчанию $0.10), сверху действует `CAMPAIGN_BUDGET_USD`.

## Что попадает в находку

Ссылка, заголовок, цена (EUR), площадь (для участка — как площадь участка, если описание не называет другую),
комнаты, адрес, телефон и имя агентства. Ничего не угадывается: нет числа — пустое поле. Объявления другого
муниципалитета, другой сделки или за пределами радиуса отбрасываются.

## Включение на VPS

```bash
cd /opt/real-estate-bot
cat >> .env <<'EOF'
APIFY_FOTOCASA_ENABLED=true
APIFY_YAENCONTRE_ENABLED=true
APIFY_IDEALISTA_PROVIDER=igolaizola
EOF
docker compose --env-file .env up -d campaign-runner
```

Первый раз — одна тестовая кампания, затем проверка:

```sql
select name, state, error_code, jsonb_array_length(listings) from web_listing_source_runs
 order by updated_at desc limit 5;
select item, code, sum(cost_usd) from campaign_costs where stage = 'api' group by 1, 2;
```

Если у Yaencontre или Milanuncios `state = 'ready'`, а объявлений 0, пришлите одну строку выдачи актора (Apify Console
→ Runs → Dataset): маппер этих порталов написан по схеме, а не по живому запуску.

## Выключить

Флаг портала в `false` (или `APIFY_IDEALISTA_PROVIDER=axlymxp`) и `docker compose --env-file .env up -d
campaign-runner`. Обычный поиск по сайтам работает как раньше.
