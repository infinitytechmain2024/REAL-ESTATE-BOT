-- Оценка одной кампании: воронка, причины, хосты, источники, расходы, лиды, длительность, модели.
-- ТОЛЬКО ЧТЕНИЕ. Параметр: psql -v cid=<uuid кампании>  (см. scripts/eval_campaign.sh).
-- Колонки сверены с миграциями 014-048 (bot/services/db/migrations/).

-- 0. Кампания и длительность
select c.id, left(c.source_text, 80) as task, c.state, c.stop_reason, c.plan_version,
       c.created_at, c.finished_at,
       round(extract(epoch from (coalesce(c.finished_at, now()) - c.created_at)) / 60)::int as minutes_total,
       (select round(extract(epoch from (coalesce(r.finished_at, now()) - r.started_at)) / 60)::int
          from web_search_runs r where r.campaign_id = c.id) as minutes_web_stage,
       (select r.rounds from web_search_runs r where r.campaign_id = c.id) as web_rounds
  from campaigns c where c.id = :'cid'::uuid;

-- 1. Воронка: ссылок -> прочитано -> находок -> карточек отправлено / похожих / отклонено
select step, n from (
  select 1 as o, 'ссылок' as step, count(*) as n from web_campaign_urls where campaign_id = :'cid'::uuid
  union all select 2, 'прочитано (state=fetched)', count(*) from web_campaign_urls
    where campaign_id = :'cid'::uuid and state = 'fetched'
  union all select 3, 'находок (campaign_findings)', count(*) from campaign_findings where campaign_id = :'cid'::uuid
  union all select 4, 'карточек отправлено (exact, sent)', count(*) from campaign_findings
    where campaign_id = :'cid'::uuid and bucket = 'exact' and state = 'sent'
  union all select 5, 'exact не отправлено (held/sending)', count(*) from campaign_findings
    where campaign_id = :'cid'::uuid and bucket = 'exact' and state <> 'sent'
  union all select 6, 'похожих (similar)', count(*) from campaign_findings
    where campaign_id = :'cid'::uuid and bucket = 'similar'
  union all select 7, 'прочее (other)', count(*) from campaign_findings
    where campaign_id = :'cid'::uuid and bucket = 'other'
  union all select 8, 'отклонено (excluded)', count(*) from campaign_findings
    where campaign_id = :'cid'::uuid and bucket = 'excluded'
  union all select 9, 'дубликаты (duplicate_of)', count(*) from campaign_findings
    where campaign_id = :'cid'::uuid and duplicate_of is not null
) t order by o;

-- 1b. Состояния ссылок и слои чтения
select layer, state, coalesce(detail, '-') as detail, count(*) as n
  from web_campaign_urls where campaign_id = :'cid'::uuid group by 1, 2, 3 order by n desc limit 40;

-- 2. Корзина, состояние, причина (why / hold_reason)
select bucket, state, coalesce(why, '-') as why, coalesce(left(hold_reason, 60), '-') as hold_reason, count(*) as n
  from campaign_findings where campaign_id = :'cid'::uuid group by 1, 2, 3, 4 order by n desc limit 40;

-- 3. Хосты: известные порталы против остальных (список = SPAIN_PORTALS + банки + UA из bot/web_search/urls.py)
with known(d) as (values ('idealista.com'), ('fotocasa.es'), ('yaencontre.com'), ('pisos.com'), ('habitaclia.com'),
    ('milanuncios.com'), ('indomio.es'), ('tucasa.com'), ('kyero.com'), ('thinkspain.com'), ('terrenos.es'),
    ('solvia.es'), ('alisedainmobiliaria.com'), ('servihabitat.com'), ('sareb.es'), ('altamirainmuebles.com'),
    ('haya.es'), ('hogaria.net'), ('spainhouses.net'), ('green-acres.es'),
    ('dom.ria.com'), ('lun.ua'), ('olx.ua'), ('rieltor.ua')),
u as (
  select host, state, exists (select 1 from known k where u.host = k.d or u.host like '%.' || k.d) as is_portal
    from web_campaign_urls u where campaign_id = :'cid'::uuid)
select case when is_portal then 'известный портал' else 'остальные хосты' end as kind,
       count(distinct host) as hosts, count(*) as links, count(*) filter (where state = 'fetched') as read
  from u group by is_portal order by is_portal desc;

select host, count(*) as links, count(*) filter (where state = 'fetched') as read,
       min(left(url, 90)) as example_url
  from web_campaign_urls where campaign_id = :'cid'::uuid group by host order by links desc limit 25;

-- 4. Источники-объявления (Apify и др.): портал, состояние, код ошибки, объявлений
select name as portal, state, coalesce(error_code, '-') as error_code, jsonb_array_length(listings) as listings,
       import_offset as imported, created_at, updated_at
  from web_listing_source_runs where campaign_id = :'cid'::uuid order by name;

-- 5. Расходы: по этапу и виду записи, по item (модель/сайт) и по code
select stage, kind, round(sum(cost_usd)::numeric, 4) as usd, sum(units) as units, count(*) as rows
  from campaign_costs where campaign_id = :'cid'::uuid group by rollup (stage, kind) order by stage, kind;

select stage, kind, coalesce(item, '-') as item, coalesce(code, '-') as code,
       round(sum(cost_usd)::numeric, 4) as usd, sum(units) as units, count(*) as rows
  from campaign_costs where campaign_id = :'cid'::uuid group by 1, 2, 3, 4 order by usd desc, rows desc limit 50;

-- 6. Лиды: investor_leads, campaign_lead_deliveries, reach_contacts, чтение комментариев
select 'investor_leads' as what, role as detail, count(*) as n
  from investor_leads where campaign_id = :'cid'::uuid group by role
union all select 'campaign_lead_deliveries', state, count(*)
  from campaign_lead_deliveries where campaign_id = :'cid'::uuid group by state
union all select 'reach_contacts (relevant=' || relevant::text || ')', kind, count(*)
  from reach_contacts where campaign_id = :'cid'::uuid group by relevant, kind
union all select 'campaign_comment_reads', state, count(*)
  from campaign_comment_reads where campaign_id = :'cid'::uuid group by state
union all select 'reach_queries', coalesce(platform, '-'), count(*)
  from reach_queries where campaign_id = :'cid'::uuid group by platform
order by 1, 2;

-- 7. Модели: кто судил находки (reviewer/match) и какие модели платили за вызовы
select 'relevance' as source, coalesce(model, '-') as model, coalesce(verdict, '-') as verdict, count(*) as n
  from campaign_finding_relevance where campaign_id = :'cid'::uuid group by 2, 3
union all select 'costs.llm', coalesce(item, '-'), kind, count(*)
  from campaign_costs where campaign_id = :'cid'::uuid and stage = 'llm' group by 2, 3
order by 1, 4 desc;
