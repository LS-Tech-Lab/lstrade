# Trader IA 24/7

Sistema real de trading algorítmico en spot (sin apalancamiento) para criptomonedas,
vía [ccxt](https://github.com/ccxt/ccxt) (compatible con Binance, Kraken, KuCoin, Bybit, OKX y ~100 exchanges más).

Escanea el mercado, genera señales con indicadores técnicos reales (SMA, ATR, RSI, momentum),
las filtra con un módulo de riesgo que usa tu equity real, arma un plan de trading (entrada/stop/objetivo),
te lo manda por Telegram (o consola) para tu aprobación, y ejecuta la orden real si decidís que sí.

[![CI](https://github.com/LS-Tech-Lab/trader-ia-247/actions/workflows/ci.yml/badge.svg)](https://github.com/LS-Tech-Lab/trader-ia-247/actions)

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

## Módulo Polymarket (señales, solo lectura)

`polymarket_main.py` es un módulo aparte e independiente del bot de cripto — escanea los
mercados de [Polymarket](https://polymarket.com) (predicción de eventos) vía sus APIs públicas
Gamma y CLOB, sin necesidad de wallet ni autenticación. No ejecuta ninguna operación real
(eso requeriría una wallet on-chain en Polygon, fuera del alcance actual): solo analiza,
genera señales y te avisa por Telegram.

```bash
# un solo ciclo, analiza el top 20 por volumen (default) y termina
python polymarket_main.py --once

# loop continuo, cada 600s (default), analizando el top 30
python polymarket_main.py --top 30 --loop-interval 600
```

Cada ciclo manda dos cosas por Telegram (si `NOTIFY_TELEGRAM=true`):

1. **Market Watch**: resumen de los 5 mercados con más volumen 24h.
2. **Señales**: hasta 3 mercados donde el motor (`polymarket_signal_engine.py`) detecta
   ineficiencia de precio (YES + NO ≠ 1.00), momentum real en el historial de precios,
   alta rotación de capital o resolución próxima — descartando mercados en probabilidad
   extrema (< 5% o > 95%), donde no hay edge real y sí mucho riesgo de liquidez.

**Deduplicación**: como el loop no tiene memoria propia entre reinicios, `polymarket_state.py`
guarda en `polymarket_state.json` (local, no se sube a git) cuándo se avisó cada mercado por
última vez. Una señal solo se vuelve a mandar si pasaron `POLYMARKET_RESEND_COOLDOWN_HOURS`
(6h por defecto, configurable en `.env`) desde el último aviso, si cambió de dirección
(YES↔NO), o si el score subió al menos 20% — así no te satura Telegram con el mismo mercado
ciclo tras ciclo.

**Plan de salida sugerido:** en Polymarket no hace falta esperar a que el mercado resuelva
para ganar — como cualquier libro de órdenes, se puede revender la acción antes si el precio
se mueve a favor. Cuando hay momentum real detrás de una señal, el memo ahora incluye un
precio de entrada, uno de toma de ganancia y uno de salida por pérdida, calculados con la
volatilidad del historial de precios (mismo principio que `ATR_STOP_MULT`/`MIN_RR` en el
módulo cripto, configurable con `POLYMARKET_STOP_VOL_MULT` y `POLYMARKET_TARGET_RR` en el
`.env`). Es un punto de partida razonable, calculado igual que el stop/target de cripto, pero sin
validar todavía contra resultados reales — vale la pena tratarlo como una referencia, no
como una garantía, hasta tener suficiente historial de señales resueltas para ajustarlo.

## Backtesting del motor de señales

`backtest.py` responde una pregunta que el proyecto no tenía forma de contestar antes: si
`generate_signal()`, tal como está hoy (con sus pesos de score y umbrales fijos), tiene una
edge real contra datos históricos, o si nadie lo validó todavía.

```bash
# los símbolos de SYMBOLS en tu .env, últimos 180 días
python backtest.py --days 180

# un solo símbolo, con el detalle de cada trade en un CSV
python backtest.py --symbol ETH/USDT --days 90 --output resultados.csv

# simulando el trailing stop real de position_manager.py (no stop/target fijos)
python backtest.py --days 365 --simulate-trailing --output resultados.csv

# validación walk-forward: últimos 30% del período aparte, como out-of-sample
python backtest.py --days 365 --oos-frac 0.3
```

Corre símbolo por símbolo: cada vez que `generate_signal()` da señal (usando exactamente las
mismas ventanas de velas y el mismo sesgo de BTC que usa `main.py` en producción), abre una
posición simulada con el stop y el target que definen `ATR_STOP_MULT` (o el multiplicador
adaptativo si `ADAPTIVE_ATR_STOP=true`, ver más abajo) y `MIN_RR`, y camina vela a vela hasta
que uno de los dos se toque. Al final te da win rate, expectancy en R, drawdown máximo y
profit factor — por símbolo y en total.

**Limitaciones a tener en cuenta al leer los resultados:**
- Por defecto (sin `--simulate-trailing`) cada trade cierra en el stop o el target fijo, sin
  gestión activa. Con `--simulate-trailing` se replica la lógica exacta de breakeven a 1 ATR
  y trailing a 1.5 ATR — usá esta variante si querés que el número se parezca a lo que corre
  en producción de verdad.
- No simula comisiones ni slippage (escenario optimista).
- No simula exposición cruzada entre símbolos ni el circuit breaker de drawdown del sistema
  completo — es un backtest por símbolo, no una simulación del portafolio entero.

### Calibrar los pesos del score con datos reales

Los pesos del score (`momentum*1.2 + trend_align*0.8 - vol*1.5`, etc.) en `signal_engine.py`
son constantes elegidas a mano. `calibrate_weights.py` corre una regresión logística simple
sobre el CSV que genera el backtest para ver qué features predicen de verdad un trade ganador:

```bash
python backtest.py --days 365 --simulate-trailing --output resultados.csv
python calibrate_weights.py resultados.csv
```

No reemplaza los pesos automáticamente — imprime los coeficientes para que decidas si aplicarlos
(y re-backtestear después de cualquier cambio).

### ATR stop adaptativo por volatilidad

`ADAPTIVE_ATR_STOP=true` en `.env` hace que el múltiplo de ATR del stop escale según qué tan
alta esté la volatilidad reciente, en vez de usar siempre `ATR_STOP_MULT` fijo. Configurable con
`ATR_STOP_VOL_REF_PCT`, `ATR_STOP_MULT_MIN` y `ATR_STOP_MULT_MAX`. Se aplica igual en
`risk_manager.py` (producción) y `backtest.py`, para que no diverjan.

### Exposición correlacionada

`MAX_CORRELATED_POSITIONS` (default 3) limita cuántas posiciones abiertas simultáneas puede
haber en la misma dirección (LONG o SHORT), sin importar el símbolo — varias altcoins LONG a
la vez suelen ser, en la práctica, una sola apuesta direccional concentrada.

## Cierre de posiciones con resultado + estadísticas reales

Antes, una posición abierta quedaba en `open_trades` indefinidamente — `position_manager.py`
sólo movía el trailing stop, pero nunca detectaba ni registraba si el precio realmente había
tocado el stop o el target. Ahora sí: cuando eso pasa, la posición se cierra, se guarda el
resultado (`target`/`stop` y el R múltiplo) en `closed_trades`, y se manda un aviso a Telegram.

Con eso ya hay estadísticas reales disponibles:

```bash
# Dashboard local (SQLite) — sin depender de Vercel/Supabase, corre en tu VPS/PC
python local_dashboard.py            # http://localhost:8787

# Resumen semanal (win rate, expectancy, mejor/peor símbolo) a Telegram
python weekly_summary.py                  # últimos 7 días
python weekly_summary.py --days 30        # últimos 30 días
python weekly_summary.py --dry-run        # solo consola, no manda nada
```

Si usás el modo serverless (Vercel + Supabase), `schema.sql` ya trae las tablas
`open_trades`/`closed_trades`/`polymarket_signals` — volvé a correrlo en el SQL Editor si tu
proyecto es de antes de este cambio, y el dashboard de Next.js (`dashboard/`) ya muestra las
mismas tarjetas de win rate/expectancy/profit factor.

## Backtesting y tracking de resultados — Polymarket

Hasta ahora el módulo Polymarket no tenía ninguna forma de saber si `generate_polymarket_signal()`
tiene edge real. Dos piezas nuevas cierran ese gap:

**`polymarket_backtest.py`** — equivalente de `backtest.py` pero para mercados de Polymarket ya
resueltos: trae mercados cerrados, descarga el historial real de precios de los tokens YES/NO,
y camina el historial punto a punto simulando entrada/target/stop.

```bash
python polymarket_backtest.py --limit 100
python polymarket_backtest.py --limit 200 --output resultados_pm.csv
```

Ver las limitaciones documentadas al principio del archivo (liquidez/volumen usados son los
actuales, no los históricos de cada punto en el tiempo).

**Tracking de señales en vivo** — cada señal con plan de salida que `polymarket_main.py` manda
por Telegram ahora se registra en la base (`polymarket_signals`). `polymarket_track_results.py`,
corrido periódicamente, revisa esas señales pendientes y registra si tocaron el target o el stop:

```bash
python polymarket_track_results.py              # un chequeo y termina
python polymarket_track_results.py --loop 1800  # cada 30 min, en loop
```

Con eso, `db.polymarket_stats_summary()` (visible en `local_dashboard.py` y en el dashboard de
Next.js) ya refleja win rate real sobre señales resueltas, no solo señales enviadas.

**Gráfico de la señal** — cada señal con plan de salida ahora también manda una imagen a
Telegram con la curva de precio y la entrada/target/stop marcados (`polymarket_chart.py`),
usando el mismo historial que ya se descargaba para generar la señal.

## ¿Se puede desplegar en Vercel?

**Sí — con Supabase como base de datos, y así corre hoy en producción** (`lstrade.vercel.app`). Esta es la arquitectura ya incluida en el repo (`app.py`, `supabase_db.py`, `vercel.json`). Cosas a tener en cuenta respecto al modo VPS:

1. **Desde 2026, el runtime Python de Vercel ya no soporta "un archivo = una función" dentro de `api/`.** Construye una sola Vercel Function a partir de un único entrypoint en la raíz que exponga una variable `app` (ASGI) — acá es **`app.py`** (FastAPI), que registra las 8 rutas reales:
   `/api/cycle`, `/api/polymarket_cycle`, `/api/polymarket_resolve`, `/api/polymarket_track_results`
   (alias de `polymarket_resolve`, ver tabla de Estructura), `/api/manage_positions`, `/api/weather_cycle`,
   `/api/reset_halt` y `/api/telegram_webhook`.
   Los archivos en `api/*.py` **no se despliegan** — son referencia legible de la misma lógica, mantenida
   ahí para que cada endpoint se pueda leer aislado sin scrollear todo `app.py`. Toda ruta nueva tiene que
   agregarse como endpoint dentro de `app.py` o queda inalcanzable: Vercel la sirve, pero FastAPI le
   devuelve un 404 real (`{"detail":"Not Found"}`) porque nunca la registró — no un 401 de `CRON_SECRET`.
   Un 401 confirma que la ruta existe y pide auth; un 404 significa que falta agregarla a `app.py`.
2. **El cron nativo de Vercel en el plan Hobby (gratis) solo corre 1 vez por día.** `vercel.json` trae
   ese cron diario sobre `/api/cycle` como red de seguridad, pero el disparo real cada pocos minutos lo
   hace un cron externo (hoy, **cron-job.org**) golpeando cada endpoint directo con
   `Authorization: Bearer CRON_SECRET`. Los workflows de GitHub Actions (`trigger-*.yml`) quedaron solo
   con `workflow_dispatch` para forzar una corrida manual puntual — el `schedule` de GitHub Actions se
   sacó porque en la práctica corría cada 1-12h en vez de cada 10 min en intervalos cortos (limitación
   documentada de GitHub Actions, no un bug del código).
3. **La aprobación humana ya no puede bloquear la función** (10s de timeout en Hobby). Por eso el ciclo
   de cripto solo manda el memo por Telegram con botones y corta — `/api/telegram_webhook` es la ruta
   aparte que recibe tu click y ahí sí ejecuta la orden real.
4. **Heartbeat**: si pasan `HEARTBEAT_INTERVAL_SECONDS` (6h por defecto) sin que se mande ningún mensaje
   a Telegram, `app.py` manda un aviso corto de "sigo vivo" con equity/drawdown y el último snapshot de
   indicadores — para no confundir horas seguidas de "no_signal" con que el bot dejó de correr.

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

**4. cron-job.org (o similar) — disparar cada endpoint periódicamente**
Creá un cron job por endpoint que necesite correr seguido, apuntando directo a la URL de Vercel con el
header `Authorization: Bearer <CRON_SECRET>` (el valor que copiaste en el paso 2):

| Endpoint | Frecuencia sugerida |
|---|---|
| `/api/cycle` | cada 10 min |
| `/api/polymarket_cycle` | cada 10 min |
| `/api/polymarket_resolve` (= `/api/polymarket_track_results`) | cada 30 min |
| `/api/manage_positions` | cada 10-15 min |
| `/api/weather_cycle` | cada 30 min (el pronóstico no cambia tan rápido como el precio) |

Los workflows `.github/workflows/trigger-*.yml` quedaron como respaldo manual (`workflow_dispatch`
desde la pestaña Actions) — no como el disparador real, ver nota en la sección anterior. Antes de dar
por buena cualquier URL nueva en el cron externo, probala una vez a mano (paso 5) y confirmá que
responde 200 (o 401 si te olvidaste el header) — un 404 significa que la ruta no está en `app.py`.

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
| `polymarket_client.py` | Cliente de las APIs públicas de Polymarket (Gamma + CLOB), sin autenticación |
| `polymarket_signal_engine.py` | Detecta ineficiencia de precio y momentum en mercados de Polymarket |
| `polymarket_main.py` | Orquesta el loop de análisis de Polymarket (solo lectura, ver sección arriba) |
| `polymarket_state.py` | Deduplicación de señales de Polymarket entre ciclos (`polymarket_state.json`) |
| `polymarket_backtest.py` | Backtest de `generate_polymarket_signal()` sobre mercados resueltos reales |
| `polymarket_track_results.py` | Revisa señales de Polymarket pendientes y registra target/stop |
| `polymarket_chart.py` | Gráfico de la señal de Polymarket (precio + entrada/target/stop) para Telegram |
| `main.py` | Orquesta el loop 24/7 (modo VPS) |
| `backtest.py` | Backtest de `generate_signal()` sobre histórico real (ver sección arriba) |
| `calibrate_weights.py` | Calibra los pesos del score con regresión logística sobre resultados reales |
| `local_dashboard.py` | Dashboard local (SQLite) sin depender de Next.js/Vercel/Supabase |
| `weekly_summary.py` | Resumen semanal de performance real por Telegram |
| `db.py` | Persistencia SQLite (modo VPS) |
| `supabase_db.py` | Persistencia Postgres vía Supabase (modo serverless) |
| `weather_signal_engine.py` | Motor de análisis de clima para mercados de Polymarket (NWS + METAR/TAF, solo fuentes con API oficial) |
| `weather_report.py` | Modo manual del análisis de clima — reporte completo para correr vos mismo, sin tocar el ciclo automático |
| `polymarket_categories.py` | Categorización compartida de mercados de Polymarket por keywords (usada en producción y en el backtest offline) |
| `app.py` | **Único entrypoint real de Vercel** (FastAPI) — registra las 8 rutas serverless (`/api/cycle`, `/api/polymarket_cycle`, `/api/polymarket_resolve`, `/api/polymarket_track_results`, `/api/manage_positions`, `/api/weather_cycle`, `/api/reset_halt`, `/api/telegram_webhook`) y el heartbeat |
| `api/*.py` | Referencia legible de cada endpoint — **no se despliegan**; la lógica real vive en `app.py` (ver sección "¿Se puede desplegar en Vercel?") |
| `dashboard/` | Panel Next.js — bitácora, equity y estado del sistema (ver `GUIA_IMPLEMENTACION.md`) |
| `schema.sql` | Tablas de Supabase — correr una vez en el SQL Editor |
| `deploy/trader-ia.service` | Unidad systemd para correrlo 24/7 en un VPS |
| `.github/workflows/ci.yml` | Compila el código, corre la prueba sintética del motor de señales, y verifica que `app.py` registre todas las rutas que usan los crons externos |
| `.github/workflows/trigger-*.yml` | Respaldo manual (`workflow_dispatch`) por endpoint — el disparo periódico real lo hace cron-job.org, no GitHub Actions (ver sección de despliegue) |

## Extensiones razonables (no incluidas todavía)

- Migración a Vercel + Supabase (ver sección de arriba) si preferís serverless a un VPS.
- Apalancamiento vía futuros — deliberadamente fuera de este diseño por el riesgo de liquidación;
  si lo querés, es una capa adicional sobre `exchange_client.py`, no un rediseño completo.
- Recalibrar `calibrate_weights.py` con más historial y aplicar los coeficientes resultantes en
  `signal_engine.py` — hoy el script solo imprime los coeficientes, no los aplica solo.

## Cambios recientes

- **Corregido** (`app.py`, `vercel.json`, `.github/workflows/ci.yml`): `/api/weather_cycle` y
  `/api/polymarket_track_results` estaban en `api/*.py`, en los workflows y en `vercel.json`, pero
  nunca se habían registrado como endpoint dentro de `app.py` — Vercel construye una sola función
  desde ahí, así que cualquier cron externo apuntando a esas rutas recibía 404 real de FastAPI en
  silencio. Agregadas ambas rutas a `app.py`. De paso: `api/polymarket_track_results.py` resultó ser
  lógica duplicada de `/api/polymarket_resolve` (mismo `check_open_signals`) — quedó como alias en vez
  de reimplementarla, para no tener que tocar la URL ya cargada en el cron externo. `vercel.json`
  limpiado de entradas de `api/*.py` que la arquitectura real ignora. `ci.yml` ahora importa `app.py`
  y falla si falta alguna de las 8 rutas que usan los cron externos.
- **Corregido** (`indicators.py`): `ema()` sembraba con `closes[0]` sin importar `window`, y como
  `signal_engine` pasa una ventana rolling que se recorta distinto en cada ciclo, el punto de
  arranque cambiaba cada vez y el EMA "saltaba" en vez de evolucionar suavemente. Ahora siembra
  con `SMA(window)`, el estándar para reducir ese sesgo de arranque.
- **Nuevo** (`backtest.py`): `--simulate-trailing` (replica el trailing stop real de
  `position_manager.py`) y `--oos-frac` (split walk-forward in-sample/out-of-sample) — ver
  sección "Backtesting del motor de señales".
- **Nuevo** (`calibrate_weights.py`): calibración de los pesos del score con regresión logística
  sobre resultados reales del backtest.
- **Nuevo** (`risk_manager.py`, `db.py`, `config.py`): chequeo de exposición correlacionada
  (`MAX_CORRELATED_POSITIONS`) y ATR stop mult adaptativo por régimen de volatilidad
  (`ADAPTIVE_ATR_STOP`).
- **Nuevo** (`position_manager.py`, `db.py`): las posiciones abiertas ahora se cierran de verdad
  cuando el precio toca el stop o el target — antes solo se les movía el trailing stop, nunca se
  detectaba el cierre ni se registraba el resultado. Nueva tabla `closed_trades` con el R múltiplo
  de cada trade.
- **Nuevo** (`local_dashboard.py`, `weekly_summary.py`): dashboard local sin depender de
  Vercel/Supabase, y resumen semanal de performance real por Telegram.
- **Nuevo** (`dashboard/`, `schema.sql`, `supabase_db.py`): tarjetas de win rate/expectancy/profit
  factor real en el dashboard de Next.js, con las tablas y métodos equivalentes agregados también
  al modo serverless (Supabase) para que `risk_manager.check()` no rompa en `api/cycle.py`.
- **Corregido** (`polymarket_client.py`, `polymarket_backtest.py`): `fetch_active_markets()`
  mandaba siempre `active=true`, incluso pidiendo `closed=true` — combinación contradictoria en
  el modelo de datos de Polymarket (un mercado resuelto no puede seguir "activo"), que hacía que
  el backtest paginara sin encontrar nada útil hasta pegarle a un 422 de la API. Ahora `active`
  se ajusta solo según `closed` (override disponible si hace falta), y `polymarket_backtest.py`
  filtra mercados cerrados por volumen total en vez de liquidez — la liquidez de un mercado
  resuelto es ~0 estructuralmente (el order book se cierra), así que filtrar por ahí descartaba
  prácticamente todo.
- **Nuevo** (`polymarket_backtest.py`): backtest del motor de señales de Polymarket sobre mercados
  ya resueltos — antes no había ninguna forma de medir si tenía edge real.
- **Nuevo** (`polymarket_track_results.py`, `db.py`): tracking de resultados de señales de
  Polymarket con plan de salida (antes solo existía deduplicación de avisos, sin registro de
  resultado).
- **Nuevo** (`polymarket_chart.py`, `telegram_notifier.py`): gráfico de la curva de precio con
  entrada/target/stop marcados, enviado junto al memo de texto de cada señal de Polymarket.
- **Corregido** (`polymarket_client.py`): el historial de precios se pedía con el `conditionId`
  del mercado, pero la API CLOB (`/prices-history`) necesita el `clobTokenId` del outcome
  específico (YES o NO). Con el id equivocado, el endpoint devolvía historial vacío en
  silencio y el componente de momentum del score de Polymarket nunca se activaba.
- **Nuevo** (`polymarket_state.py`): deduplicación de señales de Polymarket entre ciclos —
  ver sección "Módulo Polymarket" más arriba.
- **Corregido** (`api/cycle.py`): el filtro de spread (`MAX_SPREAD_PCT`) no se aplicaba en modo
  serverless porque `risk_manager.check()` se llamaba sin el `ticker` del exchange. Ahora se
  trae el ticker antes de correr el chequeo de riesgo, igual que ya hacía `main.py` en modo VPS.
- **Nuevo** (`backtest.py`): backtest del motor de señales sobre histórico real — ver sección
  "Backtesting del motor de señales" más arriba. Requirió agregar el parámetro opcional
  `since` a `ExchangeClient.fetch_ohlcv()` para poder paginar histórico completo.
- **Corregido** (`polymarket_signal_engine.py`): el score podía dispararse solo con factores
  secundarios (rotación de capital, resolución próxima) sin ineficiencia de precio ni momentum
  real detrás — y sin momentum, la dirección se elegía apostando en contra de lo que ya fuera
  el precio más caro, sin ningún fundamento. Ahora ineficiencia o momentum son requisito para
  que la señal exista, rotación/resolución solo suman como bonus encima de eso, y sin momentum
  real no se elige dirección.
- **Nuevo** (`polymarket_signal_engine.py`, `polymarket_main.py`): plan de salida sugerido
  (entrada/target/stop) para señales con momentum real — no hace falta esperar a la
  resolución del mercado para tomar ganancia o cortar pérdida. Configurable con
  `POLYMARKET_STOP_VOL_MULT` y `POLYMARKET_TARGET_RR`.
