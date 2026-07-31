-- Run this in the Supabase SQL Editor once.
-- Sessions lifecycle:
--   uploading → awaiting_coach_accept → live | declined
--   live → reviewing → completed

create table if not exists public.sessions (
  id uuid primary key default gen_random_uuid(),
  coach_id uuid not null references public.profiles (id) on delete cascade,
  player_id uuid null references public.profiles (id) on delete set null,
  video_url text null,
  status text not null default 'uploading'
    check (status in (
      'uploading',
      'awaiting_coach_accept',
      'live',
      'declined',
      'reviewing',
      'completed'
    )),
  created_at timestamptz not null default now(),
  uploaded_at timestamptz null,
  cancel_reason text null
);

-- Incremental migrations (safe to re-run on existing projects).
alter table public.sessions add column if not exists uploaded_at timestamptz;
alter table public.sessions add column if not exists cancel_reason text;

-- Live vs solo async recording (coach reviews alone).
alter table public.sessions add column if not exists session_type text;
update public.sessions set session_type = 'live' where session_type is null;
alter table public.sessions alter column session_type set default 'live';
alter table public.sessions alter column session_type set not null;
alter table public.sessions drop constraint if exists sessions_session_type_check;
alter table public.sessions add constraint sessions_session_type_check
  check (session_type in ('live', 'solo_recording', 'stream_review'));

-- Solo Path B has no player until a share link is opened later.
alter table public.sessions alter column player_id drop not null;

-- Video length, captured once when the VOD first loads in Review Mode.
alter table public.sessions add column if not exists duration_seconds numeric;

-- Soft-hide from workspace lists (player/coach independently).
alter table public.sessions
  add column if not exists hidden_from_player boolean not null default false;
alter table public.sessions
  add column if not exists hidden_from_coach boolean not null default false;

-- Allow 'reviewing' on existing databases (constraint name may vary).
alter table public.sessions drop constraint if exists sessions_status_check;
alter table public.sessions add constraint sessions_status_check
  check (status in (
    'uploading',
    'awaiting_coach_accept',
    'live',
    'declined',
    'reviewing',
    'completed'
  ));

create index if not exists sessions_coach_id_idx on public.sessions (coach_id);
create index if not exists sessions_player_id_idx on public.sessions (player_id);
create index if not exists sessions_status_idx on public.sessions (status);

alter table public.sessions enable row level security;

-- SELECT: coach, claimed player, or any authenticated user viewing an open
-- (unclaimed) uploading session via a shared link.
drop policy if exists "sessions_select_participants" on public.sessions;
create policy "sessions_select_participants"
  on public.sessions
  for select
  to authenticated
  using (
    auth.uid() = coach_id
    or auth.uid() = player_id
    or (player_id is null and status = 'uploading')
  );

-- Additive SELECT for unclaimed stream_review links (status=live, no player yet).
drop policy if exists "sessions_select_unclaimed_stream_review" on public.sessions;
create policy "sessions_select_unclaimed_stream_review"
  on public.sessions
  for select
  to authenticated
  using (
    session_type = 'stream_review'
    and status = 'live'
    and player_id is null
  );

-- INSERT: coaches create sessions for themselves.
drop policy if exists "sessions_insert_coach" on public.sessions;
create policy "sessions_insert_coach"
  on public.sessions
  for insert
  to authenticated
  with check (auth.uid() = coach_id);

-- UPDATE: coach, claimed player, or a player claiming an open uploading session.
drop policy if exists "sessions_update_participants" on public.sessions;
create policy "sessions_update_participants"
  on public.sessions
  for update
  to authenticated
  using (
    auth.uid() = coach_id
    or auth.uid() = player_id
    or (player_id is null and status = 'uploading')
  )
  with check (
    auth.uid() = coach_id
    or auth.uid() = player_id
  );

-- Additive UPDATE: first opener claims player_id on an unclaimed stream_review.
drop policy if exists "sessions_update_claim_stream_review" on public.sessions;
create policy "sessions_update_claim_stream_review"
  on public.sessions
  for update
  to authenticated
  using (
    session_type = 'stream_review'
    and status = 'live'
    and player_id is null
  )
  with check (
    session_type = 'stream_review'
    and status = 'live'
    and auth.uid() = player_id
  );

-- DELETE: coach or claimed player can remove a past session from their workspace.
drop policy if exists "sessions_delete_participants" on public.sessions;
create policy "sessions_delete_participants"
  on public.sessions
  for delete
  to authenticated
  using (
    auth.uid() = coach_id
    or auth.uid() = player_id
  );

-- Coaches need player display names on the incoming-request card.
-- Safe if a broader profiles select policy already exists.
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'profiles'
      and policyname = 'profiles_select_authenticated_basic'
  ) then
    create policy "profiles_select_authenticated_basic"
      on public.profiles
      for select
      to authenticated
      using (true);
  end if;
exception
  when undefined_table then null;
  when duplicate_object then null;
end $$;

-- Realtime: player watch view + coach accept/decline listeners.
-- Safe to re-run: ignores if the table is already in the publication.
do $$
begin
  alter publication supabase_realtime add table public.sessions;
exception
  when duplicate_object then null;
  when others then
    -- Publication may already include the table under a different error.
    null;
end $$;
