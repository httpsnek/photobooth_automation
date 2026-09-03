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
| `SUPPORT_PHONE` | printed on the error screens |
| `PRICE_UAH`, `CURRENCY`, `SHOTS` | pricing |
| `PAYMENT_PROVIDER` | `mock` or `monobank` |
| `BANK_TOKEN` | Monobank acquiring X-Token (`api.monobank.ua` → Еквайринг) |
| `DSLRBOOTH_TRIGGER` | `hotkey` (basic) or `rest` (dslrBooth Pro) |
| `SESSION_DURATION_SEC`, `PRINT_DURATION_SEC` | measured on the real booth |
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
  dslrbooth.py      BoothTrigger: hotkey | rest | fake
  notify.py         Telegram alerts (booth-tagged)
  config.py         all of .env, one place
  logging_setup.py  console + rotating file, booth-tagged
frontend/
  templates/kiosk.html   one document, JS swaps screens
  static/css/style.css   light theme, brand pink, Montserrat (bundled)
  static/js/kiosk.js      SSE listener + screen renderer + demo modes
  static/vendor/morphicons.js   animated icon web component (bundled, MIT)
setup.ps1 / setup.bat   one-shot Windows installer
run.bat                 launcher with crash-restart loop
update.bat              git pull + deps
```

---

## Status / TODO

Done and verified on mock: full session flow, trigger-failure → retry →
auto-refund, OUT_OF_SERVICE + recovery, restart reconciliation, per-booth
DB/logs, paper counter + alert, heartbeat, admin pause/resume, SSE reconnect.

Needs the real thing:
- **Monobank** — live test with a real `BANK_TOKEN` (create / poll / refund / webhook signature).
- **dslrBooth** — `hotkey` trigger on the actual Windows PC + camera; measure the timings.
- **On-site** — auto-login, dslrBooth autostart, Fully Kiosk, static IP, UPS.
