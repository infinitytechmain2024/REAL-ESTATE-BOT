-- X (Twitter) joins the platforms a browser profile can be logged in to (/login x, «🔐 Вход в соцсети»).
-- Its session (cookies auth_token and ct0) stays in the profile; the reach searcher reads X with it.

alter table public.browser_profiles drop constraint if exists browser_profiles_platform_check;
alter table public.browser_profiles add constraint browser_profiles_platform_check
    check (platform in ('facebook', 'tiktok', 'instagram', 'telegram', 'website', 'linkedin', 'x'));
