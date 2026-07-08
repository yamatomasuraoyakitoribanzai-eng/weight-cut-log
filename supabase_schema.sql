create table if not exists weight_logs (
  id bigint generated always as identity primary key,
  measure_date date not null,
  weight numeric,
  body_fat numeric,
  muscle numeric,
  water numeric,
  memo text,
  created_at timestamp with time zone default now()
);

alter table weight_logs add column if not exists extra jsonb not null default '{}'::jsonb;

alter table weight_logs add constraint weight_logs_measure_date_key unique (measure_date);

create table if not exists app_config (
    id integer primary key default 1,
    weigh_in_date date,
    fight_date date,
    start_weight numeric,
    target_weight numeric,
    updated_at timestamptz not null default now(),
    constraint app_config_singleton check (id = 1)
);

alter table weight_logs disable row level security;
alter table app_config disable row level security;
