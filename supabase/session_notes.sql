-- Run in the Supabase SQL Editor once (after sessions exists).
-- Persisted notes/flags from live review (same pattern as explanations).

create table if not exists public.session_notes (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.sessions(id) on delete cascade,
  video_timestamp numeric not null,
  category text not null check (category in ('mistake', 'good_play', 'question')),
  text text,
  created_at timestamptz not null default now()
);

create index if not exists session_notes_session_id_idx
  on public.session_notes (session_id);

create index if not exists session_notes_session_category_idx
  on public.session_notes (session_id, category);

alter table public.session_notes enable row level security;

drop policy if exists "session_notes_select_involved" on public.session_notes;
create policy "session_notes_select_involved"
  on public.session_notes for select
  to authenticated
  using (
    exists (
      select 1 from public.sessions s
      where s.id = session_notes.session_id
      and (s.coach_id = auth.uid() or s.player_id = auth.uid())
    )
  );

drop policy if exists "session_notes_insert_coach" on public.session_notes;
create policy "session_notes_insert_coach"
  on public.session_notes for insert
  to authenticated
  with check (
    exists (
      select 1 from public.sessions s
      where s.id = session_id
      and s.coach_id = auth.uid()
    )
  );

drop policy if exists "session_notes_delete_coach" on public.session_notes;
create policy "session_notes_delete_coach"
  on public.session_notes for delete
  to authenticated
  using (
    exists (
      select 1 from public.sessions s
      where s.id = session_notes.session_id
      and s.coach_id = auth.uid()
    )
  );

notify pgrst, 'reload schema';
