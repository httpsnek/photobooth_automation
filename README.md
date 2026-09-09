# InstaBOX — self-service photobooth kiosk

Turns a dslrBooth photobooth into an unattended vending machine: the wall
tablet shows a QR, the guest pays by phone (Apple/Google Pay), the booth
shoots and prints, the screen resets. Money goes straight to the owner's
account. Built to run **one codebase across a whole fleet of booths** —
a new machine only needs its own `.env`.

```
[ Android tablet ]  ──HTTP──►  [ FastAPI backend (booth PC) ]  ──localhost──►  [ dslrBooth ]
      │ SSE /events ◄──────────────────  │  polls invoice status (outbound only)
      │ (scans QR)                       ▼
[ guest phone ] ───────► [ Monobank Acquiring ]
```

Everything is one asyncio process. A single background task drives the
session state machine — the only source of truth. The tablet is a dumb
display that renders whatever snapshot arrives over SSE.

---

## Quick start (local, no hardware)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults: PAYMENT_PROVIDER=mock, FAKE_TRIGGER_OK=1
uvicorn backend.app:app --reload --port 8000
```

- Kiosk: <http://localhost:8000/kiosk>
- Pay in mock mode: open the `pay_url` the log prints (`/mock/pay/<id>`) and click the button.
- Owner status page: <http://localhost:8000/status>
- Screen preview without SSE: `/kiosk?demo` (tap to step), `/kiosk?demo=auto`, `/kiosk?demo=shooting`

---

## Deploy a booth (Windows)

**Fast path — customer / a fresh booth:** `make_release.bat` builds a clean
`InstaBOX_release.zip` (source + scripts + docs, no `.git`/`.venv`/`.env`/logs).
On the booth PC: unzip it anywhere and run **`СТАРТ_ТУТ.bat` as administrator**.
It self-elevates via UAC, creates `.env` and opens it in Notepad for you to fill
(`BOOTH_ID`, `PRINTER_NAME`, `PRICE_UAH`, tokens…), then runs `setup.ps1`
(firewall rule for :8000, sleep/USB-suspend off, the two Scheduled Tasks) and
starts the kiosk. Manual Windows steps that remain (auto-login, dslrBooth
autostart, static IP, Fully Kiosk) are printed at the end and listed in
[`DEPLOY_CHECKLIST.md`](DEPLOY_CHECKLIST.md).

> **On-site verification: [`DEPLOY_CHECKLIST.md`](DEPLOY_CHECKLIST.md)** — a tick-box
> launch checklist (`.env`, Windows, dslrBooth, tablet, 8 field-test scenarios).
> [`DEPLOY.md`](DEPLOY.md) is the same in ordered "how-to" form.

### Manual clone (developer)

1. `git clone` the repo onto the booth PC.
2. Right-click **`setup.bat` → Run as administrator**. It:
   - checks Python / the port,
   - creates `.venv` and installs dependencies,
   - creates `.env` and asks for this booth's basics (id, name, price, tokens…),
   - registers a Scheduled Task that starts the kiosk **at logon** and
     **restarts it if it dies**.
3. Finish the machine setup (once):
   - Windows **auto-login** for the kiosk user.
   - **dslrBooth** in the Startup folder, set to fullscreen; assign its
     trigger key and put the same key in `DSLRBOOTH_HOTKEY`.
   - *(dslrBooth Pro, recommended)* set `DSLRBOOTH_EVENTS_ENABLED=1` +
     `DSLRBOOTH_EVENT_TOKEN=<random>`, then in dslrBooth **Settings › General ›
     Triggers** set the URL to
     `http://localhost:8000/dslrbooth/event?token=<random>`. Now the tablet's
     screens (and the 3·2·1 countdown) follow the real camera instead of a timer.
   - Give the PC a **static DHCP lease** on the router.
   - On the **Android tablet**: install *Fully Kiosk Browser*, point it at
     `http://<pc-ip>:8000/kiosk`, enable *Start on Boot*, *Screen Always On*,
     *Auto Reload on Connection Error*, *Relaunch on Crash*.
   - A small **UPS** on the PC + router rides out short power cuts.
4. Measure `SESSION_DURATION_SEC` / `PRINT_DURATION_SEC` with a stopwatch on
   the real booth and put them in `.env`, then restart the task.

Update a booth later: `update.bat` (`git pull` + deps), then restart the task.

---

## Configuration (`.env`)

Every booth-specific value lives in `.env` — see `.env.example` for the full
list with comments. The essentials:

| Key | Purpose |
|---|---|
| `BOOTH_ID` | unique id — goes into the DB filename, every log line, and each bank invoice |
| `BOOTH_NAME` | shown on screen, in the payment description, and in alerts |
| `SUPPORT_PHONE` / `SUPPORT_TELEGRAM` | error-screen contact — phone as text, Telegram @handle / link as a scannable QR |
| `PRICE_UAH`, `CURRENCY`, `SHOTS` | pricing |
| `PAYMENT_PROVIDER` | `mock` or `monobank` |
| `BANK_TOKEN` | Monobank acquiring X-Token (`api.monobank.ua` → Еквайринг) |
| `DSLRBOOTH_TRIGGER` | `hotkey` (basic) or `rest` (dslrBooth Pro) — how we *start* a session |
| `DSLRBOOTH_EVENTS_ENABLED`, `DSLRBOOTH_EVENT_TOKEN` | dslrBooth Pro Triggers drive the screens by real events, not timers |
| `DSLRBOOTH_PROC_NAMES`, `DSLRBOOTH_REQUIRE_FOREGROUND` | hotkey-mode liveness: no QR unless dslrBooth is actually running |
| `SESSION_DURATION_SEC`, `PRINT_DURATION_SEC` | measured on the real booth — used when events are off, or as the watchdog |
| `PRINTER_CHECK_ENABLED`, `PRINTER_NAME` | Windows: stop selling when the printer is offline / jammed / out of paper |
| `WATCH_TTL_SEC`, `REFUND_RETRY_BASE_SEC`, `REFUND_MAX_ATTEMPTS` | dead-letter queue — money owed is retried until the acquirer confirms |
| `TELEGRAM_ALERTS_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID` | owner alerts |
| `PAPER_CAPACITY`, `PAPER_WARN_PRINTS_LEFT` | thermal-paper tracking |
| `ADMIN_TOKEN` | protects `/admin/*` |
| `HEARTBEAT_URL` | optional — POST status JSON to a fleet collector |

Renamed keys keep working: `MONOBANK_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `DSLRBOOTH_REST_URL` are still accepted.

---

## Per-booth isolation

- **DB**: `data/<BOOTH_ID>.db` (SQLite — every session, revenue, print count).
- **Logs**: `logs/<BOOTH_ID>.log` (rotating, 2 MB × 5), every line tagged `[<BOOTH_ID>]`.
- **Invoices**: the bank `reference` is `"<BOOTH_ID>/<session-id>"` and the
  payer sees `"<BOOTH_NAME>: фотосесія"` — one statement, many booths.
- **Alerts**: every Telegram message is prefixed with the booth name + id.

---

## Endpoints

| Route | Who | What |
|---|---|---|
| `GET /kiosk` | tablet | the fullscreen UI |
| `GET /events` | tablet | SSE state stream |
| `GET /health` | monitoring | JSON: state, version, today's stats, paper left, uptime |
| `GET /status` | owner | the same, as a phone-friendly HTML page |
| `POST /admin/pause` · `/admin/resume` · `/admin/paper-reset` | owner | `?token=<ADMIN_TOKEN>` |
| `POST /webhook` | Monobank | payment accelerator; signature-checked, poller stays authoritative |
| `GET/POST /dslrbooth/event` | dslrBooth Pro | Trigger callbacks (`?event_type=…`) — advance the session on real camera events; `?token=<DSLRBOOTH_EVENT_TOKEN>` |

---

## Alerts the owner gets

payment received · payment-but-no-shot (with auto-refund result) ·
booth offline / back online · paused / resumed · **paper low** ·
interrupted session recovered after a restart · nightly summary.

---

## Fleet management

Point `HEARTBEAT_URL` at any collector (a Google Apps Script webhook, n8n,
a tiny Flask app…). Each booth POSTs this every `HEARTBEAT_INTERVAL_SEC`:

```json
{"booth_id":"booth-002","booth_name":"…","version":"1.0.0","state":"AWAITING_PAYMENT",
 "paused":false,"down":false,"uptime_sec":81234,
 "today":{"sessions":47,"revenue":7050,"prints":47},"paper_left":213,"last_error":null,"ts":…}
```

That's enough for a live grid of every booth: who's earning, who's offline,
who needs paper. No inbound connection to the booths required.

---

## Project layout

```
backend/
  app.py            FastAPI routes + lifespan
  controller.py     the session state machine (single async task)
  state.py          FSM enum, SQLite store, SSE broadcaster
  payment.py        PaymentProvider: mock | monobank (invoice / status / refund / webhook sig)
  dslrbooth.py      BoothTrigger: hotkey | rest | fake  + process-liveness check
  booth_events.py   normalises dslrBooth Pro "Trigger" callbacks -> FSM events
  printer.py        Windows print-queue health (jam / offline / paper-out)
  notify.py         Telegram alerts (booth-tagged)
  config.py         all of .env, one place
  logging_setup.py  console + rotating file, booth-tagged
frontend/
  templates/kiosk.html   one document, JS swaps screens
  static/css/style.css   light theme, brand pink, Montserrat (bundled)
  static/js/kiosk.js      SSE listener + screen renderer + demo modes
  static/vendor/morphicons.js   animated icon web component (bundled, MIT)
СТАРТ_ТУТ.bat           customer entry point — self-elevates, .env wizard, setup, start
make_release.py / .bat  build a clean InstaBOX_release.zip for handover
setup.ps1 / setup.bat   Windows installer — venv, firewall, power, Scheduled Tasks
run.bat                 launcher with crash-restart loop (also starts the watchdog)
watchdog.py / .bat      external watchdog — restarts a wedged backend, escalates to PC reboot
check.bat               pre-flight self-check (backend.selfcheck)
update.bat              git pull + deps
```

### Two-layer recovery

1. **In-process** — `controller.run()` catches any exception in the session
   loop and `_recover_stuck_session()` refunds + resets. Handles ~everything
   while the asyncio loop is still alive.
2. **External** — `watchdog.py` polls `/health` every `WATCHDOG_INTERVAL_SEC`.
   No answer `WATCHDOG_FAIL_LIMIT`× in a row, or the same session frozen in a
   non-`OUT_OF_SERVICE` state past `WATCHDOG_STUCK_SEC` → it kills the backend
   (`run.bat` restarts it). `WATCHDOG_REBOOT_AFTER_KILLS` restarts in the
   window → `shutdown /r`. Runs as its own Scheduled Task **and** from `run.bat`
   (a pidfile lock keeps it single); every action is logged to
   `logs/watchdog.log` and pinged to Telegram.

---

## Status / TODO

Done and verified on mock: full session flow (timer **and** event-driven),
trigger-failure → retry → auto-refund, booth-event watchdog → refund,
**stuck-session self-recovery** (any crash mid-flow → refund + reset),
**preflight gate** (no QR unless dslrBooth is up, paper > 0, printer OK),
**late-payment capture** + **refund dead-letter queue** (money owed is never
dropped — retried with backoff until `reversed`), idempotency guard against a
duplicated trigger, OUT_OF_SERVICE + recovery, restart reconciliation, per-booth
DB/logs, paper counter + alert, heartbeat, admin pause/resume, SSE reconnect.

Needs the real thing:
- **Monobank** — live test with a real `BANK_TOKEN` (create / poll / refund / webhook signature).
- **dslrBooth** — `hotkey` (or `rest`) trigger on the actual Windows PC + camera;
  with Pro, wire the Triggers URL and confirm the event names; measure the
  fallback timings either way.
- **Printer** — confirm the photo printer's Windows `PrinterStatus` strings for
  offline / out-of-paper (drivers vary) and tune `backend/printer.py` if needed.
- **On-site** — auto-login, dslrBooth autostart, Fully Kiosk, static IP, UPS.
