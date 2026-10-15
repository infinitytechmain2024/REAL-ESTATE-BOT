# REAL-ESTATE-BOT v2 — мастер-план (пятница → понедельник)

Владелец: Dmytro. Сдача клиенту: **понедельник**. Исполнитель плана: главный архитектор (Claude Fable 5.1) с
субагентами разработки из `.claude/agents/`. Правила работы — `CLAUDE.md`. Детальная разбивка задач —
`docs/v2/TASKS.md` (её пишет архитектор по этому плану, см. `docs/v2/KICKOFF_FABLE.md`).

---

## 1. Цель (что клиент получает в понедельник)

Telegram-бот, который **24/7** выполняет задачи двух типов, в том числе **одновременно** (`vertical = both`):

1. **Недвижимость** — по задаче («участок в Мадриде, покупка, от 2000 м², до 3 млн €») находит объекты на порталах
   Испании и в группах Facebook, проверяет каждый по условиям и присылает карточки: цена, площадь, адрес, телефон,
   ссылка, почему подходит.
2. **Инвесторы** — находит людей и компании, которые покупают или инвестируют: в группах инвесторов, в комментариях
   под объектами и под **постом по ссылке**, присланной пользователем; присылает карточки лидов с публичной ссылкой
   на профиль и цитатой, почему это лид.

Поток пользователя: голос/текст в Telegram → распознавание → **архитектор** разбирает задачу и показывает план
(«Проверьте задачу») → пользователь принимает или правит → кампания идёт сама: сбор → анализ → карточки → отчёт.

### Критерии приёмки (Definition of Done)
| # | Критерий | Как проверить |
|---|---|---|
| DoD-1 | На «золотом наборе» (§7) недвижимость: ≥ 70 % отправленных карточек — точные совпадения, 0 мусорных сайтов | ручная разметка 5 кампаний |
| DoD-2 | Инвесторы: по посту-ссылке с ≥ 30 комментариями бот выдаёт лидов с профилем и цитатой; ≥ 60 % лидов разумны | 3 поста из золотого набора |
| DoD-3 | `both`: обе ветки идут параллельно, отчёт разделён, бюджеты считаются по каждой | 1 кампания `both` |
| DoD-4 | 24/7: все сервисы с healthcheck и `restart: unless-stopped`; падение сервиса, исчерпание бюджета, 0 результатов подряд → сообщение владельцу в Telegram | убить контейнер, проверить алерт |
| DoD-5 | Стоимость кампании видна в отчёте; потолок `CAMPAIGN_BUDGET_USD` соблюдается | `campaign_costs` |
| DoD-6 | Всё новое за флагами; откат каждой части — одна строка `.env` | `docs/v2/ROLLBACK.md` |
| DoD-7 | CI зелёный (ruff + pytest), только моки в тестах | GitHub Actions |

---

## 2. Контекст: что есть сейчас (на `main`, коммит `d59620e`)

Сервисы (`docker-compose.yml`, проект `real-estate-monitor`, VPS `/opt/real-estate-bot`):

| Сервис | Код | Роль |
|---|---|---|
| telegram | `bot/control_plane/` | бот, интервью, голос→текст (`stt.py`), понимание задачи (`understanding.py`, `intake.py`), черновики задач с подтверждением (миграции 018, 031) |
| campaign-runner | `bot/campaign/`, `bot/web_search/` | кампании, окна, сайтовый этап, Apify-порталы, отбор (`tolerance.py`, `relevance.py`), дедуп, отчёт (`final_report.py`), инвесторы (`reach.py`, `leads.py`) |
| analysis-worker | `bot/analysis_pipeline/` | префильтр → LLM (OpenRouter) → факты с цитатами |
| facebook-runner / facebook-collector | `bot/facebook_collector/` | группы FB: браузер (`local`) или Apify (`apify/`, выключен флагом) |
| browser | `bot/browser_session/` | профиль оператора FB (Playwright) |
| agent-reach-worker | `bot/agent_reach/` | чтение заданных URL через браузер: facebook / instagram / tiktok / website (**LinkedIn нет**) |
| reduction-worker, verification, scrapling-worker | `bot/agents/`, `bot/verification/`, `bot/scrapling_connector/` | вспомогательные |
| postgres, redis, searxng, caddy | — | инфраструктура |

Готовые кирпичи, которые v2 **переиспользует, а не переписывает**:
- `bot/acquisition/apify_client.py` — общий Apify HTTP (запуск без повторов, ретраи чтения, `LaunchUnknown`).
- `bot/web_search/sources/apify.py` `ApifyListingSource` — жизненный цикл платного запуска (claim, чекпоинт run id,
  биллинг один раз, импорт); `sources/portals.py` — Fotocasa / Yaencontre / Idealista (`igolaizola`).
- `bot/facebook_collector/apify/` — открытые группы FB через `apify/facebook-groups-scraper`, закрытые → браузер.
- `bot/utils/costs.py` + `campaign_costs` (042) — учёт денег и бюджет кампании.
- `bot/acquisition/contracts.py` `AcquiredPost` — единый формат поста для `collected_posts`.
- Vertical `real_estate | investors | both` (`bot/campaign/models.py:24`).

### Почему результаты сейчас плохие (диагноз)
1. **Мусор на входе**: основной объём даёт поисковик SearXNG + свой загрузчик (словари, блокировки, сниппеты вместо
   страниц) — `docs/idealista-integration/SCRAPING_ANALYSIS.md`.
2. **Терялась площадь**: старый актор Idealista (`axlymxp`) давал `size` без смысла «участок» → 0 точных совпадений
   в smoke-тесте (19 находок, 0 отправлено). Новые акторы `igolaizola` дают площадь — **не проверено живой кампанией**.
3. **Слабые модели на ключевых шагах** (`gpt-4o-mini` пишет запросы; Sonnet 4.5 на анализе).
4. **Соцсети** читаются только как сниппеты `site:tiktok.com …` — для объектов почти бесполезно.
5. Инвесторы ищутся через поисковик (`reach.py`) — только сниппеты; лиды из комментариев — через браузер, медленно.

---

## 3. Целевая архитектура v2 (Apify-first)

```
Telegram (голос/текст)
  └─ stt.py ─► ARCHITECT (Claude, headless) ─► «Проверьте задачу» ─► принять
                                                             │
                ┌────────────────────────────────────────────┴─────────────────────────────┐
           REAL ESTATE                                                                  INVESTORS
  Apify-порталы (6): Idealista, Fotocasa, Yaencontre,             Пост по ссылке: apify/facebook-posts-scraper +
  Pisos, Habitaclia, Milanuncios                                  apify/facebook-comments-scraper → лиды
  FB открытые группы: apify/facebook-groups-scraper               Группы инвесторов FB (Apify) → лиды
  FB закрытые: уведомление «вступите» → браузер                   Комментарии под отправленными объектами (Apify)
  Поисковик: только запасной, только allowlist                    Instagram / TikTok (Apify, за флагом)
                └──────────────────────────────┬──────────────────────────────────────────┘
                                   collected_posts / leads (БД)
                                               ▼
                       analysis-worker: префильтр → LLM → факты с цитатами
                                               ▼
                     отбор (точно / похоже / нет) → дедуп → карточки → отчёт (раздельно RE / инвесторы)
                                               ▼
                            watchdog: алерты владельцу, дневная сводка
```

Принципы:
- **Apify — основной сбор.** Поисковик — запасной (если Apify дал < N), только по allowlist. `site:`-соцпоиск выключен.
- **Архитектор на Claude** — headless Claude Code / Claude Agent SDK в отдельном сервисе `architect`; на выходе план
  в существующем формате кампании; при ошибке/таймауте — текущий `understanding.py` (fallback).
- **Закрытые группы FB** — только через вступление человеком (бот присылает ссылку), чтение — браузером. Никаких
  cookie-акторов и обхода входа.
- **LinkedIn** — без автоматизации аккаунта (банит); только сниппеты поисковика в инвесторском поиске.
- **Оплата LLM в проде — API-ключ** (`ANTHROPIC_API_KEY`). Подписка Claude — только для разработки владельцем.
  Код одинаков, меняется переменная окружения.

---

## 4. Расписание и задачи

ID — для `docs/v2/TASKS.md`. «Агент» — субагент разработки (`.claude/agents/`). ⛔ — нужна остановка и «ок»
владельца. Ветка на каждый поток (`v2/<поток>`), PR на поток, мерж только после зелёного CI.

### Пятница — сбор без мусора (поток `v2/acquisition`)
| ID | Задача | Агент | Файлы | Приёмка |
|---|---|---|---|---|
| A1 | Флаг `ACQUISITION_MODE=apify\|legacy` (по умолчанию `legacy`): Apify-источники первыми; SearXNG только если Apify дал < `APIFY_MIN_LISTINGS`; соцпоиск `site:` выключен | builder + sources | `web_search/worker.py`, `web_search/settings.py`, `campaign/runner.py` (социальная стадия), `campaign/settings.py` | тесты: при `apify` нет вызовов SearXNG, если порталы дали ≥ N; при `legacy` поведение прежнее |
| A2 | Закрытая группа FB → сообщение оператору «Группа X закрыта, вступите: ссылка»; после вступления группа читается браузером | builder | `facebook_collector/apify/provider.py`, `apify/store.py`, `control_plane` (исходящие уведомления) | тест: INACCESSIBLE → одно уведомление на группу; повторная проверка после «вступил» |
| A3 | ⛔ Живая проверка Yaencontre и Idealista-`igolaizola` (≤ 5 объявлений, ≤ $0.05), правка мапперов по реальной выдаче | sources | `web_search/sources/portals.py`, фикстуры | фикстуры из живой выдачи (без PII), тесты зелёные |
| A4 | ⛔ Включение Apify для FB: 1 открытая группа, ≤ 50 постов, билд `0.99.1538` | sources + deployer | `.env` VPS | `apify_runs.items_imported > 0`, стоимость = Apify Billing |
| A5 | Модели по этапам (только `.env`): анализ и отбор → `anthropic/claude-sonnet-5.5`; запросы → `anthropic/claude-haiku-5.5`; отчёт → `anthropic/claude-opus-5.5` | analyst | `.env.example`, `docs/v2/MODELS.md` | таблица этап → модель → цена |
| A6 | Золотой набор (§7) и скрипт оценки кампании (SQL + разметка) | analyst | `scripts/eval_campaign.sql`, `docs/v2/GOLDEN_SET.md` | базовая линия до изменений записана |

### Суббота — инвесторы и порталы (потоки `v2/investors`, `v2/portals`)
| ID | Задача | Агент | Файлы | Приёмка |
|---|---|---|---|---|
| B1 | **Пост по ссылке → лиды**: пользователь шлёт URL поста FB → `apify/facebook-posts-scraper` (пост) + `apify/facebook-comments-scraper` (комментарии, `includeNestedComments`) → LLM классифицирует комментаторов (инвестор / покупатель / агент / шум) → лиды с профилем и цитатой → карточки | builder + sources + analyst | новый `bot/acquisition/apify_facebook_posts.py`, `campaign/leads.py` (хранилище лидов переиспользовать), `control_plane` (распознать URL как задачу), миграция при необходимости (db) | моки: 3 сценария (лиды есть / нет / закрытый пост); стоимость ≈ $0.0025 за комментарий в журнале |
| B2 | Лиды из комментариев под отправленными объектами — через Apify вместо браузера (флаг `LEADS_PROVIDER=apify\|browser`) | builder | `campaign/leads.py` | при `apify` браузер не трогается |
| B3 | Группы инвесторов FB (открытые) через Apify → тот же классификатор лидов | sources | `facebook_collector/apify/`, `campaign/reach.py` | лиды из групп в отчёте |
| B4 | `both`: две ветки параллельно, бюджет и отчёт раздельно | analyst + builder | `campaign/runner.py`, `final_report.py` | тест: `both` запускает обе ветки; отчёт с двумя разделами |
| B5 | Порталы P2: Pisos (`logiover/pisos-com-property-scraper`), Habitaclia (`trev0n/habitaclia-com-spain-scraper`), Milanuncios (`rastriq/milanuncios-scraper`) — по образцу `ApifyFotocasaSource` | sources | `web_search/sources/portals.py`, settings, compose, `.env.example` | моки + ⛔ живой сэмпл ≤ $0.02 на актор |
| B6 | (если успеваем) Instagram `apify/instagram-scraper`, TikTok `clockworks/tiktok-scraper` — поиск по хэштегам/словам, посты в анализ; за флагами | sources | новый `bot/acquisition/apify_social.py` | моки; по умолчанию выключено |

### Воскресенье — архитектор и 24/7 (потоки `v2/architect`, `v2/ops`)
| ID | Задача | Агент | Файлы | Приёмка |
|---|---|---|---|---|
| C1 | ⛔ (новая зависимость `claude-agent-sdk`) Сервис `architect`: headless Claude (Agent SDK), вход — текст задачи + ответы, выход — JSON плана, валидируется схемой кампании (`campaign/models.py`); скиллы `.claude/skills/task-*` внутри образа; таймаут 90 с; fallback на `understanding.py`; флаг `ARCHITECT_PROVIDER=claude\|openrouter` | builder + analyst | `bot/architect/` (новый), `docker/architect.Dockerfile`, compose, `control_plane/intake.py` | моки SDK; при ошибке — fallback, пользователь не видит разницы |
| C2 | Проводка: голос → `stt.py` → architect → «Проверьте задачу» → принять → кампания (RE / инвесторы / both / пост-ссылка) | builder | `control_plane/intake.py`, `service.py` | e2e-тест на памяти: 4 типа задач |
| C3 | 24/7: healthcheck у всех сервисов (сейчас нет у analysis-worker, campaign-runner, reduction-worker, searxng); watchdog → алерт владельцу (сервис down, 3 ошибки Apify подряд, бюджет исчерпан, 0 результатов в 2 кампаниях подряд); дневная сводка | deployer + builder | `docker-compose.yml`, новый `bot/ops/watchdog.py` | убить контейнер → алерт ≤ 2 мин |
| C4 | Бэкап БД по cron, ротация логов Docker, план перезагрузки VPS | deployer | `scripts/backup.sh`, `docs/v2/OPS.md` | бэкап есть, восстановление проверено на копии |
| C5 | (если успеваем) Общий LLM-клиент: Claude API напрямую, OpenRouter запасной; перевод 11 мест вызова | builder + analyst | новый `bot/llm/client.py` | моки; флаг `LLM_PROVIDER=openrouter\|anthropic` |

### Понедельник — приёмка и сдача
| ID | Задача | Агент |
|---|---|---|
| D1 | Золотой набор на VPS: 5 RE + 3 инвестора + 1 both; разметка; исправления | analyst + reviewer |
| D2 | Деплой (`scripts/update.sh`), включение флагов, проверка алертов | deployer |
| D3 | `docs/v2/HANDOFF.md`, `ROLLBACK.md`, инструкция клиенту (как ставить задачу, что значат карточки) | architect |

### Зависимости и автозапуск
- Источник правды по задачам — `docs/v2/tasks.json`: `id`, `deps`, `status`, `agent`, `stream`, `hours`, `day`.
  Доска для людей — `docs/v2/board.html` (колонки по статусам + граф зависимостей), пересобирается командой
  `python3 scripts/build_v2_board.py` (скрипт падает на неизвестной зависимости и на цикле).
- Задачи владельца — узлы `O1…O9` со статусом `waiting_owner`; владелец закрывает их ответом в чате, архитектор
  ставит `done`.
- **Правило планировщика:** задача `ready`, когда все её `deps` в `done` и она не `cut`. Архитектор после каждого
  изменения статуса пересчитывает готовые и **сразу запускает** их субагентами — параллельно, не больше 3 одновременно,
  каждый поток в своём git worktree / ветке `v2/<поток>`. Порядок среди готовых: сначала те, что разблокируют больше
  задач (по графу), затем по дню.
- Статусы: `in_progress` при запуске, `review` после сдачи исполнителем (идёт reviewer → PR), `done` после мержа PR
  с зелёным CI. Ошибка исполнителя — задача обратно в `ready` с заметкой в `TASKS.md`; две неудачи подряд — ⛔ владельцу.

### Линия отсечения (если не успеваем)
Не отрезать: **A1, A2, A3, A5, B1, B4, C2, C3, D1, D2**. Можно перенести: B6, C5, B5 (частично), C1 (тогда C2 идёт
через текущий `understanding.py` с сильной моделью).

---

## 5. Агенты разработки (`.claude/agents/`)

| Агент | Модель | Роль | Может | Не может |
|---|---|---|---|---|
| architect | opus | план, разбивка, раздача, контроль правил | читать, писать `docs/` | менять код |
| builder | sonnet | реализация по задаче | код, тесты | live-запросы, зависимости, секреты |
| sources | sonnet | Apify-акторы, мапперы, FB | `acquisition/`, `sources/`, `facebook_collector/` | live-запуски без ⛔ |
| analyst | sonnet | промпты, отбор, оценка качества | `analysis_pipeline/`, `campaign/`, SQL-диагностика | менять сбор |
| db | sonnet | миграции и откаты | `migrations/`, `scripts/rollback/` | удалять данные |
| reviewer | opus | ревью диффа перед PR | читать, ruff, pytest | править код |
| deployer | sonnet | команды VPS, ops | `docs/`, `scripts/`, compose | ходить на сервер |

## 6. Скиллы (`.claude/skills/`)
`add-apify-source`, `new-migration`, `diagnose-campaign`, `deploy-vps`, `add-llm-stage`, плюс существующий
`real-estate-pipeline`. Скиллы для самого архитектора-продукта (`task-real-estate`, `task-investors`,
`task-post-link`) создаются в задаче C1.

---

## 7. Золотой набор (заполняет владелец, см. §8)
| # | Тип | Задача | Что считается хорошим результатом |
|---|---|---|---|
| G1 | RE | участок, Мадрид, покупка, ≥ 2000 м² | реальные участки ≥ 2000 м² в провинции Мадрид, с ценой и телефоном |
| G2–G5 | RE | (владелец) | (владелец) |
| G6 | инвесторы | пост-ссылка FB с комментариями | (владелец) |
| G7–G8 | инвесторы | (владелец) | (владелец) |
| G9 | both | (владелец) | (владелец) |

---

## 8. Что нужно от владельца (блокеры отмечены ⛔)

| # | Что | Зачем | К когда |
|---|---|---|---|
| O1 ⛔ | Решение клиента по `COMPLIANCE.md` §4: Apify обрабатывает содержимое групп FB и комментарии (персональные данные, GDPR) — письменно | A4, B1, B3 | пятница |
| O2 ⛔ | Apify: тариф, лимит трат в Apify Console (например $30/мес), токен уже на VPS | все Apify-задачи | пятница |
| O3 | «Ок» на маленькие живые запуски (≤ $0.05 каждый) | A3, B5 | пятница |
| O4 ⛔ | `ANTHROPIC_API_KEY` в `.env` на VPS (Claude Console → проект «Real Estate Bot» → лимит трат); в чат не присылать | C1, C5 | суббота |
| O5 ⛔ | «Ок» на новую зависимость `claude-agent-sdk` в образ `architect` | C1 | суббота |
| O6 | Список групп FB: недвижимость (10–20) и инвесторы (5–10); аккаунт FB для браузера (уже есть) — готовность вступать в закрытые группы | A2, B3 | пятница |
| O7 | Золотой набор: 8–9 задач и что считается хорошим результатом, 2–3 ссылки на посты с комментариями | A6, D1 | суббота |
| O8 | Telegram ID владельца для алертов (`OWNER_ALERT_CHAT_ID`) | C3 | воскресенье |
| O9 | Окно для перезагрузки VPS (висит «System restart required») | C4 | воскресенье |
| O10 | Бюджет кампании по умолчанию (`CAMPAIGN_BUDGET_USD`, сейчас $5) | DoD-5 | пятница |

## 9. Риски
| Риск | Мера |
|---|---|
| Акторы Apify меняют формат выдачи | закреплённые билды, фикстуры из живой выдачи, алерт «0 объявлений у портала» |
| Facebook банит профиль браузера | лимиты безопасности остаются (`SAFETY_*`), Apify для открытых групп снижает нагрузку на профиль |
| Не успеваем C1 | линия отсечения: архитектор на текущем `understanding.py` + Sonnet 5.5 |
| Расходы | `CAMPAIGN_BUDGET_USD`, потолки на запуск у каждого актора, лимит в Apify Console |
| GDPR по лидам | хранить только публичное (имя, ссылка, цитата), срок хранения, удаление по запросу — в `COMPLIANCE.md` |
