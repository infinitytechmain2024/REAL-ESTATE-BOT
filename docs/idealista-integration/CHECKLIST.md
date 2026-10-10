# Final Acceptance Checklist

- [x] ListingSource protocol реализован — `bot/web_search/sources/base.py:28`; SourceListing с отдельным plot_m2, экспорт в sources/__init__.py.
- [x] ApifyIdealistaSource написан и покрыт обработкой ошибок — tests/test_apify_source.py, HTTPX mocks; default-off, live fixture не получен.
- [x] _from_sources создаёт claim/POST только в первом раунде; _resume_sources обрабатывает existing run/cache без нового запуска — tests/test_listing_source_worker.py.
- [ ] via/layer=api: core store/worker поддержаны и проверены; pages_api/полная отчётность ещё требуют фазы 5.
- [x] Failed URL можно повторно захватывать через API — Memory и реальный Postgres, same/other campaign; успешные/snippet URL защищены.
- [x] API success/failure не меняет HTTP/render refusals/blocked_until, не трогает HTML breaker — success/failure tests с исходными ненулевыми отказами.
- [ ] В отчётах видно "Idealista (API)"
- [x] APIFY Settings/.env.example/compose/docs синхронны; Scrape.do settings остаются фазой 4.
- [x] Базовые тесты проходят — фаза 3: полный pytest 1508 passed, 0 skipped, Postgres/golden включены; Ruff чисто. Повторная приёмка после фазы 5.
- [x] Mock/system регрессии других порталов проходят в полном pytest фазы 3; live-доступность 20 порталов не утверждается, повторить после фазы 5.
- [x] Токен repr=False/Bearer, безопасные исключения и логи, .env.example без ключей — tests/test_apify_source.py и deployment tests; повторить после фазы 4 query auth.

## Проверка foundation (фаза 1, 2026-10-10)

- [x] CHECK 043 допускает api/NULL и прежние http/render/scrape/none; другие CHECK остаются — `test_listing_source_migration_upgrades_populated_039_and_is_idempotent`.
- [x] Обновление реальной схемы 039 с данными и повторное применение 043 — тот же PostgreSQL-тест.
- [x] Пост API сохраняется с campaign linkage, raw_payload.via и layer; HTML refusals/blocks не меняются — `test_structured_api_listing_uses_the_existing_post_and_campaign_path`.
- [x] Memory сохраняет API-пост/layer и не добавляет refusals — `test_memory_store_persists_structured_api_listing`.
- [x] JSON-LD факты совместимы с prefilter; plot_m2 не подменяется площадью постройки — `test_source_listing_facts_preserve_plot_area_for_analysis_prefilter`.
- [x] Оба списка scripts/apply_migrations.sh перечисляют новую миграцию — `test_migration_script_lists_every_migration_by_its_real_path_in_order` (часть полного прогона).

Это частичная приёмка foundation. Выше остаются открытыми актор, первый раунд, retry failed, полная семантика api в store/counters и отчётах. Round-trip не доказывает правильность счётчиков pages_fetched/pages_failed: их исправление запланировано фазой 3, источник пока не подключён.

## Дополнительная приёмка фаз 2–3

- [x] Durable launch/cache/offset и idempotent billing; нет повторного POST после сбоя.
- [x] Pending paid run имеет явно помеченную оценку и bounded settlement при time_cap/cancel.
- [x] Fresh claim/BUSY не перехватывается, stale claim восстанавливается; готовые факты сохраняются.
- [x] Пауза/удаление источника и robots/cap/operator queue policy сохранены; HTML-only block допускает API import.
- [x] Host success/failure и progress.read правильно учитывают API; persistent scrape не переименован.
- [x] API ошибки видны в финальном отчёте; skip означает отсутствие вызова ИИ, а не отсутствие расходов на источник.

Доказательства и пределы приёмки: [REVIEW_PHASE_3.md](REVIEW_PHASE_3.md). Фазы 4–5 и smoke впереди.
