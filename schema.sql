-- Trader IA 24/7 — esquema para Supabase (Postgres)
-- Corré esto una sola vez en Supabase → SQL Editor → New query → Run.

create table if not exists equity_history (
    id bigserial primary key,
    ts timestamptz not null,
    equity double precision not null
);

create table if not exists decisions (
    id bigserial primary key,
    ts timestamptz not null,
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
    ts timestamptz not null,
    symbol text not null,
    signal jsonb not null,
    risk_report jsonb not null,
    plan jsonb not null,
    resolved boolean not null default false
);

create index if not exists idx_decisions_ts on decisions (ts desc);
create index if not exists idx_equity_ts on equity_history (ts desc);

-- Posiciones abiertas y su cierre.
-- ACTUALIZADO (auditoría 02/09/2026): este comentario decía que
-- /api/manage_positions cerraba por target/stop fijo y que el trailing
-- stop dinámico solo vivía en modo VPS/local -- eso quedó desactualizado
-- cuando run_cycle() (app.py) empezó a llamar también a
-- position_manager.manage_open_positions() en modo serverless. Ahora
-- /api/manage_positions delega en el mismo PositionManager (ver app.py),
-- así que hay UNA sola implementación de la lógica de cierre/trailing
-- stop compartida entre los dos triggers (cron-job.org → /api/cycle y
-- GitHub Actions → /api/manage_positions), no dos independientes.
--
-- uq_open_trades_symbol: constraint de unicidad a nivel DB, de refuerzo.
-- has_open_trade_for_symbol() en supabase_db.py ya bloquea a nivel
-- aplicación abrir una segunda posición del mismo símbolo, pero es un
-- SELECT-then-INSERT sin lock: dos invocaciones casi simultáneas de
-- /api/cycle (ej. cron-job.org + un workflow_dispatch manual) podrían
-- ambas pasar el chequeo antes de que cualquiera inserte. El índice
-- único convierte ese caso en un error de INSERT explícito en vez de
-- una duplicación silenciosa; supabase_db.py lo atrapa y lo trata como
-- "ya hay posición abierta" (ver add_open_trade()).
create table if not exists open_trades (
    id bigserial primary key,
    symbol text not null,
    direction text not null,
    entry_price double precision not null,
    current_stop double precision not null,
    target_price double precision not null,
    position_size double precision not null,
    order_id text,
    ts_opened timestamptz not null,
    stop_distance double precision
);

create unique index if not exists uq_open_trades_symbol on open_trades (symbol);

create table if not exists closed_trades (
    id bigserial primary key,
    symbol text not null,
    direction text not null,
    entry_price double precision not null,
    exit_price double precision not null,
    outcome text not null,
    r_multiple double precision,
    ts_opened timestamptz not null,
    ts_closed timestamptz not null
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
    ts_signaled timestamptz not null,
    outcome text,
    exit_price double precision,
    ts_resolved timestamptz
);

-- Señales de clima con la probabilidad estimada por el modelo (my_prob) —
-- weather_track_results resuelve outcome ('yes'/'no') mirando si el precio
-- del token convergió a ~1 o ~0, y con eso se puede medir calibración real
-- (Brier score) en vez de confiar a ciegas en la estimación.
create table if not exists weather_signals (
    id bigserial primary key,
    condition_id text not null,
    question text,
    event_title text,
    station_icao text,
    my_prob double precision not null,
    market_price double precision not null,
    ev double precision,
    center_estimate_f double precision,
    sigma double precision,
    yes_token_id text,
    ts_signaled timestamptz not null,
    outcome text,
    ts_resolved timestamptz
);

create table if not exists indicator_snapshots (
    id bigserial primary key,
    symbol text not null,
    ts timestamptz not null,
    price double precision,
    rsi double precision,
    atr_pct double precision,
    volume_ratio double precision,
    volatility double precision,
    momentum double precision,
    trend_align double precision,
    trend_bias text
);

create table if not exists polymarket_notify_state (
    condition_id text primary key,
    direction text not null,
    score double precision not null,
    ts timestamptz not null
);

create index if not exists idx_closed_trades_ts on closed_trades (ts_closed desc);
create index if not exists idx_polymarket_signals_outcome on polymarket_signals (outcome);
create index if not exists idx_weather_signals_outcome on weather_signals (outcome);
create index if not exists idx_indicator_snapshots_symbol_ts on indicator_snapshots (symbol, ts desc);
