# Акторы Apify для D2–D5 (P1.0)

Сверено 2026-11-11 через `search-actors` и `fetch-actor-details` Apify MCP, без запусков. TikTok-видео по ссылке и
часть LinkedIn — из разведки 2026-10-17 (`docs/v2/E2_E3_SOURCES.md`). Цены — тариф FREE, в скобках — следующие
тарифы, если видны. «Польз.» — всего / в месяц.

**Все поля считаются непроверенными до живого сэмпла P1.7.** «Поля подтверждены схемой»:
«да» — поле есть в `outputSchema`; «частично» — `outputSchema` не снят или поле выводится косвенно;
«нет» — поля нет совсем.

Ни один выбранный актор не требует cookie или входа. Актор с cookie отклоняется (правило 10).

## Instagram (D2)

| Назначение | Актор | Цена | Польз. / рейтинг / обновлён | Вход | Нужные поля выхода | Поля подтверждены схемой | Cookie | Риски |
|---|---|---|---|---|---|---|---|---|
| Посты (хэштеги, поиск, ссылки) | `apify/instagram-scraper` (официальный) | Result $0.0027 (BRONZE 0.0023, SILVER 0.0019, GOLD 0.0015) | 477k / 66.7k, 4.67, — | `resultsType`, `directUrls`, `search`, `searchType`, `searchLimit`, `resultsLimit`, `onlyPostsNewerThan`, `addParentData` | `url`, `shortCode`, `caption`, `hashtags[]`, `timestamp`, `ownerUsername`, `ownerFullName`, `ownerId`, `locationName`, `likesCount`, `commentsCount`, `latestComments[]{text, ownerUsername, owner}`; цены и площади как полей нет, они только в `caption` | да (автор, текст, ссылка); цена и площадь — нет | нет | лайкеров нет, только `likesCount`; PII — имя и username автора |
| Комментаторы | `apify/instagram-comment-scraper` (официальный) | Comment $0.0026 (BRONZE 0.0023 … DIAMOND 0.0014) | 71k / 10k, 4.38, 2026-11-11 | `directUrls` (обязательно), `resultsLimit`, `includeNestedComments` | `id`, `text`, `timestamp`, `ownerUsername`, `owner{id, username, is_verified}`, `likesCount`, `commentUrl`, `postUrl`, `error`, `errorDescription` | да; имя — нет (`full_name` в схеме `null`); ссылка на профиль — частично (строится из `ownerUsername`) | нет | только username, без имени |
| Альтернатива: комментарии | `apidojo/instagram-comments-scraper-api` (не официальный) | $0.0075 за пост + $0.0005 за комментарий сверх 15 | 3.6k / —, 3.82, — | — | — | частично (схема не снята) | — | низкий рейтинг; при 15 комментариях на пост дороже официального |

## TikTok (D2)

| Назначение | Актор | Цена | Польз. / рейтинг / обновлён | Вход | Нужные поля выхода | Поля подтверждены схемой | Cookie | Риски |
|---|---|---|---|---|---|---|---|---|
| Посты (хэштеги, поиск, профили) | `clockworks/tiktok-scraper` (официальный, clockworks = Apify) | Result $0.0037, старт $0.001; доп. Comment $0.00125, Date filter $0.0013 | 387k / 35k, 4.75, — | `hashtags`, `searchQueries`, `searchSection`, `postURLs`, `profiles`, `resultsPerPage`, `oldestPostDateUnified` (всего 34 поля) | текст, автор, ссылка на видео, ссылка на профиль; цены и площади как полей нет | частично (`outputSchema` не снят) | в снятых полях входа нет; перед P1.3 проверить все 34 | дороже `tiktok-hashtag-scraper`; платный фильтр по дате — не включать без нужды |
| Посты по хэштегу (дешевле) | `clockworks/tiktok-hashtag-scraper` | Video $0.003 | — | `hashtags` (обязательно), `resultsPerPage` | — | частично (`outputSchema` не снят) | — | только хэштеги, без поиска по словам |
| Видео по ссылке | `clockworks/tiktok-video-scraper` (разведка 17.10) | $0.003 за видео; транскрипт +$0.048/мин (не включать) | 15k, 4.6 (на 17.10) | `postURLs[]` | `text`, `hashtags[]`, `authorMeta{name, nickName, profileUrl}`, `webVideoUrl`, `createTimeISO`, `shareCount` | частично (данные 17.10, схема заново не снималась) | нет | транскрипт платный |
| Комментаторы | `clockworks/tiktok-comments-scraper` (официальный) | comment $0.00125 (BRONZE 0.001, SILVER 0.00075, GOLD 0.0005) | 54k / 6.5k, 4.73, — | `postURLs`, `commentsPerPost`, `topLevelCommentsPerPost`, `maxRepliesPerComment`, `profiles` | `cid`, `text`, `createTimeISO`, `diggCount`, `uid`, `uniqueId`, `videoWebUrl`, `repliesToId`, `mentions[]`, `detailedMentions[]{nickName, profileUrl}` | частично: текст, `uniqueId`, ссылка на видео — да; профиль комментатора — косвенно (`https://www.tiktok.com/@{uniqueId}`); `authorMeta` — автор **видео**, не комментатора | нет | не взять `authorMeta` за комментатора; имени комментатора нет |
| Альтернатива: комментарии | `apidojo/tiktok-comments-scraper` (не официальный) | $0.0003 за комментарий | 4.6k / —, 4.95, — | `startUrls[]`, `maxItems`, `includeReplies` (17.10) | `user{username, displayName, url}` (17.10) | частично (схема не снята сейчас) | нет | в выдаче флаги `hasEmail`/`hasPhone` (17.10), их не сохранять |
| Альтернатива: посты | `apidojo/tiktok-scraper` (не официальный) | $0.0003 за пост | — | — | — | частично (схема не снята) | — | — |

## LinkedIn (D3, без cookie; только люди и компании)

Все акторы — `harvestapi`, не официальные, с пометкой «No Cookies». Официальных альтернатив без cookie нет.

| Назначение | Актор | Цена | Польз. / рейтинг / обновлён | Вход | Нужные поля выхода | Поля подтверждены схемой | Cookie | Риски |
|---|---|---|---|---|---|---|---|---|
| Люди | `harvestapi/linkedin-profile-search` | страница поиска (до 25 коротких профилей) $0.10 (FREE и BRONZE; SILVER 0.08, GOLD 0.05); Full profile $0.004; **Full profile + email search $0.01 — запрещено** | 55.7k / 8k, 4.4, — | `profileScraperMode`, `searchQuery`, `maxItems`, `locations`, `currentCompanies`, `currentJobTitles`, `industryIds` (всего 42 поля) | `linkedinUrl`, `publicIdentifier`, `firstName`, `lastName`, `headline`, `location{linkedinText, parsed{city, country}}`, `currentPosition[]{companyName, position, companyLinkedinUrl}`, `about`, `_meta.pagination` | да | нет | `emails[]` заполняется только в e-mail-режиме: режим запретить константой и тестом (P1.4) |
| Компании: поиск | `harvestapi/linkedin-company-search` | Short company $0.002, Full $0.004, старт $0.001 | 13.9k / 2k, 4.44, — | `scraperMode`, `searchQuery`, `locations`, `industryIds`, `companySize`, `maxItems`, `startPage`, `takePages` | имя, ссылка на компанию, отрасль, место | частично (`outputSchema` не снят) | нет | — |
| Компании: карточка по ссылке | `harvestapi/linkedin-company` | Company $0.004, старт $0.00005 | 33k / 7.1k, 4.86, — | `companies[]`, `searches[]` | адрес, телефоны, сайт, штат (по описанию) | частично (`outputSchema` не снят) | нет | телефоны и адрес компании брать как дал актор |
| Сотрудники компании | `harvestapi/linkedin-company-employees` | Basic $0.003, Full $0.008, Full + email $0.012, **старт $0.02** | — | — | — | частично (схема не снята) | нет | **запрещено по умолчанию**: есть режим поиска e-mail, дорогой старт |
| Посты + реакции + комментарии (D5) | `harvestapi/linkedin-post-search` | Post $0.002, Main profile $0.002, Full profile $0.004, Reaction $0.002, Comment $0.002, запрос без результата $0.001, старт $0.00005 | 42.5k / 11.6k, 4.95, — | `searchQueries`, `authorUrls`, `authorsCompanies`, `maxPosts`, `postedLimit`, `sortBy`, `scrapeReactions`, `maxReactions`, `reactionsProfileScraperMode`, `scrapeComments` (всего 24 поля) | `linkedinUrl`, `content`, `postedAt`, `author{name, linkedinUrl, type, info}`, `engagement{likes, comments, shares, reactions[]}`, `reactions[]{reactionType, actor{name, linkedinUrl, position}}`, `comments[]{commentary, linkedinUrl, actor{name, linkedinUrl, position}}` | да; репостнувшие — нет (только `engagement.shares`) | нет | реакции и комментарии оплачиваются поштучно: обязателен `maxReactions` |
| Посты профиля / компании | `harvestapi/linkedin-profile-posts`, `harvestapi/linkedin-company-posts` | как у `linkedin-post-search` | — | `scrapeReactions`, `scrapeComments`, `includeReposts` | как у `linkedin-post-search` | частично (схема не снята, по описанию те же поля) | нет | — |

## Facebook (D5)

| Назначение | Актор | Цена | Польз. / рейтинг / обновлён | Вход | Нужные поля выхода | Поля подтверждены схемой | Cookie | Риски |
|---|---|---|---|---|---|---|---|---|
| Реакции на пост | `apify/facebook-likes-scraper` (официальный) | Result $0.0062 (BRONZE 0.005, SILVER 0.0038, GOLD 0.0026) | 10.2k / 201, 4.57, 2026-11-10 | `startUrls` (обязательно), `resultsLimit` | `name`, `profileUrl`, `facebookId`, `facebookUrl`, `reaction` (like, love, care, sad, angry, haha/laugh), `url`, `inputUrl`, `error`, `errorDescription` | да | нет | только открытые посты; закрытые группы — через вступление человеком (правило 10); мало пользователей в месяц |
| Альтернатива: реакции | `scraper_one/facebook-reactions-scraper` (не официальный) | $0.004 за реакцию | 1k / —, 3.83, — | — | — | частично (схема не снята) | — | низкий рейтинг |
| Репосты | — | — | — | — | — | нет | — | публичного актора нет; `apify/facebook-posts-scraper` отдаёт только счётчик `shares` |

## Краулер сайтов (D4)

| Назначение | Актор | Цена | Польз. / рейтинг / обновлён | Вход | Нужные поля выхода | Поля подтверждены схемой | Cookie | Риски |
|---|---|---|---|---|---|---|---|---|
| Сайты по заданным адресам | `apify/website-content-crawler` (официальный) | **модель FREE**: событий нет, платится платформа (compute units, прокси) | 176k / 12k, —, 2026-11-11 | `startUrls` и ограничители `maxCrawlPages`, `maxCrawlDepth`; память и таймаут — в параметрах запуска | `url`, `text`/`markdown`, `metadata` страницы; цены, площади, телефона как полей нет, их извлекает `_normalize` из текста | частично (`inputSchema` и `outputSchema` не сняты) | нет | `maxTotalChargeUsd` расход не ограничивает (см. ниже) |

## Рекомендация

По умолчанию берём официальные акторы (apify, clockworks): у них больше пользователей и стабильнее схема. Дешёвые
(apidojo, scraper_one) — только альтернатива, после отдельного живого сэмпла.

| Назначение | Берём | Цена (FREE) | Ключевые поля | Альтернатива |
|---|---|---|---|---|
| IG посты | `apify/instagram-scraper` | $0.0027 / результат | автор, текст, ссылка — да; цена и площадь — только в тексте | — |
| IG комментаторы | `apify/instagram-comment-scraper` | $0.0026 / комментарий | текст, username — да; имя — нет; профиль — из username | `apidojo/instagram-comments-scraper-api` |
| TT посты | `clockworks/tiktok-scraper` (поиск), `clockworks/tiktok-video-scraper` (по ссылке) | $0.0037 / результат + $0.001 старт; $0.003 / видео | частично (схема не снята) | `clockworks/tiktok-hashtag-scraper` ($0.003), `apidojo/tiktok-scraper` ($0.0003) |
| TT комментаторы | `clockworks/tiktok-comments-scraper` | $0.00125 / комментарий | текст, `uniqueId` — да; профиль — из `uniqueId`; имени нет | `apidojo/tiktok-comments-scraper` ($0.0003, есть `user.url`) |
| LI люди | `harvestapi/linkedin-profile-search`, короткий режим | $0.10 / страница до 25 | имя, заголовок, место, должность, ссылка — да | — |
| LI компании | `harvestapi/linkedin-company-search` (поиск) + `harvestapi/linkedin-company` (карточка) | $0.002 / короткая + $0.001 старт; $0.004 / компания | частично (схема не снята) | — |
| LI реакции и комментарии | `harvestapi/linkedin-post-search` с `scrapeReactions`, `scrapeComments` | $0.002 / пост, реакция, комментарий | тип реакции, имя, профиль — да; репостнувшие — нет | `linkedin-profile-posts`, `linkedin-company-posts` |
| FB реакции | `apify/facebook-likes-scraper` | $0.0062 / реакция | имя, профиль, тип реакции — да | `scraper_one/facebook-reactions-scraper` ($0.004) |
| Краулер | `apify/website-content-crawler` | compute (FREE) | url, текст — частично; объявление выделяем сами | — |

## Чего нет

- Людей, поставивших лайк в Instagram или TikTok: публичного актора нет, только счётчики `likesCount` и `diggCount`.
- Людей, сделавших репост, в FB, IG, TT и LinkedIn: списком нет, только счётчики (`shares`, `engagement.shares`).
- Имени комментатора в Instagram (только username) и TikTok (только `uniqueId`).
- Структурных цены и площади в соцсетях и в краулере: только текст. По правилу 9 их извлекает анализ, маппер не угадывает.
- `outputSchema` для `clockworks/tiktok-scraper`, `harvestapi/linkedin-company*`, `website-content-crawler` не снят.

## Последствия для задач

1. **P1.5 и правило 11.** `website-content-crawler` работает по модели FREE, поэтому `maxTotalChargeUsd`, который
   ограничивает только PPE-события, его расход не ограничивает. Значит:
   - `maxTotalChargeUsd` передаётся по-прежнему (правило 11), но потолком не считается;
   - потолок держат `maxCrawlPages` (`APIFY_CRAWLER_MAX_PAGES`), `maxCrawlDepth`, память и таймаут запуска;
     `APIFY_CRAWLER_MAX_CHARGE_USD` нужно пересмотреть: это бюджет для проверки «хватит ли денег» до запуска,
     а не ограничитель;
   - стоимость в `campaign_costs` пишется по факту, из `usageTotalUsd` запуска, а не по событиям;
   - потолок краулера в P1.7 тоже держится через страницы и таймаут.
   Нужна поправка формулировки правила 11 `CLAUDE.md` (для акторов с моделью FREE). Это предложение, сам не правлю.
2. **P1.6 (D5).** Реакции — только FB (`apify/facebook-likes-scraper`) и LinkedIn (`harvestapi/linkedin-post-search`).
   Мапперы реакций для IG и TT не делаются. `Engagement(kind="repost")` ни один актор не наполняет: репост — только
   счётчик у поста.
3. **P1.4.** Запрещены e-mail-режимы: «Full profile + email search» в `linkedin-profile-search` и
   `harvestapi/linkedin-company-employees` целиком (старт $0.02). Компании — через `linkedin-company-search`
   и `linkedin-company`, их `outputSchema` снять до кода.
4. **P1.2, P1.3.** Профиль комментатора строится из username (`instagram.com/<ownerUsername>`,
   `tiktok.com/@<uniqueId>`). В TikTok `authorMeta` в строке комментария — автор видео. Это проверить на сэмпле P1.7.
