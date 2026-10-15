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
| Yaencontre | `APIFY_YAENCONTRE_ENABLED` | `igolaizola/yaencontre-scraper` | по схеме выдачи из Apify Store |
| Idealista | `APIFY_IDEALISTA_ENABLED` + `APIFY_IDEALISTA_PROVIDER=igolaizola` | `igolaizola/idealista-scraper` | по схеме выдачи из Apify Store |
| Pisos.com | `APIFY_PISOS_ENABLED` | `logiover/pisos-com-property-scraper` | по схеме из README, живой сэмпл не делался (README с сервера не читался — поля выдачи предположены) |

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

Если у Yaencontre или Idealista `state = 'ready'`, а объявлений 0, пришлите одну строку выдачи актора (Apify Console
→ Runs → Dataset): маппер написан по схеме, а не по нашему запуску.

## Выключить

Флаг портала в `false` (или `APIFY_IDEALISTA_PROVIDER=axlymxp`) и `docker compose --env-file .env up -d
campaign-runner`. Обычный поиск по сайтам работает как раньше.
