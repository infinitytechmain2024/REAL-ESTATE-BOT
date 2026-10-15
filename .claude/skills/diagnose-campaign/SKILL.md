---
name: diagnose-campaign
description: Диагностика плохих результатов кампании — воронка (ссылки → страницы → находки → карточки), причины отказов, источники, Apify-запуски, расходы, модели. Используй при жалобах «бот выдаёт не то», «0 карточек», «дорого».
---

# diagnose-campaign

Дай владельцу команды (только чтение), он запускает их на VPS в `/opt/real-estate-bot`:

```bash
docker compose --env-file .env exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
select id, left(source_text,80) task, state, created_at from campaigns order by created_at desc limit 3;
with c as (select id from campaigns order by created_at desc limit 1)
select 'ссылок' s, count(*) from web_campaign_urls, c where campaign_id=c.id
union all select 'прочитано', count(*) from web_campaign_urls, c where campaign_id=c.id and state='fetched'
union all select 'находок', count(*) from campaign_findings, c where campaign_id=c.id;
with c as (select id from campaigns order by created_at desc limit 1)
select cf.why, coalesce(left(cf.hold_reason,50),'-'), count(*) from campaign_findings cf, c
 where cf.campaign_id=c.id group by 1,2 order by 3 desc limit 15;
with c as (select id from campaigns order by created_at desc limit 1)
select host, count(*), count(*) filter (where state='fetched') from web_campaign_urls, c
 where campaign_id=c.id group by host order by 2 desc limit 15;
with c as (select id from campaigns order by created_at desc limit 1)
select name, state, error_code, jsonb_array_length(listings) from web_listing_source_runs, c where campaign_id=c.id;
with c as (select id from campaigns order by created_at desc limit 1)
select stage, item, round(sum(cost_usd)::numeric,3) from campaign_costs, c where campaign_id=c.id group by 1,2 order by 3 desc;
SQL
grep -E "^OPENROUTER_.*MODEL=|^WEB_SEARCH_DOMAIN_POLICY=|^APIFY_.*ENABLED=|^ACQUISITION_MODE=" .env
```

Разбор:
- много ссылок с чужих хостов → поисковик (A1, allowlist);
- `why=area` / «площадь не указана» → маппер площади, `plot_area`, `tolerance.min_area_of`;
- Apify-портал `state=failed` или `n=0` → `error_code`, живая выдача актора (скилл `add-apify-source`, шаг 6);
- расходы по `llm` выше `api` → модели и префильтр (`docs/v2/MODELS.md`);
- сравни с `docs/v2/GOLDEN_SET.md`. Вывод: одна главная причина + задача из `TASKS.md`, которая её чинит.
