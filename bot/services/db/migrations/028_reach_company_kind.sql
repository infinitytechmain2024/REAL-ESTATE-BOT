-- Investor reach (bot/campaign/reach.py) finds whatever the task asks for, anywhere in the world:
-- a company or service provider of the asked kind (villa management, property management,
-- relocation ...) is its own kind.

alter table public.reach_contacts drop constraint if exists reach_contacts_kind_check;
alter table public.reach_contacts add constraint reach_contacts_kind_check
    check (kind in ('investor', 'company', 'agent', 'agency', 'fund', 'network', 'developer', 'seeking', 'other'));
