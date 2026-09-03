# Showing the UI to the client

The kiosk has a **demo mode** that runs every screen with no payment / no
hardware:

| URL | Behaviour |
|---|---|
| `/kiosk?demo` | tap right = next screen, tap left = back |
| `/kiosk?demo=auto` | auto-plays all 9 screens on a loop |
| `/kiosk?demo=shooting` (or `paid`, `done`, `refunded`, `out_of_service`, …) | freezes on one screen |

Screens shown: connecting · **QR / оплата** · оплату отримано · дивіться в
камеру (+4 dots) · друкуємо · готово · повертаємо кошти · помилка · не працює.

---

## 1. Best fidelity — the real tablet

This is exactly what the guest sees. On the Android tablet, open Chrome /
Fully Kiosk at `http://<your-machine>:8000/kiosk?demo` (same Wi-Fi) or the
tunnel URL below. Real size, real font, real animations.

## 2. Fastest shareable link — Cloudflare quick tunnel

Run the app, then in another terminal:

```bash
# one-time: brew install cloudflared   (or download the binary)
uvicorn backend.app:app --port 8000        # terminal 1
cloudflared tunnel --url http://localhost:8000   # terminal 2
```

`cloudflared` prints an `https://<random>.trycloudflare.com` URL — send it
to the client, they open `…/kiosk?demo` on their own phone/tablet. Works
while your machine stays on. No account needed.

## 3. Permanent link — deploy the demo

There's a `Dockerfile`. Any Python/Docker host works (Render, Railway,
Fly.io — all have a free tier):

- **Render**: New → Web Service → connect the repo → it detects the
  Dockerfile → deploy. Env: `PAYMENT_PROVIDER=mock`. Done — permanent
  `https://…onrender.com/kiosk?demo` the client can revisit anytime.

(The free tier sleeps after ~15 min idle; first hit takes ~30 s to wake.)

## 4. Zero-setup — send a video

Open `/kiosk?demo=auto` on a tablet-sized window and screen-record one full
loop (~40 s). Send the clip. No interactivity, but nothing for the client
to install.

---

**Before the demo:** decide tablet **orientation** (portrait vs landscape) —
the layout adapts, but the client should see it in the orientation it will
actually hang. Set your preview window / tablet accordingly.
