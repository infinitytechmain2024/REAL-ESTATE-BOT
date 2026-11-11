# REAL-ESTATE-BOT v3: разбивка задач

Дата: 2026-11-10. База: `main` @ `7247fb5`. Мастер-план: `docs/v3/MASTER_PLAN.md`. Статусы: `docs/v3/tasks.json`.
Ссылки `файл:строка` сверены с кодом этой базы.

Общие правила для каждой задачи:
- Всё новое за флагом и выключено по умолчанию. Новая переменная попадает в settings, в `docker-compose.yml`
  (нужный сервис) и в `.env.example`; `tests/test_deployment.py:84-117` это проверяет.
- Тесты только на моках: `httpx.MockTransport`, фейковые store. Postgres-тест пропускается без
  `SYSTEM_TEST_DATABASE_URL` и вносится в явный список job `integration` (`.github/workflows/ci.yml:110-119`).
- Номер миграции здесь плановый. При мерже берётся следующий свободный после последнего в `main`. Файл
  регистрируется в обоих списках `scripts/apply_migrations.sh` (`:16`, `:81`), откат кладётся в
  `scripts/rollback/NNN_*_down.sql`, строка добавляется в `docs/v3/ROLLBACK.md`.
- Приёмка общая для всех задач: `.venv/bin/ruff check .` и `.venv/bin/python -m pytest -q` зелёные. Ниже указаны
  только команды, специфичные для задачи.
- ⛔ означает, что нужна остановка и «ок» владельца. Номер стопа — из `KICKOFF.md` `<stop_conditions>`.

---

## 0. Что выяснилось при сверке с кодом

| # | Kickoff / допущение | В коде | Следствие для плана |
|---|---|---|---|
| 1 | «`/forget` покрывает новые таблицы» (P3) | В живом боте `/forget` нет. Он есть только в `legacy/bot/handlers/privacy.py:45` и `legacy/bot/main.py:81`. Меню владельца без него (`bot/control_plane/menu.py:11-24`) | P3.4 пишет `/forget` с нуля, а не расширяет существующий |
| 2 | «хранение 90 дней» | Удаляющей задачи нет. `CAMPAIGN_LEAD_DAYS=90` только фильтрует чтение (`bot/campaign/settings.py:144`, `COMPLIANCE.md:65-66`). Удаляет только `bot/facebook_collector/apify/retention.py` (сырые данные Apify, 7 дней) | P3.3 — новая задача очистки, ⛔ стоп 4 |
| 3 | «`LiveViewController`» | В telegram это `LiveViewCoordinator` (`bot/control_plane/live_view.py:160`). `LiveViewController` — класс сервиса браузера (`bot/browser_session/live.py:49`) | В задачах используются оба имени, каждое на своём месте |
| 4 | «Окно входа открывает только владелец» | Его открывает любой staff (owner, helper, operator): `is_operator` (`live_view.py:174`), `/login` (`service.py:518`). Только кнопка панели доступна одному владельцу (`service.py:315`) | P2.1 и P2.6 переводят окно на «свой профиль — своё окно» |
| 5 | Внешняя ссылка (D12) | Уже есть `?for=<uid>` с подтверждением в чате: не больше 3 запросов, 600 с (`live_view.py:189`, `:249-288`, `:139-140`). Образец одноразового токена — `bot/verification/tokens.py:13-18` и таблица `verification_access_tokens` (`009_verification_flow.sql:22-34`) | P2.8 строит ссылку на этом образце: 10 минут, один раз, без подтверждения в чате |
| 6 | «Виртуальный экран фиксированного размера» | Размер зашит дважды: в Xvfb `1440x1000x24` (`docker/browser-session.Dockerfile:23`) и во viewport Playwright (`bot/browser_session/manager.py:78`). noVNC масштабирует только картинку, `resize=scale` (`live_view.py:406`) | P2.7: RANDR + `xrandr` (пакет в образ, ⛔ стоп 2, O9) и смена viewport |
| 7 | Один профиль на сеть | Индекс `browser_profiles_one_in_use_platform_idx` разрешает один `in_use` на платформу глобально (`003_orchestration.sql:64-65`). Имя профиля `<platform>-main` (`service.py:527`) | P2.2 меняет индекс на «один на (пользователь, платформа)», а очередь P2.4 обеспечивает «один браузер за раз» |
| 8 | Очередь профилей (D10) | Очереди нет. Окно входа одно и отказывает, пока есть другая сессия (`bot/browser_session/live.py:80-83`). Занятый браузер отвечает 409 (`bot/browser_session/main.py:79-80`) | P2.4 — новая очередь FIFO в Redis |
| 9 | Прокси на профиль (D11) | У браузера прокси нет (`manager.py:76-81`). Единственный прокси — `WEB_SEARCH_PROXY_URL` для веб-этапа (`docker-compose.yml:656`) | P2.5 передаёт `proxy` в `launch_persistent_context` (`manager.py:527-532`) |
| 10 | Шифрование (D20) | `cryptography` нет ни в `requirements*.txt`, ни в `docker/requirements-*.txt` | P2.3 ⛔ стоп 2: `cryptography` (Fernet), O9 |
| 11 | «Роли operator и helper удаляются» | Роли используются в 6 модулях: `bot/operators.py:22-24`, `service.py` (около 15 мест), `bot/control_plane/main.py:113-185`, `bot/orchestra/dispatcher.py:80-144`, `bot/control_plane/auto.py:71-107`, `bot/verification/service.py:112-342`. **Helper сейчас проверяет находки вручную** (`STAFF_ROLES`, `operators.py:23-24`) | P2.1: проверку берёт владелец, а свою кампанию — её автор. Значения в БД не переписываются: helper/operator считаются user в коде (после ревью) |
| 12 | Контракт соцсетей | `AcquiredPost`/`Provenance` настроены на FB: regex id поста (`bot/acquisition/contracts.py:34`), `extra="forbid"` (`:47`), поля платформы нет. `profile_link` принимает только хосты FB (`bot/campaign/leads.py:82`) | P1.1 — общий контракт (платформа, профиль по сети) до источников IG/TT/LI |
| 13 | Учёт запусков Apify | `apify_runs.batch_id not null` ссылается на `acquisition_batches` FB (`046_facebook_apify_runs.sql:15`). Порталы ведутся в `web_listing_source_runs` (044). Посты по ссылке — в `campaign_post_reads` (048) | P1.1 — общая таблица `apify_source_runs` для новых соц-источников: claim, run id, биллинг один раз |
| 14 | Сменный анализ (D18) | У каждого этапа уже свой Protocol: `Interviewer` (`bot/control_plane/interviewer.py:61`), `Understander` (`understanding.py:177`), `SearchPlanner` (`bot/campaign/search_plan.py:331`), `RelevanceJudge` (`relevance.py:114`), `Reviewer` (`:289`), `Recommender` (`final_report.py:455`), `LeadJudge` (`leads.py:196`), `ReachJudge` (`reach.py:341`). Общий транспорт — `OpenRouterJSON.complete` (`bot/agents/llm.py:60-88`). У анализатора своего Protocol нет (`bot/analysis_pipeline/openrouter.py:406`) | P7.1 не переписывает этапы, а добавляет маршрутизатор и Protocol для анализатора |
| 15 | Лиды личные (D13) | `investor_leads` уникален по `(profile_key, post_url)` на всех (`026_campaign_comment_leads.sql:48`), колонки пользователя нет. `stored_people_of_campaign` фильтрует по ссылкам кампании, а не по владельцу (`bot/campaign/runs.py:958-977`). Это `HANDOFF.md` §4 п. 11 | P3.1: `owner_user_id` и смена уникальности, ⛔ стоп 4 |
| 16 | Реакции (D5) | Нигде не собираются. `investor_leads.comment_text not null`, роль только `investor|buyer` (`026:36`) | P4.1 — новая таблица `person_signals`. `investor_leads` не трогаем, кроме `owner_user_id` |
| 17 | Кнопка под карточкой (D6) | У карточки находки нет клавиатуры: `messenger.send` (`bot/campaign/runner.py:866`). `send_buttons` умеет только один ряд (`runner.py:187-191`). Колбэки обрабатывает сервис `telegram` (`service.py:242-257`), а не раннер | P4.3: несколько рядов, колбэк `people:finding:<id>` (≤ 64 байт) |
| 18 | Месячный лимит (D16) | Пользователь кампании — `campaigns.requested_by` (`014_campaigns.sql:10`). У `campaign_costs` колонки пользователя нет (042). Строки с `campaign_id = null` (интервью, голос) никому не приписаны (`costs.py:203-209`) | P5.1 считает через join; P5.3 — колонка `user_id` (можно отложить) |
| 19 | Reach в режиме `apify` | Отбрасываются только `site:` для instagram, x, reddit. **LinkedIn и TikTok идут в SearXNG дальше** (`bot/campaign/reach.py:168-186`) | P1.8: при включённом Apify-LinkedIn LinkedIn-`site:` тоже отключается |
| 20 | Пользователи | Таблица `public.users` в живом коде не используется. Одобренные хранятся в `telegram_operators` (`011_operator_access_requests.sql:20`), владелец — в `.env` `TELEGRAM_OPERATOR_IDS` (`bot/operators.py:50-55`) | «Пользователь» в v3 = строка `telegram_operators` с `state='approved'`, `users` не трогаем |
| 21 | Настройки пользователя (D15) | Таблицы нет. `control_settings` глобальная, ключ `auto_mode` (`017_control_settings.sql:7-14`, `bot/control_plane/auto.py:22`) | P3.5 — `user_settings`, общий слой для P4, P5, P6 |
| 22 | Мини-апп | Сервера мини-аппа нет. Есть гейт aiohttp в `telegram:8081` (`live_view.py:410-552`) и Caddy `/live/*` (`docker/caddy/Caddyfile:22-24`); всё прочее отдаёт 404 (`:35-37`) | P6.1: маршруты `/app/*` в том же гейте, новая строка в Caddyfile |
| 23 | Состояние v2 | `tasks.json` v2 отмечает A3 как `done`, а `HANDOFF.md` §1 — как blocked. Решения O1–O10 записаны в `docs/v2/CLIENT_DECISIONS.md:71-88` | P0.5 сводит HANDOFF v2 с фактами |
| 24 | Зависимость P6 → P2 | Раздел «пользователи и входы» требует статусы сетей (P2.6) | P6.3 зависит от P2.6. Таблица фаз KICKOFF это не показывает |
| 25 | Зависимость P7 → P2 | Ключ пользователя хранится шифрованным (P2.3) | P7.2 зависит от P2.3 |
| 26 | Сигналы P4 → P1 | Реакции приходят из P1.6, а хранение с владельцем требует P3.1 | P1.6 даёт только контракт и мапперы без таблицы, а хранит P4.1 |
| 27 | Ревью плана (Opus, 2026-11-11) | 20 находок, все блокирующие и важные подтверждены по коду | Исправления внесены в задачи ниже, сводка — §5 |

---

## 1. Задачи

### Поток `v3/rules` (фаза R)

#### R1 — Правки правил отдельным PR ⛔ стоп 5
**Цель.** Убрать противоречия между D1, D9, D17, D5 и правилами. Закрывает D1, D9, D17, D5.
**Файлы.** `CLAUDE.md` п. 10. `docs/v2/KICKOFF_FABLE.md:50` (запрет «автоматизации LinkedIn-аккаунта») и `:51`
(строка про подписку). `COMPLIANCE.md`: новый раздел
§2.5 «v3» и §4a «вопросы клиенту по v3». Текст правок — `docs/v3/RULE_CHANGES.md`.
**Приёмка.** Владелец видит дифф и отвечает «ок». PR `v3/rules` с зелёным CI.
**Соседи.** Субагенты читают `CLAUDE.md`, поэтому до мержа R1 исполнители P2 работают по старому п. 10. LinkedIn
через браузер с входом до мержа не трогать.
Исполнитель: **architect** (главная сессия). Deps: нет. 1.5 ч. Ветка `v3/rules`.

#### R2 — Агенты на Opus, ссылки на v3, агент `webapp`
**Цель.** Подготовить команду. **Файлы.** `.claude/agents/{builder,sources,analyst,db,deployer}.md` (`model: opus`,
ссылки `docs/v3/TASKS.md`, правила коммита `feat(v3)`), `architect.md`, `reviewer.md` (ссылка на v3) и новый
`webapp.md`. **Приёмка.** `grep -L 'model: opus' .claude/agents/*.md` пуст.
Исполнитель: **architect**. Deps: нет. 0.5 ч. Ветка `v3/plan` (входит в PR плана).

#### R3 — Доска v3
**Цель.** Обобщить `scripts/build_v2_board.py`: `--plan v2|v3` (по умолчанию `v2`, поведение прежнее), пути
`docs/<plan>/tasks.json|board.template.html|board.html`. Скрипт падает на неизвестной зависимости и на цикле
(функция `check` уже это делает, `build_v2_board.py:20-39`). Шаблон доски `docs/v3/board.template.html` — копия
шаблона v2.
**Тесты.** `tests/test_board.py`: неизвестная зависимость → код 1; цикл → код 1; v2 без флага собирается как раньше.
**Приёмка.** `python3 scripts/build_v2_board.py --plan v3` выводит `docs/v3/board.html: N tasks`.
Исполнитель: **builder**. Deps: нет. 1 ч. Ветка `v3/plan`.

---

### Поток `v3/ops` (фаза P0, выполняет владелец на VPS)

#### P0.1 — Чек-лист включения v2
**Цель.** Свести `docs/v2/DEPLOY.md` §0–§4 в короткий чек-лист `docs/v3/P0_CHECKLIST.md`. Порядок: бэкап →
`update.sh` → флаги (а)–(з) → A4 → проверки §4. У каждого блока есть «норма» и откат. Закрывает D21.
**Приёмка.** В каждом блоке есть команда, норма и откат. Ссылки ведут на строки `DEPLOY.md`.
Исполнитель: **deployer**. Deps: нет. 1.5 ч.

#### P0.2 — ⛔ A4: Apify для групп FB (стоп 1, 3)
Владелец выполняет `DEPLOY.md` §3 (в): одна открытая группа, не больше 50 постов, потолок $0.30. Норма:
`select status, items_imported, billed_usd from apify_runs order by launched_at desc limit 1` → `SUCCEEDED`,
`items_imported > 0`, стоимость совпадает с Apify Billing.
Исполнитель: **owner**. Deps: P0.1. 0.5 ч.

#### P0.3 — ⛔ D2: деплой и флаги v2 (стоп 3)
По `P0_CHECKLIST.md`: бэкап, `BRANCH=main ./scripts/update.sh`, блоки (а)–(з), алерт watchdog не позже чем через
2 минуты. Норма — `DEPLOY.md` §4.
Исполнитель: **owner**. Deps: P0.1. 2 ч.

#### P0.4 — ⛔ D1: базовая линия G1–G9 (стоп 1)
Девять кампаний золотого набора. Потолок — $7 суммарно (подтверждён владельцем 2026-11-11). Базовая линия снимается
только после включения флагов v2; прогон «до флагов» (`docs/v2/GOLDEN_SET.md:29`) не нужен — решение владельца 2026-11-11. Бюджет считается только на кампанию (`costs.py:317-326`),
поэтому на время прогона `CAMPAIGN_BUDGET_USD=0.8` и лимит в Apify Console; после — вернуть `5`. Числа записываются в
`docs/v2/GOLDEN_SET.md` по `GOLDEN_SET.md` «Как снять базовую линию».
Исполнитель: **owner** (+ analyst разбирает вывод). Deps: P0.2, P0.3, O7. 2 ч.

#### P0.5 — D3: закрыть v2
`docs/v2/HANDOFF.md`, `ROLLBACK.md`, `CLIENT_GUIDE.md` сводятся с фактами P0.2–P0.4 (в том числе с расхождением про
A3, §0 п. 23). В `docs/v2/tasks.json` задачи A4, D1, D2, D3 переводятся в `done`.
Исполнитель: **architect**. Deps: P0.4. 1 ч.

---

### Поток `v3/sources` (фаза P1)

#### P1.0 — Выбор акторов и сверка схем (без запусков)
**Цель.** Закрепить акторы для D2–D5, сверив их схемы входа и выхода через `fetch-actor-details`. Результат — таблица
`docs/v3/ACTORS.md`: актор, цена, вход, поля выхода, нужные нам поля и есть ли они.
Кандидаты (найдены в Apify Store 2026-11-10, цены уровня FREE):

| Назначение | Актор | Цена |
|---|---|---|
| IG посты (хэштеги, ссылки) | `apify/instagram-scraper` | $0.0027 / результат |
| IG комментарии | `apify/instagram-comment-scraper` | $0.0026 / комментарий |
| TT посты | `clockworks/tiktok-scraper` (сверить) | — |
| TT комментарии | `clockworks/tiktok-comments-scraper` | $0.00125 / комментарий |
| LI люди | `harvestapi/linkedin-profile-search` (**без** режима e-mail) | $0.10 / страница до 25 профилей |
| LI посты + реакции + комментарии | `harvestapi/linkedin-post-search` | $0.002 / пост, реакцию, комментарий |
| LI компании | сверить `harvestapi/*company*` | — |
| FB реакции | `apify/facebook-likes-scraper` | $0.0062 / реакция |
| FB репосты | нет публичного актора (P1.0): только счётчик `shares` | — |
| Краулер сайтов | `apify/website-content-crawler` (сверить) | — |

**Приёмка.** В `ACTORS.md` у каждой строки есть «поля подтверждены схемой: да/нет». Все акторы без cookie;
актор, требующий cookie или входа, отклоняется.
Исполнитель: **sources**. Deps: нет. 3 ч. Без затрат (`search-actors` и `fetch-actor-details` бесплатны).

#### P1.1 — Общий контракт соцсетей и журнал запусков
**Цель.** Источники IG/TT/LI ложатся в тот же каркас, что FB. Закрывает D1, D2.
**Файлы.**
- `bot/acquisition/contracts.py`: `Provenance.platform: Literal["facebook","instagram","tiktok","linkedin"] =
  "facebook"` (старые строки валидны). `platform_post_id` (`:136`) выбирает regex по платформе.
- Новое `bot/acquisition/people.py`: `Engagement(platform, kind: comment|reaction|repost, author, profile_key,
  profile_url, text, reaction_type, post_url, raw_id)` и `profile_link(platform, href)` — нормализация по сети.
  FB-ветка переносится из `bot/campaign/leads.py:71-103`, в `leads.py` остаётся обёртка с тем же поведением.
- Миграция **051** `apify_source_runs`: `(id, campaign_id, owner_user_id null, platform, purpose, actor_id,
  actor_build, input_hash, run_id, dataset_id, status, max_charge_usd, billed_usd, items_imported,
  launched_at, finished_at)`, уникальность `(campaign_id, purpose, input_hash)`, `run_id` по образцу `pending:<uuid>`
  (`HANDOFF.md` §4 п. 2).
- Новое `bot/acquisition/apify_source.py`: `ApifySocialRun` — claim, запуск через `ApifyHttp.launch`
  (`apify_client.py:145`, всегда с `max_charge_usd`), checkpoint run id, `costs.record_unique("apify:<run_id>")`,
  постраничное чтение `dataset_page` (`:174`).
**Настройки.** Нет (флаги заводятся в источниках).
**Тесты.** `tests/test_acquisition_contracts.py` (старые FB-строки валидны, `platform` по умолчанию `facebook`,
id поста IG/TT/LI); `tests/test_apify_source_run.py` (запуск один раз, `LaunchUnknown` → нет повторного запуска,
биллинг один раз, `over_budget` до запуска); Postgres-тест таблицы 051.
**Соседи.** `facebook_collector/apify/mapper.py` и `post_leads.py` используют `Provenance`. Умолчание `facebook`
сохраняет их поведение.
Исполнитель: **sources + db**. Deps: P1.0. 9 ч.

#### P1.2 — Instagram через Apify (D2)
**Цель.** Посты с объектами (хэштеги и слова из плана) и люди (авторы и комментаторы).
**Файлы.** Новое `bot/acquisition/apify_instagram.py`: `ApifyInstagramPosts` (вход `search`/`directUrls`,
`resultsLimit`) → `AcquiredPost(platform="instagram")` → `collected_post_row()`;
`ApifyInstagramComments` (`directUrls`, `resultsLimit`, `includeNestedComments`) → `Engagement(kind="comment")`.
Настройки — в `bot/campaign/settings.py` рядом с `:154-185`.
**Настройки.** `APIFY_INSTAGRAM_ENABLED` bool `false`; `APIFY_INSTAGRAM_POSTS_ACTOR` str
`apify/instagram-scraper`; `APIFY_INSTAGRAM_COMMENTS_ACTOR` str `apify/instagram-comment-scraper`;
`APIFY_INSTAGRAM_ACTOR_BUILD` str пустая (без билда сервис не стартует при включённом флаге,
`check_pinned_build`, `apify_client.py:65`); `APIFY_INSTAGRAM_MAX_RESULTS` int 30 (1–50);
`APIFY_INSTAGRAM_MAX_CHARGE_USD` float 0.20. Сервис `campaign-runner` (уже в `egress`).
**Тесты.** `tests/test_apify_social.py::instagram_*`: тело запуска, маппинг поста (цена и площадь только из текста
через анализ, а не маппером), маппинг комментария, ошибка `post_unavailable`, пустая выдача, флаг выкл → источник
не строится.
**Соседи.** `BLOCKED_HOSTS` (`bot/web_search/urls.py:23-25`) не трогаем: страницы IG по-прежнему не загружаются
своим загрузчиком.
По ревью P1.1 (касается P1.2–P1.6): `ApifySocialRun` считает 429 попыткой (`max_attempts`=3); источник
либо передаёт свой `max_attempts`, либо 429 не считается попыткой — решить в P1.2 и покрыть тестом.
Исполнитель: **sources**. Deps: P1.1. 5 ч.

#### P1.3 — TikTok через Apify (D2)
То же, что P1.2, для `clockworks/tiktok-scraper` и `clockworks/tiktok-comments-scraper`. Настройки
`APIFY_TIKTOK_*` с теми же умолчаниями, `MAX_CHARGE_USD` 0.15. Тесты в `tests/test_apify_social.py::tiktok_*`.
Исполнитель: **sources**. Deps: P1.1. 4 ч.

#### P1.4 — LinkedIn: люди и компании без cookie (D3)
**Цель.** Инвесторский поиск по LinkedIn через Apify вместо сниппетов SearXNG. Объекты в LinkedIn не ищем.
**Файлы.** Новое `bot/acquisition/apify_linkedin.py`: `ApifyLinkedInPeople` (`searchQuery`, `locations`,
`profileScraperMode` = короткий профиль, **режим с поиском e-mail запрещён** константой и тестом) и
`ApifyLinkedInCompanies`. Выдача ложится в `reach_contacts` (`027_investor_reach.sql:34`) через существующий
`OpenRouterReachJudge` (`bot/campaign/reach.py:410-447`), а `kind` — человек или компания (028).
**Настройки.** `APIFY_LINKEDIN_ENABLED` `false`; `APIFY_LINKEDIN_PEOPLE_ACTOR`, `APIFY_LINKEDIN_COMPANIES_ACTOR`,
`APIFY_LINKEDIN_ACTOR_BUILD`; `APIFY_LINKEDIN_MAX_PAGES` int 1 (1–4); потолок запуска считается как `MAX_PAGES × APIFY_LINKEDIN_USD_PER_PAGE
(0.10) + 0.05` и не выше `APIFY_LINKEDIN_MAX_CHARGE_USD` (0.45); иначе вторая страница недостижима.
**Тесты.** `tests/test_apify_linkedin.py`: вход без e-mail-режима, маппинг человека и компании, профиль
`linkedin.com/in/…` нормализуется, cookie-поля во входе отсутствуют (тест сканирует тело запроса).
**Соседи.** `reach.keep_query` (`reach.py:178-186`) правит P1.8, а не эта задача.
⛔ **Стоп 7 (2026-11-11).** Три блокирующих ревью подряд по `clean_text` (вырезание телефонов из текста LinkedIn):
суммы тикетов → слитные `+34…` → суммы с `+`/`euros`. Ждёт решения владельца о подходе (см. отчёт).
Исполнитель: **sources**. Deps: P1.1. 5 ч.

#### P1.5 — Краулер сайтов клиента (D4)
**Цель.** Сайты из списка O2 читает универсальный краулер Apify. SearXNG и свой загрузчик остаются запасным путём
(`_apify_gate`, `bot/web_search/worker.py:399-412`).
**Файлы.** `bot/web_search/sources/crawler.py`: `ApifyCrawlerSource(ApifyListingSource)`. `_plan` строит
`startUrls` по сайтам и задаче (`apify.py:121`), `_normalize` (`:126`) применяет правило «что считать объявлением»
к каждому сайту: regex URL объявления и обязательные признаки (цена с валютой). Без признаков строка отбрасывается
со skip-записью. Список сайтов — `config/crawler_sites.yaml`, без кода: хост, `listing_url_regex`, `index_urls`,
сделка и тип. Регистрация — `WebSearchSettings.sources()` (`bot/web_search/settings.py:209-280`).
**Настройки.** `APIFY_CRAWLER_ENABLED` `false`; `APIFY_CRAWLER_ACTOR`, `_ACTOR_BUILD`; `APIFY_CRAWLER_MAX_PAGES`
int 30 (1–50, предел `ApifyListingSource`, `apify.py:81`); `APIFY_CRAWLER_MAX_CHARGE_USD` 0.20;
`APIFY_CRAWLER_SITES_FILE` str `config/crawler_sites.yaml`.
**Деньги (по P1.0, `docs/v3/ACTORS.md`).** `apify/website-content-crawler` оплачивается за compute, а не за события,
поэтому `maxTotalChargeUsd` (передаётся всё равно, правило 11) его расход **не ограничивает**. Потолок держат
`maxCrawlPages` = `APIFY_CRAWLER_MAX_PAGES`, `maxCrawlDepth` (`APIFY_CRAWLER_MAX_DEPTH` int 1, 0–3), память
(`APIFY_CRAWLER_MEMORY_MB` int 1024) и таймаут запуска (`APIFY_CRAWLER_TIMEOUT_SECONDS` int 300). Стоимость в
`campaign_costs` берётся из `usageTotalUsd`, до запуска бронируется оценка `MAX_CHARGE_USD`. Решение владельца
2026-11-11: потолок через страницы, глубину, память и таймаут; актор остаётся `apify/website-content-crawler`.
**Тесты.** `tests/test_apify_crawler.py`: план по двум сайтам, страница объявления против страницы поиска,
цена без валюты → `None`, площадь участка отдельно от дома, сайт вне списка отбрасывается, кривой YAML → источник
выключен с ошибкой в логе (кампания не падает).
**Соседи.** `classify_url` (`bot/web_search/urls.py`) должен знать новые хосты, иначе `_PortalSource._url`
отбросит строку. Хосты берутся из YAML, а не из `SPAIN_PORTALS`.
Исполнитель: **sources**. Deps: P1.0, O2. 7 ч.

#### P1.6 — Реакции и репосты: мапперы (D5)
**Цель.** Там, где актор отдаёт людей с реакцией или репостом, превращать их в `Engagement(kind="reaction"|"repost")`.
Обязательно — FB. LinkedIn-реакции — за линией отсечения.
Хранение и сила сигнала — P4.1.
**Файлы.** `bot/acquisition/apify_facebook_reactions.py` (`apify/facebook-likes-scraper`: тип реакции, имя,
профиль); LinkedIn-реакции и комментарии к посту по ссылке (`harvestapi/linkedin-post-search` с
`scrapeReactions`, `scrapeComments`). По P1.0 (`docs/v3/ACTORS.md`): лайкеров IG и TT публичные акторы не отдают,
репостнувших списком не отдаёт ни один актор (FB, IG, TT, LI) — только счётчики. Поэтому мапперы только FB и LI,
`Engagement(kind="repost")` пока никто не наполняет, а число репостов хранится счётчиком у поста.
**Настройки.** `APIFY_FACEBOOK_REACTIONS_ENABLED` `false`, `_ACTOR`, `_ACTOR_BUILD`, `_MAX_RESULTS` 100 (1–500),
`_MAX_CHARGE_USD` 0.70; `APIFY_LINKEDIN_POST_REACTIONS_ENABLED` `false` и такие же поля.
**Тесты.** `tests/test_apify_reactions.py`: маппинг типов (`like`, `love`…), строка без профиля отбрасывается,
пустая выдача, потолок передаётся.
Исполнитель: **sources**. Deps: P1.1. 4 ч.

#### P1.7 — ⛔ Живые сэмплы новых акторов (стоп 1, O6)
Каждый актор запускается один раз, не больше 5 строк. Потолки:

| Актор | Строк | Потолок |
|---|---|---|
| `apify/instagram-scraper` | 5 | $0.02 |
| `apify/instagram-comment-scraper` | 5 | $0.02 |
| `clockworks/tiktok-scraper` | 5 | $0.03 |
| `clockworks/tiktok-comments-scraper` | 5 | $0.01 |
| `harvestapi/linkedin-profile-search` | 1 страница | $0.15 |
| `harvestapi/linkedin-post-search` (реакции, комментарии) | 5 | $0.03 |
| `apify/facebook-likes-scraper` | 5 | $0.04 |
| краулер, 1 сайт из O2 (compute: 5 страниц, глубина 1, 1024 МБ, 120 с) | 5 страниц | ≈ $0.05 (оценка, не потолок) |
| **Итого** | | **≤ $0.35** |

Проверить живьём (по ревью P1.6): раскрывает ли `apify/facebook-likes-scraper` share-ссылки `/share/p|v|r/<token>`
и `fb.watch/<id>` (их даёт «Поделиться → Копировать ссылку» в мобильном FB). Если да — разрешить их в
`normalize_post_url`. Как актор считает `resultsLimit` (на пост или на запуск), точные слова `reaction`, вид `error`.
**Ход P1.7 (2026-11-11, «ок» O6 до $0.35, каждый запуск — с подтверждением владельца).**
- `apify/instagram-scraper` и `apify/instagram-comment-scraper`: по 5 строк, ≈ $0.027 суммарно; поля совпали,
  новое — `likesCount = -1` (скрытые лайки) → `None`.
- `clockworks/tiktok-scraper`: Apify отказал на старте — **минимальный `maxTotalChargeUsd` у актора $0.50** (в карточке
  цен не виден). Следствие: `APIFY_TIKTOK_MAX_CHARGE_USD=0.15` (P1.3) даёт 4xx на каждом запуске → задача **P1.3a**.
- Остальные акторы могут иметь такой же минимум: каждый сэмпл сначала проверяет это (отказ на старте бесплатен).
Выдача становится фикстурой с заменённой PII, мапперы правятся по реальным полям, `ACTORS.md` получает пометку
«проверено живьём <дата>». Расход заносится в отчёт фазы.
Исполнитель: **sources**. Deps: P1.2, P1.3, P1.4, P1.5, P1.6, O6. 3 ч.

#### P1.8 — Проводка источников в кампанию
**Цель.** При `ACQUISITION_MODE=apify` социальная стадия идёт через Apify. Браузерная стадия сейчас выключается
(`runner.py:1612`), вместо неё появляется `_apify_social_worker`. LinkedIn в reach — через P1.4.
**Файлы.** `bot/campaign/runner.py:1610-1620` (новая ветка при `apify` и включённых флагах IG/TT);
`bot/campaign/reach.py:168-186`: при `APIFY_LINKEDIN_ENABLED` в пропуски добавляются платформа `linkedin` и хост
`linkedin.com` (`_APIFY_SKIPPED_HOSTS`, `:169`); у `keep_query` (`:178`) появляется параметр `linkedin_apify: bool`,
а вызывается Apify-источник. `runner.py:576-577` (ожидание `social_activity` в окне ожидания) учитывает новые
запуски. Посты IG/TT отправляются в анализ существующим путём `collected_posts`.
**Тесты.** `tests/test_campaign_social_apify.py`: при `apify` с флагами браузер не вызывается, Apify вызывается
один раз на кампанию; без флагов поведение прежнее; LinkedIn `site:` не уходит в SearXNG при включённом
Apify-LinkedIn; `over_budget` → запуска нет.
**По ревью P1.2–P1.6 (обязательно в этой задаче).**
- Источники Apify вызываются отдельной задачей, а не внутри тика раннера, и не отменяются во время POST
  (`wait_for`/остановка сервиса посреди окна 429 оставит `launching` → `launch_unconfirmed` с бронью потолка).
- Одна цель (хэштег, запрос, ссылка) на запуск либо явное деление лимита: `resultsLimit`/`resultsPerPage` считаются на
  цель, а `maxItems` на прогон, поэтому первая цель съедает весь лимит, а вход сохраняется как `succeeded`.
- Единая политика 429: окно `rate_limit_seconds` (P1.2) для всех соц-источников, включая LinkedIn (P1.4 сейчас — 5
  попыток без окна; привести после мержа обоих).
- LinkedIn: `enriched_at` не ставится для LI-контактов (P1.4), иначе они съедают лимит обогащения reach.
- Ошибка «все ссылки отклонены» у реакций — `ValueError` (P1.6), вызывающий обрабатывает его как пустой результат.
**Соседи.** Отчёт `final_report.py` считает источники по платформам. Новая платформа не должна дать `KeyError`.
Исполнитель: **builder**. Deps: P1.2, P1.3, P1.4. 5 ч.

---

### Поток `v3/roles` (фаза P2)

#### P2.1 — Две роли: владелец и пользователь (D8)
**Цель.** Остаются владелец (из `TELEGRAM_OPERATOR_IDS`) и одобренный пользователь. Одобрение — одной кнопкой.
**Файлы.** `bot/operators.py:22-24` (`ROLES=("user",)`; staff = owner); `bot/control_plane/access.py:105-110`
(одна кнопка «Одобрить»), `:250` (кнопки ролей убираются); `/role` (`access.py:140`) удаляется из меню;
`service.py` (места helper/operator, §0 п. 11); проверка находок вручную (`bot/verification/service.py:112-113`,
`:164`, `:250`) — получатели: владелец и автор кампании.
**Данные не переписываются** (после ревью). При `ACCESS_TWO_ROLES=true` код считает `helper` и `operator` ролью
`user` (`bot/operators.py`), новые одобрения пишут `user`. Миграция **053** только меняет `default 'helper'`
(`011_operator_access_requests.sql:26`) на `'user'` и не трогает check и строки, поэтому старая кнопка «Approve as
helper» при `false` продолжает работать. Откат — одна строка `.env`; схема 053 откатывается возвратом default.
**Настройки.** `ACCESS_TWO_ROLES` bool `false`: при `false` код работает с тремя ролями, при `true` — с двумя.
**Тесты.** Расширить `tests/test_operator_access.py`: одна кнопка, одобренный — user, бывший helper видит то же,
что user; `test_visibility_gate.py`, `test_auto_mode.py`.
**Соседи.** `orchestra/dispatcher.py:80-85` отклоняет не-операторов: решить, кто запускает Orchestra (владелец).
Исполнитель: **builder** (+ db для 053). Deps: P0.3. 12 ч (в `service.py` ≈ 39 проверок ролей).

### Поток `v3/accounts` (фаза P2)

#### P2.2 — Профили на пользователя (D9)
**Цель.** У каждого пользователя свои профили 4 сетей. **Файлы.** Миграция **052**:
`browser_profiles.owner_user_id bigint null` (null — старый общий профиль владельца); индекс
`browser_profiles_one_in_use_platform_idx` (`003:64-65`) заменяется на `(coalesce(owner_user_id,0), platform)
where state='in_use'`. `bot/control_plane/store.py:202-216` (`ensure_profile` с владельцем); имя профиля
`<platform>-u<uid>` вместо `<platform>-main` (`service.py:527`, `:326`) при флаге.
**Настройки.** `PERSONAL_PROFILES_ENABLED` bool `false` (сервисы `telegram`, `browser`, `facebook-runner`,
`campaign-runner`).
**Тесты.** Postgres: два пользователя держат `in_use` на одной платформе одновременно, а одному нельзя два `in_use`;
без флага — `<platform>-main`.
**Соседи.** `facebook_collector` и `social_search` берут «профиль платформы» без пользователя: через
`campaigns.requested_by` нужно передать профиль автора кампании.
Исполнитель: **db + builder**. Deps: P0.3. 5 ч.

#### P2.3 — Шифрование секретов в БД (D20) ⛔ стоп 2 (O9)
**Цель.** `bot/utils/secretbox.py`: `seal(plaintext) -> bytes`, `open_(blob) -> str` на Fernet (`cryptography`).
Ключ — `SECRETS_ENCRYPTION_KEY` (32 байта, base64; `repr=False`). Без ключа `seal` падает, и функции, которым
нужны секреты, выключаются. Ротация: `SECRETS_ENCRYPTION_KEY_OLD` для чтения.
**Зависимость.** `cryptography==<закреплённая>` в `requirements.txt` и в образах `telegram`, `browser`,
`campaign-runner` (`tests/test_image_imports.py`). Обоснование: в stdlib нет AEAD; pgcrypto увёл бы ключ в SQL и в логи Postgres.
**Тесты.** `tests/test_secretbox.py`: туда и обратно, чужой ключ → ошибка, старый ключ читается, в `repr` и в логах
нет открытого текста.
Исполнитель: **builder**. Deps: O9. 3 ч.

#### P2.4 — Очередь профилей в одном браузере (D10)
**Цель.** Один браузер на VPS. Задания разных профилей идут по очереди, окно входа одно на всех.
**Файлы.** `bot/browser_session/queue.py`: FIFO в Redis (`LPUSH`/`BRPOP`, задание `{profile_id, kind:
read|login, user_id}`), один исполнитель; `main.py:79-80` вместо 409 ставит в очередь и возвращает позицию;
`live.py:80-83` берёт слот очереди. Вызывающие, которые сейчас берут профиль синхронно, переходят на ожидание
слота: `bot/web_search/render.py:93`, `facebook_collector`, `social_search`, окно входа. Telegram пишет пользователю: «Вы в очереди: N».
**Настройки.** `BROWSER_QUEUE_ENABLED` `false`; `BROWSER_QUEUE_MAX` int 20; `BROWSER_JOB_TIMEOUT_SECONDS` int 900.
**Тесты.** `tests/test_browser_queue.py` (фейковый Redis): порядок FIFO; окно входа не стартует, пока идёт чтение;
таймаут освобождает слот; падение задания освобождает слот.
**Соседи.** Аренда профиля в Redis и `flock` (`manager.py:188-222`) остаются, очередь работает над ними.
Исполнитель: **builder**. Deps: P2.2. 12 ч.

#### P2.5 — Жилой прокси на профиль (D11)
**Цель.** У каждого пользователя свой постоянный прокси, закреплённый за его профилями. Без подмены отпечатков.
**Файлы.** Миграция **054** `user_proxies(user_id pk, scheme, host, port, username, secret_ciphertext, country,
updated_at)`; `manager.py:527-532`: `proxy={server, username, password}` в `launch_persistent_context`;
`LAUNCH_OPTIONS` (`:76-81`) не меняются. Ввод прокси — команда владельца `/proxy <user>` и мини-апп (P6.3). Пароль
хранится через `secretbox`.
**Настройки.** `BROWSER_PROXY_REQUIRED` bool `false`: при `true` профиль без прокси не стартует и ошибка видна
пользователю.
Сообщение владельца с паролем прокси бот удаляет сразу после чтения (как ключ в P7.2).
**Тесты.** `tests/test_browser_proxy.py`: прокси передан в Playwright (мок), пароль не попадает в лог, профиль
без прокси при `REQUIRED` → отказ, при `false` → как раньше; тест-охранник: `LAUNCH_OPTIONS` не получает **новых**
аргументов подмены (UA, WebGL, canvas) сверх списка, утверждённого владельцем (O10). Уже существующие
`--disable-blink-features=AutomationControlled` и `ignore --enable-automation` (`manager.py:79-80`) задача не
трогает: это вопрос O10 к `COMPLIANCE.md` §5.
Исполнитель: **builder + db**. Deps: P2.2, P2.3, O3, O10. 5 ч.

#### P2.6 — Обязательное подключение 4 сетей, блок при слетевшем входе (D9)
O8 закрыт 2026-11-11: владелец подтвердил, что слетевший вход в любую из 4 сетей блокирует новые задачи,
даже если Apify справился бы без входа.
**Цель.** До первой задачи пользователь подключает Facebook, Instagram, TikTok и LinkedIn. Слетевший вход
блокирует новые задачи.
**Файлы.** `/connect`: список 4 сетей с кнопкой «Войти» (окно входа своего профиля, `live_view.py` `login`).
**Доступ к окну = свой профиль**: все проверки `is_operator` (`live_view.py:174`, `:251`, `:291`, `:419`, `:479`;
`service.py:518`) заменяются на «владелец или хозяин профиля»; иначе после P2.1 пользователь получит «Only operators».
Проверка «вход жив» — **новый** эндпоинт `POST /v1/profiles/{id}/check` в `bot/browser_session/main.py`: берёт профиль
через очередь P2.4, открывает контекст без окна и проверяет cookie по тем же правилам, что `live_view.py:301-305`.
Существующий `/v1/live/check` (`main.py:92-99`) проверяет только открытое окно и для этого не годится.
Запуск — раз в `ACCOUNT_CHECK_HOURS`. Блок задачи — в `intake` до карточки
«Проверьте задачу» (`bot/control_plane/intake.py`). Текст: какая сеть и кнопка «Войти».
**Настройки.** `ACCOUNT_GATE_ENABLED` `false`; `ACCOUNT_CHECK_HOURS` int 24 (1–168).
**Тесты.** `tests/test_network_gate.py`: 3 сети из 4 → задача не стартует; все 4 → стартует; вход слетел →
блок и одно сообщение; владелец без флага не блокируется.
**Соседи.** Автокампании Orchestra (`auto.py`) тоже проверяют блок.
Исполнитель: **builder**. Deps: P2.1, P2.2, P2.4, O8. 9 ч.

#### P2.9 — «Отключить аккаунт» (D20) ⛔ стоп 4
**Цель.** Кнопка у каждой сети в `/connect`. После подтверждения бот стирает каталог профиля
(`/profiles/<id>`, `manager.py:175-182`), и это записывается в лог аудита без ПДн.
**Как (после ревью).** Строку `browser_profiles` удалить нельзя: на неё ссылается `live_view_sessions` с
`on delete restrict` (`007_live_view_sessions.sql:6`). Поэтому удаление мягкое (`state='deleted'`), а повторное
подключение создаёт профиль с новым именем `<platform>-u<uid>-<n>`. Каталог лежит в volume сервиса `browser`
(`docker-compose.yml:178-180`), поэтому его стирает новый эндпоинт `DELETE /v1/profiles/{id}` в
`bot/browser_session/main.py`, а не telegram.
**Тесты.** Подтверждение обязательно; занятый профиль сначала освобождается; ошибка удаления видна честно;
повторное подключение после удаления работает.
Исполнитель: **builder**. Deps: P2.2, P2.4. 6 ч.

#### P2.10 — ⛔ Шифрованный раздел для профилей (стоп 3)
Runbook владельцу `docs/v3/VPS_ENCRYPTED_PROFILES.md`: LUKS-том под volume `browser_profiles`
(`docker-compose.yml:178-180`), перенос, проверка, откат, где хранить ключ разблокировки. Команды выполняет владелец.
Исполнитель: **deployer**. Deps: нет. 2 ч.

### Поток `v3/login` (фаза P2)

#### P2.7 — Адаптивный экран окна входа (D12) ⛔ стоп 2 (O9)
**Цель.** Виртуальный экран подстраивается под устройство.
**Файлы.** `docker/browser-session.Dockerfile:23`: Xvfb с запасом `-screen 0 2560x1600x24 +extension RANDR`,
пакет `x11-xserver-utils` (`xrandr`); `bot/browser_session/live.py`: `POST /v1/live` принимает `width,height`
(в пределах 360–2560 × 640–1600), вызывает `xrandr --fb WxH` и `page.set_viewport_size`; мини-апп передаёт
`window.innerWidth/innerHeight × devicePixelRatio` в `live_view.py` (страница гейта `:406`).
**Настройки.** `LIVE_VIEW_ADAPTIVE` `false`.
**Тесты.** `tests/test_live_adaptive.py`: размеры обрезаются по границам, команда `xrandr` собрана без инъекции
(список аргументов), без флага экран 1440×1000.
Исполнитель: **webapp**. Deps: P2.4, O9. 5 ч.

#### P2.8 — Одноразовая внешняя ссылка на 10 минут (D12)
**Цель.** Второй способ открыть окно: ссылка для обычного браузера. Живёт 10 минут, открывается один раз, только
свой профиль.
**Файлы.** Миграция **055** `live_view_links(token_sha256 pk, user_id, profile_id, expires_at, used_at)` по образцу
`009:22-34`; токен — `secrets.token_urlsafe(32)` с хранением SHA-256 (`bot/verification/tokens.py:13-18`).
**Гейт.** Middleware `known_ids_only` стоит на всём приложении и отдаёт 404 любому пути без `session`
(`live_view.py:421-425`, `:546`). Задача выносит его в подприложение `/live/{session}`, иначе `/live/link/*` (и
`/app/*` из P6.1) всегда 404.
`GET /live/link/<token>` только показывает страницу с кнопкой «Открыть», а гасит токен `POST` (предпросмотр ссылок
Telegram и префетч Safari иначе сожгут её). Ссылка уходит с `disable_web_page_preview`. Повтор → 410, после 10 минут
→ 410. Подтверждение в чате, которое есть сейчас (`:249-288`), остаётся по умолчанию: `LIVE_VIEW_LINK_CONFIRM` bool
`true`. Существующий путь `?for=` (`:189`) остаётся при выключенном флаге.
**Настройки.** `LIVE_VIEW_ONE_TIME_LINK` `false`; `LIVE_VIEW_LINK_MINUTES` int 10 (1–30).
**Тесты.** `tests/test_live_link.py`: одноразовость, срок, чужой профиль недоступен, токен не логируется.
Исполнитель: **webapp**. Deps: P2.4, P2.6. 6 ч.

---

### Поток `v3/privacy` (фаза P3)

#### P3.1 — Находки и лиды привязаны к пользователю (D13) ⛔ стоп 4
**Цель.** Закрыть `HANDOFF.md` §4 п. 11: общей базы между пользователями нет.
**Файлы.** Миграция **056**: `owner_user_id bigint` в `investor_leads`, `reach_contacts`,
`campaign_lead_deliveries`; заполняется из `campaigns.requested_by` по `campaign_id`. У строк без кампании —
владелец из `TELEGRAM_OPERATOR_IDS[0]`, и это записывается в лог миграции. После заполнения `owner_user_id not null` (в PG16 null
в уникальном ключе даёт дубли). Уникальность `investor_leads` `(profile_key, post_url)` (`026:48`) →
`(owner_user_id, profile_key, post_url)`, а `reach_contacts` `url_key` (`027_investor_reach.sql:36`) →
`(owner_user_id, url_key)`.
**В том же PR** меняются все `on conflict` и проверки «уже известно»: `save_leads` (`leads.py:791`),
reach (`reach.py:768`, `:779`, `save_enrichment` `:793`). Иначе вставка падает с «no unique constraint matching».
Флаг `PERSONAL_DATA_SCOPE` уникальность назад **не** откатывает: откат — только скриптом схемы.
Перед и после — `select count(*)` по таблицам (числа совпадают).
**Тесты.** Postgres: тот же комментатор у двух пользователей — две строки; миграция идемпотентна; откат отказывает,
если есть дубли по старому ключу.
Исполнитель: **db + builder**. Deps: P2.1. 8 ч.

#### P3.2 — Запросы людей по владельцу
**Файлы.** `bot/campaign/runs.py:881-907` (`stored_people`), `:958-984` (`stored_people_of_campaign`), `:935-949`
(reach) — фильтр `owner_user_id = <автор кампании>` (запись владельца уже сделана в P3.1).
**Настройки.** `PERSONAL_DATA_SCOPE` bool `false`: при `false` поведение прежнее.
**Тесты.** `tests/test_people_isolation_postgres.py` (в список `integration`): два пользователя, одна ссылка, каждый
видит только своих (AC-4).
Исполнитель: **builder**. Deps: P3.1. 4 ч.

#### P3.5 — Настройки пользователя `user_settings` (D15, основа D6 и D16)
**Файлы.** Миграция **057** `user_settings(user_id, key, value jsonb, updated_by, updated_at, pk(user_id,key))`;
`bot/control_plane/user_settings.py`: схема ключей pydantic (`people_delivery: off|button|auto`, `people_depth`,
`people_min_strength`, `monthly_limit_usd` — его пишет только владелец), store на asyncpg, `get/set` с проверкой
схемы. Один слой для чата и мини-аппа.
**Тесты.** Чужой пользователь не пишет `monthly_limit_usd`; неизвестный ключ — ошибка; значения по умолчанию
при пустой таблице.
Исполнитель: **db + builder**. Deps: P3.1. 3 ч.

#### P3.3 — Хранение 90 дней (D20) ⛔ стоп 4
**Цель.** Ежедневная очистка находок и лидов старше `PERSONAL_DATA_RETENTION_DAYS`.
**Файлы.** `bot/ops/retention.py` по образцу `bot/facebook_collector/apify/retention.py` (`--dry-run`, счётчики);
таблицы берутся из общего реестра `bot/ops/personal_tables.py` (его же использует `/forget`): `investor_leads`,
`reach_contacts`, `campaign_findings`; `person_signals` регистрирует P4.1. `collected_posts` общие для кампаний
разных пользователей (`003_orchestration.sql:192`): пост удаляется, только когда **все** ссылающиеся кампании
старше срока. Порядок: сначала дети, потом родители (`COMPLIANCE.md` §6). Запуск — из watchdog раз в сутки.
**Настройки.** `PERSONAL_DATA_RETENTION_DAYS` int `0` = выключено (владелец ставит 90 по чек-листу).
**Тесты.** `tests/test_retention_postgres.py`: 91 день удаляется, 89 остаётся, `--dry-run` ничего не удаляет,
`0` — ничего.
Исполнитель: **builder**. Deps: P3.1. 4 ч.

#### P3.4 — `/forget` в живом боте (D20) ⛔ стопы 4 и 5
**Цель.** Пользователь стирает всё о себе. Сначала бот спрашивает подтверждение, а неудачу удаления сообщает честно
(`COMPLIANCE.md` §6).
**Файлы.** `bot/control_plane/forget.py`: в одной транзакции удаляется всё по реестру `bot/ops/personal_tables.py`.
Реестр содержит только существующие таблицы; каждая задача, которая добавляет таблицу с пользователем (P2.5,
P2.8, P4.1, P7.2), регистрирует её там, и это часть её приёмки. Удаляются кампании пользователя (`requested_by`),
`investor_leads`/`reach_contacts`/`person_signals` по `owner_user_id`, `user_task_drafts`, `user_settings`,
`telegram_access_requests` (`telegram_user_id`, `display_name`, `011:4-12`). Расходы `campaign_costs` не
удаляются, а обезличиваются (`campaign_id`/`user_id` → null) до удаления кампаний: это учёт денег. После
коммита профили браузера стираются через P2.9. Строка `telegram_operators` переводится в `revoked`.
`menu.py:11-24` — команда в меню пользователя.
**Настройки.** `FORGET_ENABLED` `false`.
**Тесты.** `tests/test_forget_postgres.py`: после `/forget` по `user_id` ноль строк во всех таблицах (реестр
сверяется с `information_schema`: каждая таблица с колонкой `owner_user_id`, `user_id`, `telegram_user_id` или
`requested_by` должна быть в реестре или в явном списке исключений с причиной);
у другого пользователя всё осталось; ошибка → честное сообщение.
Исполнитель: **builder**. Deps: P3.1, P3.5, P2.9. 6 ч.

---

### Поток `v3/people` (фаза P4)

#### P4.1 — Модель сигналов (D5) ⛔ стоп 5 (O5)
**Файлы.** Миграция **058** `person_signals(id, owner_user_id, campaign_id, finding_id null, platform,
profile_key, profile_url, author_name, post_url, kind comment|reaction|repost, reaction_type null, text null,
strength smallint, seen_at, unique(owner_user_id, platform, profile_key, post_url, kind))`. Сила: комментарий
с текстом 3, репост 2, реакция 1 (таблица в `bot/campaign/signals.py`, без LLM). `bot/campaign/signals.py`:
`save_engagements(owner, campaign, list[Engagement])`. Комментарии из существующих чтений
(`post_leads.py:198-208`, `comment_reader.py`) тоже пишутся сюда; `investor_leads` остаётся таблицей лидов,
прошедших классификатор.
**Настройки.** `PERSON_SIGNALS_ENABLED` `false`.
**Тесты.** `tests/test_person_signals.py`: три вида дают разную силу; повтор не дублирует; без флага запись не
идёт.
Исполнитель: **db + builder**. Deps: P1.6, P3.1, O5. 5 ч.

#### P4.2 — Глубина и отбор под задачу
**Цель.** Сколько людей и какой минимальной силы показывать — из задачи (spec) и `user_settings`. Только сигналы с
текстом идут в `OpenRouterLeadJudge` (`leads.py:251-270`); реакции попадают в список «отметились» без LLM.
**Файлы.** `bot/campaign/signals.py` (`select_people(spec, settings)`), `leads.py` (вызов судьи только для
`kind=comment`).
**Тесты.** Глубина режет список; `min_strength=2` скрывает лайки; судья не вызывается для реакций.
Исполнитель: **analyst**. Deps: P4.1, P3.5. 3 ч.

#### P4.3 — Способ 1: кнопка «Кто интересовался» (D6)
**Файлы.** `runner.py:187-191`: `send_buttons` принимает ряды; `_send_card` (`:854-866`) при
`people_delivery=button` отправляет карточку с кнопкой `people:finding:<finding_id>`. `service.py:242-257`:
колбэк ставит чтение (комментарии + реакции, если включены) через `runs.queue_comment_read` (`runs.py:871-879`),
а ответ приходит карточками людей (`leads.py:390-414` `person_card`).
**Тесты.** `tests/test_people_delivery.py::button_*`: кнопка есть только при настройке; колбэк ≤ 64 байт;
повторное нажатие не запускает второе платное чтение; чужая находка → отказ.
Исполнитель: **builder**. Deps: P4.2. 5 ч.

#### P4.4 — Способ 2: ссылка на любой пост или объявление (D6)
**Цель.** `post_link` (`bot/control_plane/intake.py:342`) понимает ссылки IG, TT и LI, а не только FB. У порталов
комментариев нет, поэтому ветка порталов убрана (ревью). Выбор читателя — по `platform` (P1.1).
**Тесты.** `tests/test_people_delivery.py::link_*`: 4 сети, ссылка портала и неизвестный хост → понятный отказ,
стоимость в `campaign_costs`.
Исполнитель: **builder**. Deps: P4.2, P1.8. 4 ч.

#### P4.5 — Способ 3: автоматически с карточкой (D6)
`people_delivery=auto`: после отправки карточки `_queue_comments` (`runner.py:878-888`) ставит чтение для любой
платформы, а не только FB. Лимит `CAMPAIGN_COMMENT_MAX_POSTS` остаётся.
**Тесты.** `auto` — чтение поставлено, `button` — нет, `off` — нет.
Исполнитель: **builder**. Deps: P4.3. 2 ч.

#### P4.6 — Черновик первого сообщения (D7)
**Цель.** У каждого лида есть кнопка «Черновик». Бот готовит текст под задачу пользователя на языке комментария.
Бот **не отправляет** его: человек копирует сам.
**Файлы.** `bot/campaign/drafts.py`: этап LLM по скиллу `add-llm-stage` (строгий JSON, `costs.record("llm", …)`,
`over_budget` до вызова). Модель — `OPENROUTER_DRAFT_MODEL`. Охранный тест AC-6:
`tests/test_read_only_guard.py` проверяет, что в `bot/` нет вызовов отправки к FB/IG/TT/LI (Graph API send,
`page.fill` в поле комментария и т. п.).
**Настройки.** `LEAD_DRAFTS_ENABLED` `false`; `OPENROUTER_DRAFT_MODEL` пусто → функция выключена.
**Тесты.** Валидный ответ, ответ вне схемы, 400, таймаут; стоимость записана; в черновике нет выдуманных фактов
(промпт запрещает, а тест проверяет, что цифры есть в задаче или в комментарии).
Исполнитель: **analyst**. Deps: P4.2. 4 ч.

---

### Поток `v3/budget` (фаза P5)

#### P5.1 — Месячный лимит пользователя (D16)
**Файлы.** `bot/utils/costs.py`: `user_spent(user_id, month)` — join `campaign_costs` → `campaigns.requested_by`
(`kind='cost'`, `date_trunc('month', created_at)`). `over_budget` (`:317-326`) дополнительно проверяет лимит автора
кампании из `user_settings.monthly_limit_usd` (кэш на 60 с). Ошибка чтения не останавливает задачу (как сейчас), но
пишет `costs.user_budget_unreadable`. Владелец без лимита по умолчанию.
**Настройки.** `USER_MONTHLY_BUDGET_ENABLED` `false`.
**Тесты.** `tests/test_user_budget.py`: сумма по двум кампаниям, граница месяца, лимит не задан → без ограничений,
флаг выкл → только `CAMPAIGN_BUDGET_USD`.
Исполнитель: **builder**. Deps: P3.5. 4 ч.

#### P5.2 — Предупреждение на 80 %, стоп на 100 %
**Файлы.** Миграция **059** `user_budget_notices(user_id, month, level 80|100, sent_at, pk(user_id, month, level))`
— каждое уведомление один раз в месяц. Проверка — в `watchdog` по образцу `ops/watchdog.py:94-103`, сообщение
пользователю (`campaigns.telegram_chat_id`) и владельцу. На 100 % `intake` не принимает новую задачу, а идущие
останавливаются через `over_budget`. Строка в отчёте — `final_report.py:658-689` (`cost_lines`): «за месяц $X из
$Y».
**Тесты.** 79 % → ничего; 80 % → одно сообщение; 100 % → задача отклонена; новый месяц → снова доступно.
Исполнитель: **builder**. Deps: P5.1. 4 ч.

#### P5.3 — Расходы без кампании приписываются пользователю
Миграция **060** `campaign_costs.user_id bigint null`, `costs.record(..., user_id=)` в интервью, STT и
понимании задачи (`control_plane`). `user_spent` суммирует обе части.
**Тесты.** Интервью без кампании попадает в месяц пользователя.
Исполнитель: **db + builder**. Deps: P5.1. 3 ч. Не отрезать: без неё в месячный лимит не попадают интервью,
голос и мониторинг FB (`facebook_collector/apify/provider.py:153`), и D16 течёт (ревью).

---

### Поток `v3/webapp` (фаза P6)

#### P6.1 — Каркас мини-аппа
**Файлы.** `bot/control_plane/webapp/`: маршруты `/app/*` в гейте aiohttp telegram (`live_view.py:410-552`,
порт 8081). Вход — `POST /app/auth` с initData → `verify_init_data` (`bot/telegram_webapp.py:20`, `max_age=600`)
→ сессия в БД (миграция **062** `webapp_sessions`, переживает рестарт) и httponly-cookie. CSRF-токен выдаётся в
`/app/auth` и проверяется на каждом POST: initData живёт 600 с (`telegram_webapp.py:16`) и для CSRF не годится.
Гейт: используется подприложение из P2.8, иначе `known_ids_only` (`live_view.py:421-425`) отдаёт 404.
Права: owner — все разделы, user — только «Мои настройки». Статика — один HTML + vanilla JS,
без сборщика и новых зависимостей. `docker/caddy/Caddyfile`: `handle /app/* { reverse_proxy telegram:8081 }`
перед catch-all (`:35-37`). Кнопка мини-аппа — в меню владельца и в `/settings`.
**Настройки.** `OWNER_WEBAPP_ENABLED` `false`.
**Тесты.** `tests/test_owner_webapp.py`: без initData → 401; подделанная подпись → 401; user в разделе владельца → 403;
CSRF — POST без токена → 403; сессия переживает перезапуск.
Исполнитель: **webapp**. Deps: P3.5, P2.8. 8 ч.

#### P6.2 — Раздел «Система и расходы»
Состояние сервисов (тот же источник, что у watchdog, `ops/watchdog.py:205-217`), расходы по дням, пользователям и
этапам (`campaign_costs`), алерты за сутки. Только чтение.
Исполнитель: **webapp**. Deps: P6.1, P5.1. 4 ч.

#### P6.3 — Раздел «Пользователи и входы»
Одобрить или отозвать (то же, что `access.py:123`), статус 4 сетей по каждому (P2.6), прокси (P2.5), месячный
лимит (P5.1). Каждое изменение пишется в `user_settings`/`telegram_operators` теми же функциями, что и чат.
Исполнитель: **webapp**. Deps: P6.1, P2.6, P2.5, P5.1. 5 ч.

#### P6.4 — Раздел «Все задачи»
Кампании всех пользователей: состояние, расход, кнопка «Остановить» (тот же путь, что команда в чате).
Исполнитель: **webapp**. Deps: P6.1. 3 ч.

#### P6.5 — Раздел «Находки и лиды всех пользователей»
Только чтение, фильтр по пользователю, видна дата удаления (90 дней). Телефоны и профили показываются так же, как в
карточке.
Исполнитель: **webapp**. Deps: P6.1, P3.2. 4 ч.

#### P6.6 — Настройки в двух местах (D15)
`/settings` в чате для пользователя (способ выдачи людей, глубина, черновики) и та же форма в мини-аппе. Оба пути
вызывают `user_settings.set` (P3.5). **Тесты.** `tests/test_settings_two_ways.py`: значение из чата читается
мини-аппом и наоборот; валидация одна.
Исполнитель: **builder + webapp**. Deps: P6.1, P3.5. 4 ч.

---

### Поток `v3/analysis` (фаза P7)

#### P7.1 — Маршрутизатор этапов (D18)
**Цель.** Сменными становятся четыре этапа: разбор задачи, анализ, отбор, отчёт. Каркас общий.
**Файлы.** `bot/llm/stages.py`: `StageRouter.for_user(user_id) -> Stages(interviewer, analyzer, judge, recommender)`
на существующих Protocol (§0 п. 14); новый `Analyzer` Protocol для `OpenRouterAnalyzer`
(`bot/analysis_pipeline/openrouter.py:406`). Режим по подключённому: ключ → прямые вызовы, подписка → агент
(P7.3), ничего → O1. Сейчас всё на общем ключе, поэтому поведение не меняется.
**Настройки.** `STAGE_ROUTER_ENABLED` `false`.
**Тесты.** `tests/test_stage_router.py`: без флага — прежние объекты; у пользователя ключ → прямой режим; агент
недоступен не владельцу (D19).
Исполнитель: **builder + analyst**. Deps: P3.5. 6 ч.

#### P7.2 — Собственный ключ API пользователя (D17)
**Файлы.** Миграция **061** `user_credentials(user_id, provider anthropic|openai|openrouter, kind key|subscription,
ciphertext, last4, created_at, pk(user_id, provider, kind))`, шифрование через `secretbox` (P2.3). Ввод — мини-апп
(P6.6) и чат: сообщение с ключом удаляется сразу после чтения. Проверка ключа одним дешёвым вызовом. Расходы по ключу
пользователя пишутся в `campaign_costs` с `provider='user_key'`. Считать ли их в месячном лимите, который задаёт
владелец, — вопрос O1 (по умолчанию не считаются: платит пользователь).
**Тесты.** Ключ не хранится открытым (AC-13); `repr` и логи чистые; неверный ключ → понятная ошибка; `/forget`
стирает (P3.4).
Исполнитель: **builder**. Deps: P7.1, P2.3, P6.1, O1. 5 ч.

#### P7.3 — Агентный режим за флагом (D17–D19) ⛔ стопы 1 и 2 (O4, O9)
**Цель.** Подписка пользователя (сначала только владельца) запускает Claude Code или Codex в headless-режиме. Агент
получает готовый JSON-пакет собранных данных и возвращает JSON по существующим схемам. Акторы он не запускает.
**Файлы.** Новый сервис `agent-runner` (`docker/agent-runner.Dockerfile`): CLI агента закреплённой версии (⛔),
без `APIFY_TOKEN`, read-only FS, tmpfs для пакета, таймаут. Сеть: `backend` (Redis) и выход в интернет **только
через исходящий прокси со списком разрешённых хостов** (API модели); сеть `egress` — это весь интернет, а в пакете
недоверенный текст комментариев и рядом токен подписки (prompt injection → утечка). Вход и выход — очередь Redis под
отдельным ACL-пользователем, а не общим `REDIS_PASSWORD`. Токен подписки берётся из `user_credentials` (P7.2). Ответ валидируется pydantic-схемой этапа; ошибка → прямой режим (fallback, как
`FallbackInterviewer`, `anthropic_interviewer.py:129-160`). Стоимость записывается как `provider='subscription'`,
`cost_usd=0`, с длительностью в `units`.
**Настройки.** `AGENT_MODE_ENABLED` `false`; `AGENT_MODE_OWNER_ONLY` `true`; `AGENT_TIMEOUT_SECONDS` 300.
**Тесты.** На моках CLI: валидный ответ, ответ вне схемы → fallback, таймаут → fallback, в окружении агента нет
`APIFY_TOKEN` (тест compose), не-владелец → прямой режим.
Исполнитель: **builder + deployer**. Deps: P7.1, P7.2, O4, O9. 16 ч.

#### P7.4 — ⛔ Золотой набор в двух режимах (стоп 1)
G1–G9 прогоняются в прямом режиме и в агентном. Результаты — в `docs/v3/GOLDEN_V3.md`. Решение D19: открыть агентный
режим пользователям, только если он не хуже прямого по «точных/отправлено» и «разумных лидов». Потолок — $14 (два
прохода по $7), обеспечивается так же, как в P0.4: `CAMPAIGN_BUDGET_USD=0.8` на время прогона и лимит в Apify Console.
Исполнитель: **analyst** (+ owner запускает). Deps: P7.2, P7.3, P0.4, O7. 4 ч.

---

### Закрытие

#### Z1 — HANDOFF v3, ROLLBACK, инструкция пользователю, чек-лист включения
`docs/v3/HANDOFF.md`, `docs/v3/ROLLBACK.md` (его дополняет каждая задача), `docs/v3/USER_GUIDE.md`,
`docs/v3/ENABLE_CHECKLIST.md`: флаги по одному, норма, откат.
Исполнитель: **architect + deployer**. Deps: все задачи P*. 4 ч.

---

## 2. Граф зависимостей (без узлов O, кроме блокирующих)

```
R1 ─ (O5)                    P0.1 ─┬─ P0.2 ─┐
R3                                 └─ P0.3 ─┼─ P0.4 ─ P0.5        (O7 → P0.4)
                                            │
P1.0 ─ P1.1 ─┬─ P1.2 ─┐                     ├─ P2.1 ─ P3.1 ─┬─ P3.2 ─ P6.5
             ├─ P1.3 ─┼─ P1.8 ─ P4.4        │               ├─ P3.3
             ├─ P1.4 ─┘                     │               ├─ P3.5 ─┬─ P5.1 ─┬─ P5.2
             └─ P1.6 ─────────┐             │               │        │        ├─ P5.3
P1.0 + O2 ─ P1.5              │             │               │        │        ├─ P6.2
P1.2..P1.6 + O6 ─ P1.7        │             └─ P2.2 ─┬─ P2.4 ┬─ P2.6 (O8) ─ P6.3
                              │                      │       ├─ P2.7 (O9)
P4.1 ◄── P1.6, P3.1, O5 ──────┘                      │       ├─ P2.8
P4.1 ─ P4.2 ─┬─ P4.3 ─┬─ P4.5                        │       └─ P2.9 ─ P3.4
             ├─ P4.4  └─ P6.6                        └─ P2.5 (O3, P2.3)
             └─ P4.6                     O9 ─ P2.3 ─ P7.2
P5.1 ─ P7.1 ─┬─ P7.2 (O1, P6.1) ─┐
             └─ P7.3 (O4, O9) ───┴─ P7.4 (P0.4, O7)
P3.5 ─ P6.1 ─ P6.2..P6.6                               все ─ Z1
```

После ревью (§5) изменились рёбра: P2.6 ← P2.1; P2.8 ← P2.6; P6.1 ← P2.8; P6.6 ← P3.5 (вместо P4.3);
P7.1 ← P3.5 (вместо P5.1); P7.3 ← P7.2; P2.5 ← O10. Точный граф — `docs/v3/tasks.json` и `docs/v3/board.html` (R3).

## 3. План по потокам (≤ 3 исполнителя одновременно)

| Волна | Слот A | Слот B | Слот C | Ждёт владельца |
|---|---|---|---|---|
| 1 | R3, P1.0 | P2.10 (runbook) | P0.1 | R1 (дифф), O2–O9 |
| 2 | P1.1 | P2.2 | — | P0.2, P0.3 на VPS |
| 3 | P1.2, P1.3 | P1.4, P1.6 | P2.1 (⛔ стоп 4) | P0.4 |
| 4 | P1.8 | P2.4 | P2.3 (после O9) | |
| 5 | P1.5 (после O2) | P2.5 (O3), P2.6 (O8) | P3.1 (⛔) | P1.7 (O6) |
| 6 | P2.7, P2.8 | P3.2, P3.5 | P3.3, P2.9 | |
| 7 | P4.1 (O5) | P5.1 | P6.1 | |
| 8 | P4.2, P4.3 | P5.2, P5.3 | P6.2, P6.4 | |
| 9 | P4.4, P4.5, P4.6 | P7.1 | P6.3, P6.5, P6.6, P3.4 | |
| 10 | P7.2 | P7.3 (O4, O9) | — | P7.4 |
| 11 | Z1 | | | |

Самая длинная цепочка по часам: P0.1 → P0.3 → P2.2 → P2.4 → P2.6 → P6.3, параллельно с P2.1 → P3.1 → P3.5 → P5.1 →
P7.1 → P7.3 → P7.4. Узкое место — узлы владельца (O4, O5, O8, O9), а не часы.

## 4. Остановки ⛔

| Задача | Стоп | Что нужно |
|---|---|---|
| R1 | 5 | «ок» на дифф `CLAUDE.md`, `KICKOFF_FABLE.md`, `COMPLIANCE.md` (`docs/v3/RULE_CHANGES.md`) |
| P0.2, P0.3, P0.4 | 1, 3 | выполняет владелец на VPS; P0.2 ≤ $0.30, P0.4 ≤ $7 |
| P1.7 | 1 | O6: живые сэмплы ≤ $0.34 суммарно (таблица P1.7) |
| P2.3 | 2 | зависимость `cryptography` (O9) |
| P2.5 | — | O10: что делать с уже существующими флагами против детекта автоматизации |
| P2.6 | — | O8: подтвердить блок при слетевшем входе |
| P2.7 | 2 | пакет `x11-xserver-utils` в образ браузера (O9) |
| P2.9 | 4 | удаление профиля пользователем |
| P2.10 | 3 | LUKS-том на VPS делает владелец |
| P3.1 | 4 | заполнение `owner_user_id` и смена уникальности `investor_leads` |
| P3.3 | 4 | удаление данных старше 90 дней |
| P3.4 | 4, 5 | `/forget` (удаление и ПДн) |
| P4.1 | 5 | O5: хранение реакций и репостов, новые сети |
| P7.3 | 1, 2 | O4 письменно; CLI агента в образе (O9) |
| P7.4 | 1 | золотой набор ×2, ≤ $14 |
| каждый PR | 6 | мерж только при зелёном `check` и `integration` |

## 5. Ревью плана (Opus, 2026-11-11): что исправлено

Рецензент — отдельный Opus, план не писал. Все блокирующие и важные находки сверены по коду.

| # | Находка | Исправлено в |
|---|---|---|
| 1 | Смена уникальности `investor_leads` ломает `on conflict` (`leads.py:791`); null в ключе даёт дубли | P3.1: схема и `save_leads` в одном PR, `not null`, флаг уникальность не откатывает |
| 2 | `reach_contacts.url_key` уникален на всех (`027:36`, `reach.py:768`, `:779`), что нарушает D13 | P3.1: `(owner_user_id, url_key)`, проверка «уже известно» по владельцу |
| 3 | `known_ids_only` отдаёт 404 на `/live/link/*` и `/app/*` (`live_view.py:421-425`) | P2.8 (подприложение), P6.1 зависит от P2.8 |
| 4 | Окно входа пускает только staff, после P2.1 это только владелец | P2.6: доступ «свой профиль», зависит от P2.1 |
| 5 | `/v1/live/check` проверяет только открытое окно (`main.py:92-99`) | P2.6: новый эндпоинт проверки, 9 ч |
| 6 | `browser_profiles` не удалить (`007:6` restrict), каталог в volume `browser` | P2.9: мягкое удаление, эндпоинт в browser, 6 ч |
| 7 | 053 не меняла `default 'helper'`, отдельный список миграций | P2.1: данные не переписываются, роли сводятся кодом, ⛔4 снят |
| 8 | `/forget` и очистка зависели от ещё не созданных таблиц; проверка пропускала `telegram_user_id`, `requested_by`; каскад стирал расходы | P3.3, P3.4: общий реестр таблиц, расходы обезличиваются |
| 9 | P7.3: нет зависимости от P7.2; сеть `egress` = весь интернет, Redis в `backend` | P7.3: прокси со списком хостов, ACL Redis, зависит от P7.2, 16 ч |
| 10 | Одноразовую ссылку сжигает предпросмотр; убиралось подтверждение | P2.8: GET-страница, POST гасит, подтверждение по умолчанию остаётся |
| 11 | CSRF на initData (600 с), сессии в памяти | P6.1: CSRF-токен, сессии в БД (062) |
| 12 | Месячная сумма без расходов вне кампании | P5.3 убрана из отсечения; ключ пользователя — вопрос O1 |
| 13 | Потолки $7 и $14 ничем не обеспечены | P0.4, P7.4: `CAMPAIGN_BUDGET_USD=0.8` на прогон и лимит Console |
| 14 | LinkedIn: вторая страница недостижима при потолке $0.15 | P1.4: потолок = страницы × $0.10 + $0.05; сэмпл $0.15 |
| 15 | Уже есть флаги против детекта (`manager.py:79-80`) при COMPLIANCE §5 | новый узел O10, P2.5 зависит от O10 |
| 16 | `collected_posts` общие для пользователей | P3.3: удаляется, только когда все кампании старше срока |
| 17 | `KICKOFF_FABLE.md:50` тоже про LinkedIn | R1 |
| 18 | `linkedin.com` в `_APIFY_SKIPPED_HOSTS`, параметр `keep_query` | P1.8 |
| 19 | AC-13 на `bytea::text` не работает | AC-13 переписан |
| 20 | Лишние зависимости P7.1 → P5.1, P6.6 → P4.3 | P7.1 зависит от P3.5, P6.6 — от P6.1 и P3.5 |
| — | Неверные строки `browser_session/live.py`, `main.py`, `store.py` | §0 п. 3, 8 и P2.2, P2.4 |
| — | Недооценки: P2.1 6 → 12, P2.4 6 → 12, P3.1 4 → 8, P2.9 3 → 6, P1.1 5 → 9 | часы в задачах и `tasks.json` |
| — | Ветка порталов в P4.4 лишняя | P4.4 |
