# Final Acceptance Checklist

- [x] ListingSource protocol реализован — `bot/web_search/sources/base.py:28`; SourceListing с отдельным plot_m2, экспорт в sources/__init__.py.
- [ ] ApifyIdealistaSource написан и покрыт обработкой ошибок
- [ ] _from_sources вызывается только в первом раунде
- [ ] via="api" и layer="api" поддерживаются везде
- [ ] Failed URL можно повторно захватывать через API
- [ ] API-вызовы не блокируют хост
- [ ] В отчётах видно "Idealista (API)"
- [ ] Settings и .env.example обновлены
- [x] Базовые тесты проходят — фаза 1: полный pytest 1421 passed, 0 skipped (PostgreSQL включён), ruff чисто, golden 70 passed; финальное повторное подтверждение после фазы 5.
- [ ] Нет регрессий по другим порталам
- [ ] Нет утечек токенов и секретов

## Проверка foundation (фаза 1, 2026-10-10)

- [x] CHECK 043 допускает api/NULL и прежние http/render/scrape/none; другие CHECK остаются — `test_listing_source_migration_upgrades_populated_039_and_is_idempotent`.
- [x] Обновление реальной схемы 039 с данными и повторное применение 043 — тот же PostgreSQL-тест.
- [x] Пост API сохраняется с campaign linkage, raw_payload.via и layer; HTML refusals/blocks не меняются — `test_structured_api_listing_uses_the_existing_post_and_campaign_path`.
- [x] Memory сохраняет API-пост/layer и не добавляет refusals — `test_memory_store_persists_structured_api_listing`.
- [x] JSON-LD факты совместимы с prefilter; plot_m2 не подменяется площадью постройки — `test_source_listing_facts_preserve_plot_area_for_analysis_prefilter`.
- [x] Оба списка scripts/apply_migrations.sh перечисляют новую миграцию — `test_migration_script_lists_every_migration_by_its_real_path_in_order` (часть полного прогона).

Это частичная приёмка foundation. Выше остаются открытыми актор, первый раунд, retry failed, полная семантика api в store/counters и отчётах. Round-trip не доказывает правильность счётчиков pages_fetched/pages_failed: их исправление запланировано фазой 3, источник пока не подключён.
