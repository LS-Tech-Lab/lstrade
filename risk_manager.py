"""
Módulo de riesgo — con filtro de Spread/Liquidez añadido.
"""
import logging

from format_utils import format_money, direction_label

log = logging.getLogger("risk_manager")


def drawdown_risk_multiplier(config, dd_pct):
    """
    NUEVO (15/09/2026, pedido del usuario): reemplaza el bloqueo binario de
    drawdown por un throttle continuo -- ver AUDITORÍA larga en
    DRAWDOWN_THROTTLE_START_PCT (config.py) sobre por qué: el equity de
    cripto está aislado de Polymarket/MLB/clima, así que "esperar a que
    suba el equity" para destrabar dependía solo del paso del tiempo
    (peak_equity_window saliendo de la ventana), que podía tardar más de
    un día sin que el bot operara nada mientras tanto.

    Por debajo de DRAWDOWN_THROTTLE_START_PCT: riesgo normal (mult=1.0).
    Entre ese punto y MAX_DRAWDOWN_PCT: decae linealmente hasta el piso
    DRAWDOWN_MIN_RISK_MULT. Por encima de MAX_DRAWDOWN_PCT (el límite que
    antes bloqueaba del todo): se mantiene en ese piso -- nunca llega a
    cero, así que position_size nunca da cero por esto solo y el trade
    sigue pudiendo abrirse, con tamaño reducido. El circuit breaker real
    (MAX_DRAWDOWN_KILL_PCT, contra el máximo histórico) es el que sigue
    parando todo de verdad, sin cambios acá.
    """
    start = config.DRAWDOWN_THROTTLE_START_PCT
    cap = config.MAX_DRAWDOWN_PCT
    min_mult = config.DRAWDOWN_MIN_RISK_MULT
    if dd_pct <= start:
        return 1.0
    if dd_pct >= cap or cap <= start:
        return min_mult
    progress = (dd_pct - start) / (cap - start)
    return 1.0 - progress * (1.0 - min_mult)


def adaptive_atr_stop_mult(config, volatility_pct):
    """
    Múltiplo de ATR para el stop, escalado por régimen de volatilidad si
    ADAPTIVE_ATR_STOP está activo (ver config.py). Compartido entre
    risk_manager.check() (producción) y backtest.py, para que el backtest
    mida exactamente lo mismo que corre en vivo.
    """
    if not getattr(config, "ADAPTIVE_ATR_STOP", False):
        return config.ATR_STOP_MULT
    ref = getattr(config, "ATR_STOP_VOL_REF_PCT", 1.0) or 1.0
    scale = volatility_pct / ref if ref > 0 else 1.0
    mult = config.ATR_STOP_MULT * scale
    return max(config.ATR_STOP_MULT_MIN, min(config.ATR_STOP_MULT_MAX, mult))


def volatility_regime(config, vol_pct):
    """
    Clasifica el régimen de volatilidad actual (misma métrica vol_pct que
    ya usa adaptive_atr_stop_mult() para el ancho del stop) en
    low/normal/high/extreme y devuelve un multiplicador de TAMAÑO de
    posición -- a diferencia de adaptive_atr_stop_mult(), que solo cambia
    el ancho del stop, esto reduce cuánto capital se arriesga cuando el
    mercado está más violento de lo normal (y lo aumenta levemente cuando
    está calmo).

    NUEVO (15/09/2026, investigación sobre CloddsBot -- src/risk/volatility.ts):
    mismo concepto de régimen con multiplicadores low=1.2x/normal=1.0x/
    high=0.5x/extreme=0.25x, adaptado a los umbrales de vol_pct que ya usa
    este codebase. Los umbrales (VOL_REGIME_*_PCT en config.py) quedan
    deliberadamente por debajo de MAX_VOLATILITY_PCT (el bloqueo duro) a
    propósito -- dan un degradado de tamaño antes de la pared dura, en vez
    de pasar de tamaño completo a bloqueo total de un salto. Mismo
    principio que ya llevó a reemplazar el bloqueo binario de drawdown por
    un throttle continuo (ver drawdown_risk_multiplier() arriba). Punto de
    partida sin validar todavía contra resultados reales.
    """
    low = getattr(config, "VOL_REGIME_LOW_PCT", 0.5)
    high = getattr(config, "VOL_REGIME_HIGH_PCT", 1.5)
    extreme = getattr(config, "VOL_REGIME_EXTREME_PCT", 3.0)
    mults = {
        "low": getattr(config, "VOL_REGIME_MULT_LOW", 1.2),
        "normal": getattr(config, "VOL_REGIME_MULT_NORMAL", 1.0),
        "high": getattr(config, "VOL_REGIME_MULT_HIGH", 0.5),
        "extreme": getattr(config, "VOL_REGIME_MULT_EXTREME", 0.25),
    }
    if vol_pct <= low:
        regime = "low"
    elif vol_pct <= high:
        regime = "normal"
    elif vol_pct <= extreme:
        regime = "high"
    else:
        regime = "extreme"
    return regime, mults[regime]


def compute_var_cvar(pnl_pct_series, confidence=0.95):
    """
    VaR/CVaR históricos sobre una serie de retornos % (uno por trade
    cerrado/resuelto del módulo, ver Database.recent_equity_returns()).

    NUEVO (15/09/2026, investigación sobre CloddsBot -- src/risk/var.ts):
    VaR = la pérdida en el percentil (1-confidence) de la distribución
    observada; CVaR (Expected Shortfall) = pérdida PROMEDIO en la cola más
    allá de ese percentil -- más informativo que VaR solo porque VaR no
    dice nada de qué tan mala es la cola, solo dónde empieza.

    Puramente informativo por ahora (no bloquea ningún trade, se muestra
    en risk_report y en la bitácora) -- mismo criterio de "punto de
    partida sin validar todavía contra resultados reales" que ya se aplicó
    a otros umbrales de este archivo. Devuelve None con muestra chica (< 5
    retornos) en vez de un número que no significa nada todavía.
    """
    if not pnl_pct_series or len(pnl_pct_series) < 5:
        return None
    sorted_pnls = sorted(pnl_pct_series)
    n = len(sorted_pnls)
    idx = max(0, min(n - 1, int((1 - confidence) * n)))
    tail = sorted_pnls[:idx + 1]
    var_pct = -sorted_pnls[idx]
    cvar_pct = -(sum(tail) / len(tail))
    return {
        "var_pct": max(0.0, var_pct),
        "cvar_pct": max(0.0, cvar_pct),
        "confidence": confidence,
        "sample_size": n,
    }


def format_blocked_message(symbol, signal, failed_checks):
    """
    Arma el mensaje de Telegram para una señal bloqueada por riesgo.
    Antes era una sola línea con todos los checks fallidos pegados por
    coma (`', '.join(failed)`) — ilegible en el celular apenas fallaba más
    de un check, y sin contexto de la señal (había que scrollear al mensaje
    anterior de "Señal detectada" para ver dirección/confianza/precio).
    Ahora: un check fallido por línea, más el contexto de la señal arriba.
    Centralizado acá porque los dos entrypoints (app.py y main.py)
    mandaban este mensaje por separado con el mismo texto.

    FIX (07/09/2026): failed_checks ahora es la lista de dicts de check
    completos (no solo los labels) para poder usar "fail_reason" cuando
    existe -- algunos labels (pensados para el checklist neutral del
    dashboard) leen como doble negación al mostrarse solos con ✕ delante
    (ver el check de "posición ya abierta" más abajo en este archivo).

    AUDITORÍA (11/09/2026): se agrega espacio en blanco entre el título,
    la frase de contexto y la línea de la señal (antes iban todas
    pegadas, apretado de leer en el celular) -- mismo criterio de
    respiración que ya se aplicó a los memos de cripto/Polymarket. Se
    agrega también una línea de cierre aclarando que no hace falta
    ninguna acción (el mensaje podía leerse como un aviso que requiere
    respuesta, cuando en realidad es puramente informativo -- el bot ya
    decidió no operar).
    """
    stars = "★" * signal.get("confidence", 0)
    price_str = format_money(signal.get("price"))
    control_word = "control" if len(failed_checks) == 1 else "controles"
    lines = [
        f"\u26D4 *{symbol} bloqueado por riesgo*",
        "",
        # AUDITORÍA (06/09/2026): se agrega esta frase en criollo antes de
        # la lista de checks — antes iba directo a la lista técnica y no
        # quedaba explícito que la conclusión es "el bot vio la señal pero
        # NO va a operar esto".
        "El bot detectó esta señal pero decidió no operarla:",
        f"{signal.get('type', '—')} · {direction_label(signal.get('direction'))} · Confianza {stars or '—'} · Precio {price_str}",
        "",
        f"No pasó {'este' if len(failed_checks) == 1 else 'estos'} {control_word} de seguridad:",
    ]
    lines.extend(f"\u2715 {c.get('fail_reason') or c['label']}" for c in failed_checks)
    lines.append("")
    lines.append("_No hace falta que hagas nada — es solo informativo._")
    return "\n".join(lines)


class RiskManager:
    def __init__(self, config, db):
        self.config = config
        self.db = db

    def is_halted(self):
        return self.db.get_state("trading_halted", "0") == "1"

    def halt(self, reason):
        self.db.set_state("trading_halted", "1")
        self.db.set_state("halt_reason", reason)
        log.error(f"CIRCUIT BREAKER ACTIVADO: {reason}. El sistema no operará hasta reinicio manual.")

    def manual_reset(self):
        self.db.set_state("trading_halted", "0")
        self.db.set_state("halt_reason", "")

    def update_equity_and_check_kill_switch(self, equity):
        self.db.record_equity(equity)
        peak = self.db.peak_equity() or equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        if dd_pct >= self.config.MAX_DRAWDOWN_KILL_PCT and not self.is_halted():
            self.halt(f"Drawdown {dd_pct:.2f}% superó el límite crítico de {self.config.MAX_DRAWDOWN_KILL_PCT}%")
        return dd_pct

    def check(self, symbol, signal, equity, ticker=None):
        atr_val = signal["atr"]
        price = signal["price"]
        vol_pct = signal["volatility"] * 100
        stop_mult = adaptive_atr_stop_mult(self.config, vol_pct)
        stop_distance = atr_val * stop_mult

        # FIX (15/09/2026, pedido del usuario): peak_equity() (máximo
        # histórico, sin ventana) atrapaba el bot para siempre una vez
        # cruzado MAX_DRAWDOWN_PCT -- ver AUDITORÍA larga en
        # MAX_DRAWDOWN_WINDOW_DAYS (config.py). Este gate ahora mide contra
        # el peak de los últimos MAX_DRAWDOWN_WINDOW_DAYS días en vez del
        # histórico completo, así que un peak viejo que ya no se puede
        # alcanzar sin un trade ganador eventualmente sale de la ventana y
        # el drawdown se diluye solo. El circuit breaker real (15%,
        # update_equity_and_check_kill_switch más arriba) sigue midiendo
        # contra el máximo histórico a propósito -- ese es intencionalmente
        # permanente hasta reset manual.
        window_days = getattr(self.config, "MAX_DRAWDOWN_WINDOW_DAYS", 7.0)
        peak = self.db.peak_equity_window(module="crypto", days=window_days) or equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0

        # NUEVO (15/09/2026, pedido del usuario): el drawdown ya no bloquea
        # la señal entera -- ver drawdown_risk_multiplier() más arriba en
        # este archivo. En vez de eso reduce el tamaño de la posición, así
        # que se aplica ACÁ, antes de calcular risk_amount/position_size,
        # para que el tamaño reducido llegue solo a trade_planner.py sin
        # tocar ese archivo.
        drawdown_risk_mult = drawdown_risk_multiplier(self.config, dd_pct)

        # NUEVO (15/09/2026, investigación sobre CloddsBot): igual que el
        # throttle de drawdown de arriba, el régimen de volatilidad también
        # se aplica ACÁ como un multiplicador más sobre risk_amount, antes
        # de exposure_pct/checks -- ver volatility_regime() arriba en este
        # archivo. Se multiplica junto con drawdown_risk_mult (ambos son
        # reductores independientes del mismo tamaño base).
        vol_regime, regime_size_mult = volatility_regime(self.config, vol_pct)

        risk_amount = equity * (self.config.RISK_PCT_PER_TRADE / 100) * drawdown_risk_mult * regime_size_mult
        position_size = risk_amount / stop_distance if stop_distance > 0 else 0

        exposure_pct = self.db.current_exposure_pct(equity)

        # NUEVO (15/09/2026, investigación sobre CloddsBot -- src/risk/var.ts):
        # VaR/CVaR históricos de cripto, puramente informativos (ver
        # compute_var_cvar() arriba) -- nunca bloquean el trade ni cambian
        # el tamaño, solo quedan visibles en risk_report y en la bitácora
        # para poder mirarlos junto al resto de los checks. Envuelto en
        # try/except: un fallo acá (ej. DB momentáneamente no disponible)
        # no debe tumbar el check de riesgo completo, mismo criterio que ya
        # se aplica al resto de este método.
        try:
            recent_returns = self.db.recent_equity_returns(
                module="crypto", limit=getattr(self.config, "VAR_LOOKBACK_TRADES", 100)
            )
            var_result = compute_var_cvar(
                recent_returns, confidence=getattr(self.config, "VAR_CONFIDENCE", 0.95)
            )
        except Exception as e:
            log.warning(f"No se pudo calcular VaR/CVaR: {e}")
            var_result = None
        
        # NUEVO: las etiquetas ahora incluyen el valor actual, no solo el
        # umbral — antes decían por ejemplo "Exposición < 20%" tanto si
        # pasaba como si fallaba, así que un check bloqueado no decía por
        # cuánto se pasó (¿19.9% o 45%?). Eso obligaba a ir a mirar los
        # campos sueltos de risk_report en vez de leer el motivo solo.
        #
        # NUEVO (15/09/2026): el check de Drawdown pasó de bloqueante
        # ("ok": dd_pct < MAX_DRAWDOWN_PCT) a informativo ("ok" siempre
        # True) -- ya no puede tumbar la señal, solo informa el % de
        # drawdown y qué % de riesgo quedó aplicado por el throttle. Sigue
        # apareciendo en la bitácora para que quede visible cuándo el bot
        # está operando con tamaño reducido por esto.
        checks = [
            {"label": "Tamaño de posición calculable", "ok": stop_distance > 0 and position_size > 0},
            {"label": f"Exposición: {exposure_pct:.1f}% < {self.config.MAX_EXPOSURE_PCT}%", "ok": exposure_pct < self.config.MAX_EXPOSURE_PCT},
            {"label": f"Drawdown: {dd_pct:.1f}% (riesgo ajustado a {drawdown_risk_mult * 100:.0f}%)", "ok": True},
            # NUEVO (15/09/2026): la etiqueta ahora incluye el régimen de
            # volatilidad y el multiplicador de tamaño que le aplicó (ver
            # volatility_regime() arriba) -- el bloqueo real sigue siendo
            # el mismo de siempre (vol_pct < MAX_VOLATILITY_PCT), esto solo
            # hace visible el degradado de tamaño que ya venía antes del
            # bloqueo duro.
            {"label": f"Volatilidad: {vol_pct:.2f}% < {self.config.MAX_VOLATILITY_PCT}% (régimen {vol_regime}, tamaño ×{regime_size_mult:.2f})", "ok": vol_pct < self.config.MAX_VOLATILITY_PCT},
            {"label": "Sistema no detenido por circuit breaker", "ok": not self.is_halted(),
             "fail_reason": "El sistema está detenido por el circuit breaker"},
        ]

        # NUEVO (15/09/2026, investigación sobre CloddsBot): informativo
        # puro -- no tiene "fail_reason" porque ok siempre es True, nunca
        # bloquea nada. Solo aparece si ya hay muestra suficiente (ver
        # compute_var_cvar(), mínimo 5 retornos) para no mostrar un número
        # que todavía no significa nada con 1-2 trades cerrados.
        if var_result:
            checks.append({
                "label": (
                    f"VaR {var_result['confidence']*100:.0f}% (últimos {var_result['sample_size']} trades): "
                    f"-{var_result['var_pct']*100:.1f}% · CVaR: -{var_result['cvar_pct']*100:.1f}%"
                ),
                "ok": True,
            })
        
        # NUEVO: antes esto era "ok": True con el comentario "fallo seguro"
        # — pero aprobar automáticamente cuando FALTAN los datos es fail-OPEN,
        # no fail-safe. Un chequeo de riesgo que no puede verificarse debe
        # bloquear, no pasar de largo. (Y antes de esto, app.py ni siquiera
        # pasaba `ticker`, así que esta rama corría siempre — el spread
        # nunca bloqueó nada; ver el fix en app.py que ahora sí lo trae.)
        if ticker and "bid" in ticker and "ask" in ticker and ticker["bid"] > 0:
            spread_pct = ((ticker["ask"] - ticker["bid"]) / ticker["bid"]) * 100
            checks.append({"label": f"Spread: {spread_pct:.2f}% < {self.config.MAX_SPREAD_PCT}%", "ok": spread_pct < self.config.MAX_SPREAD_PCT})
        else:
            checks.append({"label": "Spread (datos no disponibles — bloqueado por seguridad)", "ok": False})

        # NUEVO: Exposición correlacionada — evita que varias posiciones en la
        # misma dirección (LONG o SHORT), aunque sean símbolos distintos,
        # terminen siendo una sola apuesta concentrada disfrazada de cartera
        # diversificada. Ver MAX_CORRELATED_POSITIONS en config.py.
        correlated_count = self.db.count_open_trades_by_direction(signal["direction"])
        checks.append({
            "label": f"Posiciones correlacionadas ({signal['direction']}): {correlated_count} < {self.config.MAX_CORRELATED_POSITIONS}",
            "ok": correlated_count < self.config.MAX_CORRELATED_POSITIONS,
        })

        # FIX: faltaba bloquear señales repetidas del MISMO símbolo. El check
        # de arriba solo cuenta el total de posiciones por dirección, así que
        # dejaba abrir el mismo símbolo 2-3 veces seguidas (una por cada
        # escaneo donde la señal seguía activa) antes de que
        # MAX_CORRELATED_POSITIONS recién ahí bloqueara por volumen, no por
        # duplicado. Este check es independiente del de correlación: aunque
        # todavía quede margen de posiciones correlacionadas, una señal sobre
        # un símbolo que ya tiene posición abierta se bloquea siempre.
        # FIX (07/09/2026): "Sin posición abierta ya en X" con ok=not
        # already_open leía como doble negación en el mensaje de Telegram
        # cuando fallaba -- "✕ Sin posición abierta ya en DOT/USDT" se
        # interpreta al revés de lo que pasó (reportado en vivo). label seguí
        # useda para el checklist neutral del dashboard (ahí funciona: es un
        # ✓/✕ al lado de una condición, como cualquier checklist); fail_reason
        # es lo que se muestra en el mensaje de bloqueo, en voz activa sobre
        # lo que realmente pasó.
        already_open = self.db.has_open_trade_for_symbol(symbol)
        checks.append({
            "label": f"Sin posición abierta ya en {symbol}",
            "ok": not already_open,
            "fail_reason": f"Ya hay una posición abierta en {symbol} — no se abre otra hasta cerrarla",
        })

        overall_pass = all(c["ok"] for c in checks)
        return {
            "pass": overall_pass,
            "checks": checks,
            "risk_amount": risk_amount,
            "position_size": position_size,
            "stop_distance": stop_distance,
            "atr_stop_mult": stop_mult,
            "exposure_pct": exposure_pct,
            "drawdown_pct": dd_pct,
            "drawdown_risk_mult": drawdown_risk_mult,
            "volatility_pct": vol_pct,
            "volatility_regime": vol_regime,
            "regime_size_mult": regime_size_mult,
            "var": var_result,
        }
