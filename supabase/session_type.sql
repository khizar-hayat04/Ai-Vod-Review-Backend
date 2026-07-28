-- Solo recording support (run in Supabase SQL Editor).
-- Safe to re-run.

alter table public.sessions add column if not exists session_type text;
update public.sessions set session_type = 'live' where session_type is null;
alter table public.sessions alter column session_type set default 'live';
alter table public.sessions alter column session_type set not null;
alter table public.sessions drop constraint if exists sessions_session_type_check;
alter table public.sessions add constraint sessions_session_type_check
  check (session_type in ('live', 'solo_recording'));

alter table public.sessions alter column player_id drop not null;

-- Mark when the player has seen the “review ready” banner.
alter table public.sessions add column if not exists notified_at timestamptz;
