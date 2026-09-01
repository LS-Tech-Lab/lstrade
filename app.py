# ... (imports y configuración inicial de app.py permanecen igual) ...

def run_cycle():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange_client = ExchangeClient(config)
    risk_manager = RiskManager(config, db)
    notifier = TelegramNotifier(config)

    # NUEVO (Semana 1): Health Check del Exchange antes de iniciar cualquier ciclo.
    # Si la API del exchange está caída, en mantenimiento o la clave fue revocada,
    # es mejor abortar inmediatamente y avisar, en lugar de fallar silenciosamente.
    if config.LIVE_TRADING:
        try:
            test_symbol = config.SYMBOLS[0] if config.SYMBOLS else "BTC/USDT"
            exchange_client.fetch_ticker(test_symbol)
        except Exception as e:
            notifier.send_message(f"🚨 ALERTA CRÍTICA: Fallo de conexión con el exchange ({config.EXCHANGE_ID}). Ciclo abortado. Error: {e}")
            return {"status": "exchange_error", "detail": str(e)}

    if risk_manager.is_halted():
        reason = db.get_state("halt_reason", "desconocida")
        if db.get_state("halt_notified", "0") != "1":
            notifier.send_circuit_breaker(reason)
            db.set_state("halt_notified", "1")
            _touch_notification(db)
        return {"status": "halted", "reason": reason}
# ... (el resto de run_cycle permanece igual hasta el final de la función) ...


def run_manage_positions():
    config = Config  # NUEVO: asegurar que config esté disponible para el check
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange_client = ExchangeClient(config)
    notifier = TelegramNotifier(config)

    # NUEVO (Semana 1): Health Check antes de intentar cerrar posiciones reales.
    if config.LIVE_TRADING:
        try:
            test_symbol = config.SYMBOLS[0] if config.SYMBOLS else "BTC/USDT"
            exchange_client.fetch_ticker(test_symbol)
        except Exception as e:
            notifier.send_message(f"🚨 ALERTA CRÍTICA: Fallo de conexión con el exchange al gestionar posiciones. Error: {e}")
            return {"status": "exchange_error", "detail": str(e)}

    open_trades = db.get_open_trades()
    if not open_trades:
        return {"status": "no_open_trades"}
# ... (el resto de run_manage_positions permanece igual hasta el final de la función) ...
