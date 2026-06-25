create table if not exists public.visa_slot_state (
  key text primary key check (char_length(key) between 1 and 200),
  value jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.visa_slot_state enable row level security;

-- No anon/authenticated policies are intentionally added.
-- The monitor should access this table only with SUPABASE_SERVICE_KEY.
-- Make the no-public-access intent explicit and self-documenting.
revoke all on public.visa_slot_state from anon, authenticated;
