-- The verification page now authenticates with Telegram's signed Mini App
-- identity instead of Tailscale. Rename the session's identity column;
-- written to be a no-op on a database that already has the new name.
do $$
begin
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'verification_page_sessions'
                  and column_name = 'tailscale_login') then
        alter table public.verification_page_sessions rename column tailscale_login to identity;
    end if;
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'verification_access_tokens'
                  and column_name = 'used_by_login') then
        alter table public.verification_access_tokens rename column used_by_login to used_by_identity;
    end if;
end;
$$;

comment on table public.verification_page_sessions is
    'Verification page sessions created by consuming an access token with a Telegram-signed identity; hashes only.';
