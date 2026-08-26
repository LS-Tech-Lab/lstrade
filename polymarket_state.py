"""
Deduplicación de señales de Polymarket entre ciclos.

Sin esto, cada ciclo vuelve a escanear desde cero y puede reenviar la misma
señal por Telegram una y otra vez (cada `--loop-interval` segundos) mientras
el mercado siga cumpliendo el umbral. Este store persiste en un JSON simple
en disco — no requiere Supabase ni nada adicional, para que siga funcionando
igual en modo local.

Una señal se vuelve a notificar solo si:
  - pasó más de `resend_cooldown_hours` desde la última vez que se avisó, o
  - la dirección (YES/NO) cambió, o
  - el score subió al menos `min_score_increase_pct` respecto al último envío.
"""
import json
import logging
import os
import time

log = logging.getLogger("polymarket_state")

DEFAULT_PATH = "polymarket_state.json"


class PolymarketStateStore:
    def __init__(self, path=None, resend_cooldown_hours=6.0, min_score_increase_pct=0.20):
        self.path = path or DEFAULT_PATH
        self.resend_cooldown_seconds = resend_cooldown_hours * 3600
        self.min_score_increase_pct = min_score_increase_pct
        self._state = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"No se pudo leer {self.path}, se arranca en blanco: {e}")
            return {}

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            log.warning(f"No se pudo guardar {self.path}: {e}")

    def should_notify(self, condition_id, direction, score):
        prev = self._state.get(condition_id)
        if prev is None:
            return True

        elapsed = time.time() - prev.get("ts", 0)
        if elapsed >= self.resend_cooldown_seconds:
            return True

        if prev.get("direction") != direction:
            return True

        prev_score = prev.get("score", 0) or 0
        if prev_score > 0 and score >= prev_score * (1 + self.min_score_increase_pct):
            return True

        return False

    def record_notified(self, condition_id, direction, score):
        self._state[condition_id] = {"ts": time.time(), "direction": direction, "score": score}
        self._save()
