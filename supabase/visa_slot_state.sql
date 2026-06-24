create table if not exists public.visa_slot_state (
  key text primary key,
  value jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.visa_slot_state enable row level security;

-- No anon/authenticated policies are intentionally added.
-- The monitor should access this table only with SUPABASE_SERVICE_KEY.
