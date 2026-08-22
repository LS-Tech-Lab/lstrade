# Trader IA 24/7

Sistema real de trading algorítmico en spot (sin apalancamiento) para criptomonedas,
vía [ccxt](https://github.com/ccxt/ccxt) (compatible con Binance, Kraken, KuCoin, Bybit, OKX y ~100 exchanges más).

Escanea el mercado, genera señales con indicadores técnicos reales (SMA, ATR, RSI, momentum),
las filtra con un módulo de riesgo que usa tu equity real, arma un plan de trading (entrada/stop/objetivo),
te lo manda por Telegram (o consola) para tu aprobación, y ejecuta la orden real si decidís que sí.

[![CI](https://github.com/TU_USUARIO/trader-ia-247/actions/workflows/ci.yml/badge.svg)](https://github.com/TU_USUARIO/trader-ia-247/actions)

## ⚠️ Antes de arrancar

- Esto opera con dinero real si activás `LIVE_TRADING=true`. Podés perder capital. No es asesoría financiera.
- Empezá siempre con `LIVE_TRADING=false` (modo papel) al menos un par de días para ver cómo se comporta
  con tu configuración de riesgo antes de poner dinero real.
- Creá una API key en el exchange **sin permiso de retiro (withdrawal)**, solo lectura + trading.
  Así, aunque la key se filtre, nadie puede sacar fondos de tu cuenta.
- El circuit breaker (`MAX_DRAWDOWN_KILL_PCT`) detiene el sistema completo si tu cuenta cae ese % desde
  su máximo histórico. Cuando se activa, hay que revisar manualmente y reiniciarlo con `--reset-halt`.
  No lo desactives ni lo subas a un número irrazonable "para que no moleste".

## Instalación

```bash
git clone https://github.com/TU_USUARIO/trader-ia-247.git
cd trader-ia-247
python3 -m venv venv
source venv/bin/activate          # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Editá `.env` con tu API key/secret del exchange y tus parámetros de riesgo.

## Uso

```bash
# un solo ciclo, para probar que todo conecta bien (recomendado primero)
python main.py --once

# loop continuo 24/7
python main.py

# si el circuit breaker se activó y ya revisaste manualmente lo que pasó
python main.py --reset-halt
```

Con `LIVE_TRADING=false` (default), nada de esto manda órdenes reales: todo se registra en
`trader_ia_247.db` (SQLite) como si se hubiera ejecutado, para que puedas revisar la bitácora.

Con `LIVE_TRADING=true` y `AUTO_EXECUTE=false` (default), cuando el sistema encuentra una señal
que pasa el módulo de riesgo, te muestra el memo de decisión en la consola y espera que escribas
`s` (sí, ejecutar), `w` (watchlist) o cualquier otra cosa (rechazar) antes de tocar dinero real.

Con `AUTO_EXECUTE=true`, ejecuta sin preguntar — usalo solo cuando ya confiés en la configuración.

## Notificaciones y aprobación por Telegram

Si configurás `NOTIFY_TELEGRAM=true`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`:

1. Recibís una alerta cada vez que se detecta una señal, se bloquea por riesgo, o se activa el circuit breaker.
2. Cuando hay una operación lista para aprobar (`AUTO_EXECUTE=false`), el memo llega con tres botones
   (✅ Aprobar / 👁 Watchlist / ❌ Rechazar) y el sistema espera tu toque hasta `APPROVAL_TIMEOUT_SECONDS`
   (15 min por defecto). Si no respondés a tiempo, rechaza automáticamente y te avisa.
3. Si Telegram no está configurado, cae automáticamente a pedir confirmación por consola — no rompe nada.

Para crear el bot: hablá con `@BotFather` en Telegram (`/newbot`), y con `@userinfobot` para conseguir tu `chat_id`.
Mandale al menos un mensaje a tu bot antes de arrancar el sistema (si no, no puede detectar tu respuesta).

## ¿Se puede desplegar en Vercel?

**Sí — con Supabase como base de datos.** Esta es la arquitectura ya incluida en el repo (`api/`, `supabase_db.py`, `vercel.json`). Dos cosas cambian respecto al modo VPS:

1. **El cron nativo de Vercel en el plan Hobby (gratis) solo corre 1 vez por día.** `vercel.json` trae ese cron diario como red de seguridad, pero el disparo real cada pocos minutos lo hace **GitHub Actions** (`.github/workflows/trigger-cycle.yml`), gratis, golpeando el endpoint con un secreto compartido. No hace falta pagar Pro.
2. **La aprobación humana ya no puede bloquear la función** (10s de timeout en Hobby). Por eso `api/cycle.py` solo manda el memo por Telegram con botones y corta — `api/telegram_webhook.py` es un endpoint aparte que recibe tu click y ahí sí ejecuta la orden real.

### Pasos para desplegarlo

**1. Supabase — crear las tablas**
Creá un proyecto en supabase.com, andá a *SQL Editor* y corré el contenido de `schema.sql` (una sola vez). Guardá tu `Project URL` y la **`service_role` key** (Settings → API) — no la `anon` key, porque el bot necesita escribir en las tablas del lado del servidor.

**2. Vercel — desplegar**
```bash
npm i -g vercel
vercel link          # conectá esta carpeta a un proyecto de Vercel
vercel env add SUPABASE_URL
vercel env add SUPABASE_KEY
vercel env add EXCHANGE_ID
vercel env add API_KEY
vercel env add API_SECRET
vercel env add SYMBOLS
vercel env add LIVE_TRADING          # true / false
vercel env add AUTO_EXECUTE          # true / false
vercel env add NOTIFY_TELEGRAM       # true
vercel env add TELEGRAM_BOT_TOKEN
vercel env add TELEGRAM_CHAT_ID
vercel env add TELEGRAM_WEBHOOK_SECRET   # un string random que inventes vos
vercel --prod
```
Al desplegar con el `crons` en `vercel.json`, Vercel crea solo la variable `CRON_SECRET`. Copiala del dashboard (Settings → Environment Variables) — la vas a necesitar en el paso 4.

**3. Telegram — apuntar el webhook a tu función**
```bash
curl -X POST "https://api.telegram.org/bot<TU_TOKEN>/setWebhook" \
  -d "url=https://tu-proyecto.vercel.app/api/telegram_webhook" \
  -d "secret_token=<EL_MISMO_VALOR_DE_TELEGRAM_WEBHOOK_SECRET>"
```

**4. GitHub Actions — disparar el ciclo cada N minutos**
En tu repo de GitHub: *Settings → Secrets and variables → Actions* y agregá:
- `VERCEL_CYCLE_URL` = `https://tu-proyecto.vercel.app/api/cycle`
- `VERCEL_CRON_SECRET` = el valor de `CRON_SECRET` que copiaste en el paso 2

El workflow ya viene configurado a `*/10 * * * *` (cada 10 min) — ajustalo a tu gusto en `.github/workflows/trigger-cycle.yml`. GitHub puede demorar la ejecución real unos minutos en horas de carga, y apaga los workflows programados si el repo pasa 60 días sin ningún commit — hacele caso a ese detalle si lo dejás mucho tiempo sin tocar.

**5. Probar**
```bash
curl -X POST https://tu-proyecto.vercel.app/api/cycle \
  -H "Authorization: Bearer <CRON_SECRET>"
```
Con `LIVE_TRADING=false` esto debería registrar el ciclo en Supabase (tabla `decisions`) sin ejecutar nada real. Revisalo desde el *Table Editor* de Supabase antes de pasar a `LIVE_TRADING=true`.

**Guía paso a paso completa** (con cada comando, en orden, incluyendo el dashboard): ver `GUIA_IMPLEMENTACION.md` en la raíz del repo.

### Qué NO cambia entre VPS y serverless

`indicators.py`, `signal_engine.py`, `risk_manager.py`, `trade_planner.py`, `exchange_client.py` y `executor.py` son exactamente los mismos archivos en los dos modos — la única diferencia real es la capa de persistencia (`db.py` SQLite vs `supabase_db.py` Postgres) y cómo se dispara el ciclo (loop propio vs. cron externo). Si en algún momento volvés al VPS, no perdés nada de lo que ajustaste acá.

## Correrlo 24/7 de verdad

Esto necesita quedar corriendo sin parar, así que en algún momento vas a querer un VPS
(DigitalOcean, Hetzner, un servidor propio, etc.) en vez de tu laptop. Una vez que tengas uno:

```bash
# opción simple: screen o tmux
tmux new -s trader
python main.py
# Ctrl+B, D para salir sin matar el proceso

# opción más robusta: systemd (Linux) — ya incluido en deploy/trader-ia.service
sudo cp deploy/trader-ia.service /etc/systemd/system/
sudo nano /etc/systemd/system/trader-ia.service   # ajustá User y WorkingDirectory
sudo systemctl daemon-reload
sudo systemctl enable --now trader-ia
sudo journalctl -u trader-ia -f                   # ver logs en vivo
```

Si tu ISP local llega a bloquear el exchange (pasó con Binance en Venezuela por bloqueo de DNS
del lado del gobierno, no del exchange), correr el bot en un VPS fuera del país resuelve el problema
de raíz sin depender de VPN en tu propia conexión.

## Estructura

| Archivo | Qué hace |
|---|---|
| `config.py` | Carga toda la configuración desde `.env` |
| `db.py` | SQLite: equity histórico, bitácora de decisiones, estado del circuit breaker |
| `exchange_client.py` | Único módulo que habla con el exchange real (ccxt) |
| `indicators.py` | SMA, EMA, ATR, RSI, volatilidad — matemática real sobre datos reales |
| `signal_engine.py` | Clasifica el setup: ruptura, pullback, momentum, continuación, reversión |
| `risk_manager.py` | Exposición, drawdown, volatilidad, circuit breaker |
| `trade_planner.py` | Entrada, stop, objetivo, tamaño de posición |
| `executor.py` | Coloca la orden real (o la simula en modo papel) |
| `telegram_notifier.py` | Alertas y aprobación humana — bloqueante (VPS) o por webhook (serverless) |
| `main.py` | Orquesta el loop 24/7 (modo VPS) |
| `db.py` | Persistencia SQLite (modo VPS) |
| `supabase_db.py` | Persistencia Postgres vía Supabase (modo serverless) |
| `api/cycle.py` | Función de Vercel: un ciclo de escaneo, disparada por cron externo |
| `api/telegram_webhook.py` | Función de Vercel: resuelve tu click de Telegram y ejecuta la orden |
| `api/reset_halt.py` | Función de Vercel: reinicia el circuit breaker en modo serverless |
| `dashboard/` | Panel Next.js — bitácora, equity y estado del sistema (ver `GUIA_IMPLEMENTACION.md`) |
| `schema.sql` | Tablas de Supabase — correr una vez en el SQL Editor |
| `deploy/trader-ia.service` | Unidad systemd para correrlo 24/7 en un VPS |
| `.github/workflows/ci.yml` | Chequeo automático de que el código compila y la lógica funciona |
| `.github/workflows/trigger-cycle.yml` | Dispara `api/cycle` cada N minutos (modo serverless) |

## Extensiones razonables (no incluidas todavía)

- Backtesting sobre datos históricos antes de arriesgar capital real.
- Trailing stop / cierre parcial de posiciones abiertas.
- Migración a Vercel + Supabase (ver sección de arriba) si preferís serverless a un VPS.
- Apalancamiento vía futuros — deliberadamente fuera de este diseño por el riesgo de liquidación;
  si lo querés, es una capa adicional sobre `exchange_client.py`, no un rediseño completo.
