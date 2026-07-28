-- Run in the Supabase SQL Editor once (after profiles + sessions exist).
-- Private per-student notes owned by a coach — never readable by players.

create table if not exists public.coach_notes (
  id uuid primary key default gen_random_uuid(),
  coach_id uuid not null references public.profiles (id) on delete cascade,
  player_id uuid not null references public.profiles (id) on delete cascade,
  body text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (coach_id, player_id)
);

create index if not exists coach_notes_coach_id_idx
  on public.coach_notes (coach_id);

create index if not exists coach_notes_player_id_idx
  on public.coach_notes (player_id);

alter table public.coach_notes enable row level security;

-- Owner coach only — players never see another coach's notebook.
drop policy if exists "coach_notes_select_own" on public.coach_notes;
create policy "coach_notes_select_own"
  on public.coach_notes
  for select
  to authenticated
  using (auth.uid() = coach_id);

drop policy if exists "coach_notes_insert_own" on public.coach_notes;
create policy "coach_notes_insert_own"
  on public.coach_notes
  for insert
  to authenticated
  with check (auth.uid() = coach_id);

drop policy if exists "coach_notes_update_own" on public.coach_notes;
create policy "coach_notes_update_own"
  on public.coach_notes
  for update
  to authenticated
  using (auth.uid() = coach_id)
  with check (auth.uid() = coach_id);

drop policy if exists "coach_notes_delete_own" on public.coach_notes;
create policy "coach_notes_delete_own"
  on public.coach_notes
  for delete
  to authenticated
  using (auth.uid() = coach_id);
