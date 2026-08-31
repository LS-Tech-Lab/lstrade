"""
Notificaciones y aprobación humana vía Telegram.

Permite dos cosas:
1. Alertas (nueva señal, circuit breaker activado) — solo informativo.
2. Aprobación real: manda el memo de decisión con botones inline
   (Aprobar / Watchlist / Rechazar) y espera (polling) tu respuesta,
   para no depender de tener una consola abierta en el servidor.

Requiere crear un bot con @BotFather en Telegram y conseguir tu chat_id
(hay bots como @userinfobot que te lo dan directo).
"""
import time
import logging
import requests

log = logging.getLogger("telegram_notifier")

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, config):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.timeout = config.APPROVAL_TIMEOUT_SECONDS
        self.enabled = bool(config.NOTIFY_TELEGRAM and self.token and self.chat_id)

    def _call(self, method, payload, timeout=15):
        url = API_BASE.format(token=self.token, method=method)
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def send_message(self, text):
        if not self.enabled:
            return None
        try:
            return self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"})
        except Exception as e:
            # NUEVO: si `text` trae _ * ` [ sin escapar (títulos de mercados
            # de Polymarket, nombres de ciudades, lo que sea que no
            # controlamos nosotros), Telegram devuelve 400 "can't parse
            # entities" y el mensaje se perdía ENTERO en silencio — ni vos
            # te enterabas de que hubo una señal. Antes de darnos por
            # vencidos, reintentamos una vez en texto plano (sin
            # parse_mode): se pierde el formato en negrita, pero el
            # contenido llega.
            if "can't parse entities" in str(e).lower() or "400" in str(e):
                try:
                    return self._call("sendMessage", {"chat_id": self.chat_id, "text": text})
                except Exception as e2:
                    log.warning(f"No se pudo enviar mensaje a Telegram (ni siquiera en texto plano): {e2}")
                    return None
            log.warning(f"No se pudo enviar mensaje a Telegram: {e}")
            return None

    def send_alert(self, text):
        return self.send_message(f"\U0001F514 {text}")

    def send_photo(self, photo_bytes, caption=None, filename="chart.png"):
        """
        Manda una imagen (bytes de un PNG, por ejemplo generado con
        matplotlib) como foto de Telegram. A diferencia de send_message,
        esto va como multipart/form-data, no JSON.
        """
        if not self.enabled:
            return None
        url = API_BASE.format(token=self.token, method="sendPhoto")
        data = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "Markdown"
        try:
            resp = requests.post(
                url, data=data,
                files={"photo": (filename, photo_bytes, "image/png")},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"No se pudo enviar la imagen a Telegram: {e}")
            return None

    def send_circuit_breaker(self, reason):
        return self.send_message(f"\U0001F6D1 *CIRCUIT BREAKER ACTIVADO*\n{reason}")

    def send_approval_request(self, memo_text):
        """
        Variante NO bloqueante para entornos serverless (Vercel): manda el memo
        con botones y devuelve el message_id de inmediato, sin esperar respuesta.
        La resolución de la decisión la maneja después un webhook aparte
        (ver api/telegram_webhook.py) cuando vos tocás un botón.
        """
        if not self.enabled:
            return None
        keyboard = {
            "inline_keyboard": [[
                {"text": "\u2705 Aprobar", "callback_data": "approve"},
                {"text": "\U0001F441 Watchlist", "callback_data": "watchlist"},
                {"text": "\u274C Rechazar", "callback_data": "reject"},
            ]]
        }
        try:
            result = self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": memo_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            })
        except Exception as e:
            # Ver el mismo fallback en send_message — acá es todavía más
            # importante: si esto se pierde, no solo no te enterás de la
            # señal, el ciclo entero queda trabado esperando una aprobación
            # que nunca vas a poder dar (mitigado además por
            # expire_stale_pending_decisions, pero mejor que ni haga falta).
            if "can't parse entities" in str(e).lower() or "400" in str(e):
                try:
                    result = self._call("sendMessage", {
                        "chat_id": self.chat_id, "text": memo_text, "reply_markup": keyboard,
                    })
                except Exception as e2:
                    log.warning(f"No se pudo enviar memo a Telegram (ni siquiera en texto plano): {e2}")
                    return None
            else:
                log.warning(f"No se pudo enviar memo a Telegram: {e}")
                return None
        if not result or not result.get("ok"):
            return None
        return result["result"]["message_id"]

    def answer_callback(self, callback_query_id, text=None):
        try:
            payload = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            self._call("answerCallbackQuery", payload)
        except Exception as e:
            log.warning(f"No se pudo responder el callback de Telegram: {e}")

    def set_webhook(self, url, secret_token=None):
        payload = {"url": url, "allowed_updates": ["callback_query"]}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._call("setWebhook", payload)

    def ask_approval(self, memo_text):
        """
        Manda el memo con botones y bloquea (polling) esperando la respuesta humana.
        Devuelve 'approved' / 'watchlist' / 'rejected', o None si Telegram no está
        configurado (en ese caso el llamador debe usar otro método de confirmación).
        """
        if not self.enabled:
            return None

        keyboard = {
            "inline_keyboard": [[
                {"text": "\u2705 Aprobar", "callback_data": "approve"},
                {"text": "\U0001F441 Watchlist", "callback_data": "watchlist"},
                {"text": "\u274C Rechazar", "callback_data": "reject"},
            ]]
        }
        try:
            result = self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": memo_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            })
        except Exception as e:
            log.warning(f"No se pudo enviar memo a Telegram: {e}")
            return None

        if not result or not result.get("ok"):
            return None

        message_id = result["result"]["message_id"]
        return self._poll_for_response(message_id)

    def _poll_for_response(self, message_id):
        decision_map = {"approve": "approved", "watchlist": "watchlist", "reject": "rejected"}
        deadline = time.time() + self.timeout
        last_update_id = None

        while time.time() < deadline:
            params = {"timeout": 20}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1
            try:
                result = self._call("getUpdates", params, timeout=25)
            except Exception as e:
                log.warning(f"Error consultando Telegram (getUpdates): {e}")
                time.sleep(3)
                continue

            for update in result.get("result", []):
                last_update_id = update["update_id"]
                cq = update.get("callback_query")
                if cq and cq.get("message", {}).get("message_id") == message_id:
                    data = cq.get("data")
                    try:
                        self._call("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    except Exception:
                        pass
                    if data in decision_map:
                        return decision_map[data]

        log.warning("Se agotó el tiempo de espera de aprobación por Telegram. Se rechaza por defecto.")
        self.send_message("\u23F1 Tiempo de espera agotado — operación rechazada automáticamente.")
        return "rejected"
