---
name: add-apify-source
description: Добавить новый источник данных через Apify-актор (портал, группы/посты/комментарии Facebook, Instagram, TikTok) — выбор актора, маппер, настройки, тесты на моках, живой сэмпл. Используй при задачах «подключи портал X», «читай пост по ссылке», «добавь TikTok».
---

# add-apify-source

1. **Выбор актора** (через коннектор Apify, без запусков — бесплатно): `search-actors` → `fetch-actor-details`
   (pricing, stats, rating, inputSchema, outputSchema). Предпочитай официальные `apify/*` и акторы с большим числом
   пользователей и свежим обновлением. Запиши в PR: актор, цена за запись, входные поля.
2. **Где жить коду**:
   - объявления портала → подкласс `_PortalSource` в `bot/web_search/sources/portals.py`
     (образец `ApifyFotocasaSource`: `kinds`, `deals`, `_plan`, `_normalize`);
   - посты/комментарии соцсетей → модуль в `bot/acquisition/` на `ApifyHttp` (`apify_client.py`), запись в
     `collected_posts` через `AcquiredPost.collected_post_row()`.
3. **Маппер**: факты только из полей выдачи; нет числа — `None`; валюта EUR только если портал в евро; площадь
   участка через `plot_area` (`portals.py`); телефон/имя — `phone`/`advertiser`. Отбрасывай чужой муниципалитет,
   чужую сделку, не-объявления (`classify_url(url) == "listing"`).
4. **Настройки** (выключено по умолчанию): `APIFY_<X>_ENABLED`, `_ACTOR`, `_MAX_RESULTS` (1–50), `_MAX_CHARGE_USD`;
   добавить в `bot/web_search/settings.py` (`sources()`), `docker-compose.yml` (сервис `campaign-runner`),
   `.env.example`.
5. **Тесты** (`tests/test_apify_portals.py` как образец): supports(), тело запуска актора, маппинг строки,
   отказы (чужая сделка/хост/страница поиска), «ничего не угадывать», settings строит источник только при флаге.
6. **Живой сэмпл** — ⛔ только с «ок» владельца: `call-actor` с `maxItems ≤ 5`, `maxTotalChargeUsd ≤ 0.02`;
   выдачу превратить в фикстуру, PII заменить; обновить маппер по реальным полям.
7. Документация: строка в `docs/apify_portals.md` (флаг, актор, цена, как проверено).
