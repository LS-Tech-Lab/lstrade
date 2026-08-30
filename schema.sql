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

-- Posiciones abiertas y su cierre (paper trading en modo serverless).
-- api/manage_positions.py cierra las que tocan target/stop; no incluye
-- trailing stop dinámico (eso vive solo en el modo VPS/local,
-- position_manager.py) para mantener la función rápida y simple.
create table if not exists open_trades (
    id bigserial primary key,
    symbol text not null,
    direction text not null,
    entry_price double precision not null,
    current_stop double precision not null,
    target_price double precision not null,
    position_size double precision not null,
    order_id text,
    ts_opened double precision not null,
    stop_distance double precision
);

create table if not exists closed_trades (
    id bigserial primary key,
    symbol text not null,
    direction text not null,
    entry_price double precision not null,
    exit_price double precision not null,
    outcome text not null,
    r_multiple double precision,
    ts_opened double precision not null,
    ts_closed double precision not null
);

create table if not exists polymarket_signals (
    id bigserial primary key,
    condition_id text not null,
    question text,
    direction text not null,
    token_id text not null,
    entry double precision not null,
    target double precision not null,
    stop double precision not null,
    ts_signaled double precision not null,
    outcome text,
    exit_price double precision,
    ts_resolved double precision
);

create index if not exists idx_closed_trades_ts on closed_trades (ts_closed desc);
create index if not exists idx_polymarket_signals_outcome on polymarket_signals (outcome);
