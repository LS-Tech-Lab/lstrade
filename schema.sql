-- Trader IA 24/7 — esquema para Supabase (Postgres)
-- Corré esto una sola vez en Supabase → SQL Editor → New query → Run.

create table if not exists equity_history (
    id bigserial primary key,
    ts double precision not null,
    equity double precision not null
);

create table if not exists decisions (
    id bigserial primary key,
    ts double precision not null,
    symbol text not null,
    signal_type text,
    direction text,
    confidence integer,
    risk_pass boolean,
    risk_detail jsonb,
    plan_detail jsonb,
    decision text,          -- paper_logged / blocked / approved / watchlist / rejected / auto_executed
    order_detail jsonb
);

create table if not exists bot_state (
    key text primary key,
    value text
);

-- Decisiones que ya pasaron riesgo y están esperando tu click en Telegram.
-- message_id es el ID del mensaje de Telegram con los botones — así el
-- webhook sabe a qué operación corresponde tu respuesta.
create table if not exists pending_decisions (
    message_id bigint primary key,
    ts double precision not null,
    symbol text not null,
    signal jsonb not null,
    risk_report jsonb not null,
    plan jsonb not null,
    resolved boolean not null default false
);

create index if not exists idx_decisions_ts on decisions (ts desc);
create index if not exists idx_equity_ts on equity_history (ts desc);
