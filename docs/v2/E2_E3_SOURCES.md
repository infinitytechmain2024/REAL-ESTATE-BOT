# E2 TikTok и E3 LinkedIn: выбор акторов (разведка 2026-10-17, без живых запусков)

Все цены — тариф FREE/BRONZE Apify (с автооплатой на Starter цены те же или ниже). Ни один из выбранных акторов не требует
cookie или аккаунта клиента (правило 10 CLAUDE.md: LinkedIn без автоматизации аккаунта; для TikTok то же по аналогии).

## E2. TikTok: ссылка на видео → объект + комментарии → лиды

| Роль | Актор | Цена | Вход | Что в выдаче |
|---|---|---|---|---|
| Карточка объекта (видео) | `clockworks/tiktok-video-scraper` (Apify, 15k пользователей, 4.6) | $0.003 за видео; транскрипт +$0.048/мин (не включать) | `postURLs[]` | `text` (описание), `hashtags[]`, `authorMeta{name, nickName, profileUrl, signature, fans, verified}`, `playCount/diggCount/commentCount/shareCount`, `createTimeISO`, `locationCreated`, `textLanguage`, `videoMeta.coverUrl`, `webVideoUrl` |
| Комментарии | `clockworks/tiktok-comments-scraper` (Apify, 48k пользователей, 4.73) | $0.00125 за комментарий ($0.125 за 100) | `postURLs[]`, `commentsPerPost` (prefill 100), `maxRepliesPerComment` (0) | `text`, `uniqueId` (username), `uid`, `cid`, `createTimeISO`, `diggCount`, `repliesToId`, `videoWebUrl`, `avatarThumbnail`; имени-профиля-URL нет, строится как `https://www.tiktok.com/@{uniqueId}` |
| **Предпочтительный** вариант комментариев | `apidojo/tiktok-comments-scraper` (3.8k пользователей, 4.95) | $0.0003 за комментарий ($0.03 за 100), в 4 раза дешевле | `startUrls[]`, `maxItems`, `includeReplies` | `text`, `createdAt`, `likeCount`, `replyCount`, `parentId`, `commentLanguage`, `user{username, displayName, url, bio, region, language, hasEmail/hasPhone (bool)}` — профиль уже с URL и именем, что удобнее clockworks |

Схема в боте (копия B1 `post_leads.py`):
1. Intake: ссылка `tiktok.com/@…/video/<id>` или `vm.tiktok.com/…` → `post_link=True`, платформа `tiktok` (новое поле или префикс в `sources.required`).
2. `campaign_post_reads` (048) переиспользуется: `provider='apify_tiktok'`, тот же цикл claim → `pending:` → launch → save_run → finish (H1 защита).
3. Один запуск video-scraper ($0.003) даёт «объект»: описание, автор, хэштеги, статистика → карточка «🎬 Объект из TikTok» с полями из `text` (цена/площадь/место извлекаются Haiku как для страниц, правило 9: нет числа → None).
4. Один запуск comments-scraper (до `commentsPerPost`, потолок `maxTotalChargeUsd`) → `AcquiredPost`-подобные строки → тот же классификатор лидов (`OPENROUTER_LEADS_MODEL`) → `investor_leads` с `platform='tiktok'`, `profile_url=https://www.tiktok.com/@{uniqueId}`.
5. Отчёт: раздел «💼 Инвесторы» уже умеет источники; добавить подпись платформы.

Флаги (все off): `TIKTOK_COMMENTS_ENABLED=false`, `APIFY_TIKTOK_VIDEO_ACTOR=clockworks/tiktok-video-scraper`,
`APIFY_TIKTOK_COMMENTS_ACTOR=apidojo/tiktok-comments-scraper` (запасной `clockworks/tiktok-comments-scraper`), `APIFY_TIKTOK_COMMENTS_ACTOR_BUILD=` (pinned),
`APIFY_TIKTOK_COMMENTS_MAX_PER_POST=200`, `APIFY_TIKTOK_MAX_USD_PER_POST=0.5`. Цены в `costs.PRICES`: оба актора.
Оценка: 9 ч (sources 4, builder 3, db 0.5 — индекс/CHECK provider в 048, тесты/доки 1.5). Живой сэмпл: 1 видео ≈ $0.04 (apidojo) или $0.13 (clockworks).

Открытые вопросы к клиенту: откуда ссылки (присылает сам / бот ищет по теме через `powerai/tiktok-videos-search-scraper`
$0.005 за видео + $0.09 старт); критерий «заинтересованный» (любой вопрос о цене/контакте или только явное намерение
инвестировать); ответы под видео — нет (как для FB).

## E3. LinkedIn: поиск людей-инвесторов (без аккаунта клиента)

Решение владельца 17.10: **только люди**, не компании.

| Роль | Актор | Цена | Вход | Что в выдаче |
|---|---|---|---|---|
| Посты по теме (основной путь) | `harvestapi/linkedin-post-search` (35k пользователей, 4.95, «No Cookies») | $0.002 за пост; +$0.002 «main profile» автора; старт $0.00005; 0-результат $0.001 | `searchQueries[]` (как в строке поиска LinkedIn), `maxPosts`, `postedLimit` (`month`/`3months`), `sortBy`, `authorKeywords` («investor», «inversor»), `profileScraperMode=short\|main` | `content`, `author{name, linkedinUrl, info (заголовок), type}`, `postedAt.date`, `engagement`, `linkedinUrl` поста |
| Профили по фильтрам (дополнительный путь) | `harvestapi/linkedin-profile-search` (49k, 4.4) | $0.10 за страницу поиска (25 коротких профилей) + $0.004 за полный профиль | `searchQuery`, `locations[]` («Spain», «Madrid»), `currentJobTitles[]`, `functionIds` (10 Finance, 23 Real Estate), `seniorityLevelIds`, `profileLanguages`, `maxItems` | `firstName/lastName`, `headline`, `location.parsed{city,country}`, `currentPosition[]`, `about`, `linkedinUrl`, `followerCount`; e-mail — отдельный платный режим, **не включать** |

Схема в боте: расширение `reach.py` (B3/B4): новый источник `linkedin_posts`: запросы из плана кампании («invertir inmobiliario Málaga», «buscamos inversores proyecto residencial», «real estate investor Spain») → посты за `3months` → Haiku-классификатор (тот же, что для комментариев: «ищет проекты / предлагает деньги / шум») → `reach_contacts` или `investor_leads` с `platform='linkedin'`, `profile_url`, `headline`, цитата поста. Профильный поиск — вторым шагом, только по согласованным фильтрам (дорого: $0.10 за 25 профилей, без гарантии намерения).

Флаги (off): `LINKEDIN_LEADS_ENABLED=false`, `APIFY_LINKEDIN_POSTS_ACTOR=harvestapi/linkedin-post-search`,
`APIFY_LINKEDIN_POSTS_ACTOR_BUILD=`, `APIFY_LINKEDIN_MAX_POSTS=50`, `APIFY_LINKEDIN_MAX_USD_PER_CAMPAIGN=1`,
`APIFY_LINKEDIN_PROFILE_SEARCH_ENABLED=false`. Цены в `costs.PRICES`.
Оценка: 9 ч. Живой сэмпл: 1 запрос × 20 постов ≈ $0.08.

Открытые вопросы: язык/регион запросов (испанский + английский, Испания); нужен ли профильный поиск вообще или
достаточно постов; O1 распространяется (ПДн: имя, должность, ссылка на профиль).

## Что общего и в каком порядке
1. Оба используют `ApifyHttp.launch` с pinned build, `maxTotalChargeUsd`, `record_unique`, защиту H1 (`pending:`), `check_pinned_build`.
2. Сначала E2 (ближе к B1, меньше неизвестных), затем E3.
3. Каждый — ветка `v2/<поток>`, флаг off, тесты на моках по фикстурам живого сэмпла (1 запуск каждого актора, ≤ $0.15 суммарно, с «ок» владельца).
