-- Run in the Supabase SQL Editor once (after sessions table exists).
-- Coach voice+draw explanations anchored to a gameplay video timestamp.
-- coach_id is NOT stored here — ownership is via sessions.coach_id.

create table if not exists public.explanations (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.sessions (id) on delete cascade,
  -- Gameplay video currentTime (seconds) when Record was pressed.
  video_timestamp double precision not null,
  -- Length of the recorded mic clip (seconds).
  duration_seconds double precision null,
  -- Optional short label shown on markers / review list.
  title text null,
  -- Uploaded audio URL (Supabase Storage explanation-audio bucket public URL).
  audio_url text null,
  -- Buffered draw events: [{ relativeTimeMs, type, tool?, color?, points? }, ...]
  draw_events jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

-- Incremental migrations for existing projects.
alter table public.explanations add column if not exists duration_seconds double precision;
alter table public.explanations add column if not exists title text;

-- Drop coach_id if an older schema added it (safe if column never existed).
alter table public.explanations drop column if exists coach_id;

create index if not exists explanations_session_id_idx
  on public.explanations (session_id);
create index if not exists explanations_session_video_ts_idx
  on public.explanations (session_id, video_timestamp);

alter table public.explanations enable row level security;

-- Coach who owns the session, or the player on that session, can read.
drop policy if exists "explanations_select_participants" on public.explanations;
create policy "explanations_select_participants"
  on public.explanations
  for select
  to authenticated
  using (
    exists (
      select 1 from public.sessions s
      where s.id = explanations.session_id
        and (s.coach_id = auth.uid() or s.player_id = auth.uid())
    )
  );

-- Only the coach of the session can insert (via sessions join — no explanations.coach_id).
drop policy if exists "explanations_insert_coach" on public.explanations;
create policy "explanations_insert_coach"
  on public.explanations
  for insert
  to authenticated
  with check (
    exists (
      select 1 from public.sessions s
      where s.id = session_id and s.coach_id = auth.uid()
    )
  );

drop policy if exists "explanations_update_coach" on public.explanations;
create policy "explanations_update_coach"
  on public.explanations
  for update
  to authenticated
  using (
    exists (
      select 1 from public.sessions s
      where s.id = explanations.session_id and s.coach_id = auth.uid()
    )
  )
  with check (
    exists (
      select 1 from public.sessions s
      where s.id = explanations.session_id and s.coach_id = auth.uid()
    )
  );

drop policy if exists "explanations_delete_coach" on public.explanations;
create policy "explanations_delete_coach"
  on public.explanations
  for delete
  to authenticated
  using (
    exists (
      select 1 from public.sessions s
      where s.id = explanations.session_id and s.coach_id = auth.uid()
    )
  );
