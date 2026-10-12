# Результаты изменений

- 2026-10-10, Idealista phase 5: `pages_api`, метка «Idealista (API)» для реального API-чтения,
  раздельные API/Scrape API счётчики и финальная приёмка. Полный pytest: 1512 passed, 0 skipped;
  Ruff чисто. [Приёмка](idealista-integration/REVIEW_PHASE_5.md). Live/VPS не запускались.
- 2026-10-10, Idealista phase 4: Scrape.do через существующий unlocker; query token/ES/render/super,
  учёт фактических credits или явно помеченной оценки. Полный pytest: 1510 passed, 0 skipped;
  Ruff чисто. Платных вызовов не было. [Конфигурация](idealista-integration/SCRAPE_DO_CONFIGURATION.md).
- 2026-10-10, Idealista phases 2–3: default-off structured source с durable claim/billing
  и повтором failed URL через API; ошибки API видны в финальном отчёте, подпись
  префильтра уточнена до «без вызовов ИИ». Полный pytest: 1508 passed, 0 skipped;
  Ruff чисто. Данные и HTTP тестов синтетические; live API и VPS не запускались.
  Подробности: [HANDOFF](idealista-integration/HANDOFF.md) и
  [приёмка](idealista-integration/REVIEW_PHASE_3.md).
