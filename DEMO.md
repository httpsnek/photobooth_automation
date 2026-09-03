# Showing the UI to the client

The kiosk has a **demo mode** — every screen, no payment, no hardware.

| URL | Behaviour |
|---|---|
| `/kiosk?demo` | **tap anywhere on the screen** = next screen. Nothing visible on top. Loops. |
| `/kiosk?demo=auto` | plays through on its own, hands-off — hit record and leave it |
| `/kiosk?demo=shooting` (or `paid`, `done`, `refunded`, `out_of_service`, `awaiting_payment`, `printing`, `connecting`) | freezes on one screen |
| `/gallery` | all screens on one scrollable page (add `?ar=9/19.5` for phone proportions) |

Tour order (both `?demo` and `?demo=auto`): QR/оплата → оплату отримано →
зйомка (3·2·1 per shot) → друк → готово → помилка → не працює → back to start.
In demo the shooting phase is shortened so the recording isn't long.

---

## Making a screen recording

1. Open `…/kiosk?demo=auto` on the tablet (or any phone/laptop) — for a
   hands-off clip; or `…/kiosk?demo` if you want to control the pacing by tapping.
2. Full-screen the browser (F11 on desktop, or Fully Kiosk / add-to-home on the tablet).
3. Record:
   - **Android tablet**: built-in screen recorder (swipe-down quick settings).
   - **Mac**: ⇧⌘5.
   - **Windows**: Win+G (Game Bar) or ⊞+Alt+R.
4. For `?demo` — tap through at a calm pace, ~2–3 s per screen.

---

## Getting a URL the client can open

**Fastest — Cloudflare quick tunnel** (`cloudflared` is already installed):

```bash
uvicorn backend.app:app --port 8000            # terminal 1
cloudflared tunnel --url http://localhost:8000  # terminal 2
```

It prints `https://<random>.trycloudflare.com` — send `…/kiosk?demo` to the
client. Works while your machine stays on, no account.

**Permanent — deploy** the `Dockerfile` to Render / Railway / Fly (free tier):
Render → New → Web Service → pick the repo → it detects the Dockerfile →
env `PAYMENT_PROVIDER=mock`. Now `https://…onrender.com/kiosk?demo` is always up.

---

**Before recording:** decide the tablet **orientation** and set the browser
window / device to match — the layout adapts, but the client should see it
the way it will actually hang.
