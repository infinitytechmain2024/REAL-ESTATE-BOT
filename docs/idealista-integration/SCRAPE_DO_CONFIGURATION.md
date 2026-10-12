# Scrape.do через существующий scrape layer

Режим выключен, пока `WEB_SEARCH_SCRAPE_API_URL` пуст. Для Scrape.do задать `https://api.scrape.do/`,
`WEB_SEARCH_SCRAPE_API_AUTH_MODE=query_token` и ключ `WEB_SEARCH_SCRAPE_API_KEY` только в `.env`.
Клиент отправляет `token` в query к Scrape.do, `url` целевой страницы, `geoCode=es`, `render` и `super`.
Ключ не попадает в repr, коды ошибок и журнал расходов. Режим `bearer` остаётся умолчанием для прежних
unlocker-провайдеров.

`WEB_SEARCH_SCRAPE_DO_RENDER=false` и `WEB_SEARCH_SCRAPE_DO_SUPER=false` задают обычный запрос.
Scrape.do автоматически применяет Super Proxy к Idealista: бюджетная оценка до вызова — 10 credits,
при `render=true` — 25 credits. Другие комбинации оцениваются как 1/5/10/25 credits по флагам;
ответный `Scrape.do-Request-Cost` заменяет оценку фактическими credits даже для HTTP 400 и HTML-ошибок.
Если заголовок отсутствует или случился сетевой сбой, в журнале остаётся явно помеченная оценка.
`WEB_SEARCH_SCRAPE_DO_CREDIT_USD=0.000116` — доля Hobby $29/250000 credits, не отдельный счёт
провайдера; для другого тарифа укажите свою долю USD/credit. Сумма `scrape` в отчёте отделена от `api` Apify.

Вызов проходит только после отказа HTTP/браузера для объявления, проверки robots/cap/budget и circuit breaker.
Поддержка Fotocasa и итоговая цена требуют живого теста; этот этап внешние API не вызывал.
