-- Roll back migration 046. Run manually; rollback files are never applied by apply_migrations.sh.
drop table if exists public.apify_raw_items;
drop table if exists public.apify_runs;
