-- Solo review notification tracking (run in Supabase SQL Editor).
alter table public.sessions add column if not exists notified_at timestamptz;
