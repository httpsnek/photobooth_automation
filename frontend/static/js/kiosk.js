(function () {
  "use strict";

  var body = document.body;
  var ATTRACT_VIDEO = body.dataset.attractVideo || "";
  var PAID_VIDEO = body.dataset.paidVideo || "";
  var QR_LEFT = parseFloat(body.dataset.qrLeft) || 50;
  var QR_TOP = parseFloat(body.dataset.qrTop) || 66.34;
  var QR_SIZE = parseFloat(body.dataset.qrSize) || 24;
  var QR_HEIGHT = parseFloat(body.dataset.qrHeight) || 37.5;
  var QR_RADIUS = parseFloat(body.dataset.qrRadius) || 1.6;
  var PRICE = parseFloat(body.dataset.price) || 0;
  var SHOTS = parseInt(body.dataset.shots, 10) || 4;
  var SHOT_COUNTDOWN = parseInt(body.dataset.shotCountdown, 10) || 3;
  var DEMO = new URLSearchParams(location.search).has("demo");
  // in demo, keep SHOOTING short (countdown + ~1s capture per shot)
  var SESSION_DURATION = DEMO
    ? SHOTS * (SHOT_COUNTDOWN + 1)
    : (parseFloat(body.dataset.sessionDuration) || 30);
  var SUPPORT_PHONE = (body.dataset.supportPhone || "").trim();
  var SUPPORT_TG_URL = (body.dataset.supportTgUrl || "").trim();
  var SUPPORT_TG_HANDLE = (body.dataset.supportTgHandle || "").trim();

  // ── Lucide icon data (IconNode). Morphed by <morph-icon>. ──
  var ICON = {
    camera: [["path", { d: "M13.997 4a2 2 0 0 1 1.76 1.05l.486.9A2 2 0 0 0 18.003 7H20a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h1.997a2 2 0 0 0 1.759-1.048l.489-.904A2 2 0 0 1 10.004 4z" }], ["circle", { cx: "12", cy: "13", r: "3" }]],
    printer: [["path", { d: "M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" }], ["path", { d: "M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6" }], ["rect", { x: "6", y: "14", width: "12", height: "8", rx: "1" }]],
    check: [["path", { d: "M20 6 9 17l-5-5" }]],
    undo: [["path", { d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }], ["path", { d: "M3 3v5h5" }]],
    wrench: [["path", { d: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z" }]]
  };

  // ── Per-state UI ──
  var UI = {
    CONNECTING:       { loader: true, headline: "Завантаження…" },
    AWAITING_PAYMENT: { awaiting: true },
    PAID:             { icon: "check", badge: "ok", headline: "Оплату отримано" },
    SHOOTING:         { icon: "camera", badge: "pink", headline: "Дивіться в камеру!" },
    PRINTING:         { icon: "printer", badge: "pink", headline: "Друкуємо ваше фото…" },
    DONE:             { icon: "check", badge: "ok", headline: "Готово!", sub: "Забирайте фото знизу" },
    REFUNDING:        { loader: true, headline: "Повертаємо кошти…" },
    REFUNDED:         { icon: "undo", badge: "warn", headline: "Сталася помилка", sub: "Кошти повернено на картку", support: true },
    OUT_OF_SERVICE:   { icon: "wrench", badge: "warn", headline: "Тимчасово не працює",
                        sub: "Спробуйте, будь ласка, трохи пізніше", support: true, retry: true }
  };
  var COUNTDOWN_STATES = { PRINTING: 1 };  // SHOOTING shows its own per-shot count

  // ── Elements ──
  var brand = document.querySelector(".brand");
  var screenLayer = document.getElementById("screen");
  var badge = document.getElementById("badge");
  var badgeIcon = document.getElementById("badge-icon");
  var loader = document.getElementById("loader");
  var copy = document.getElementById("copy");
  var headlineEl = document.getElementById("headline");
  var subtextEl = document.getElementById("subtext");
  var supportEl = document.getElementById("support");
  var supportQr = document.getElementById("support-qr");
  var supportQrImg = document.getElementById("support-qr-img");
  var shotsProgress = document.getElementById("shots-progress");
  var countdownNum = document.getElementById("countdown-num");
  var flashEl = document.getElementById("flash");
  var awaiting = document.getElementById("awaiting");
  var timerEl = document.getElementById("timer");
  var qrEl = document.getElementById("qr");
  var priceEl = document.getElementById("price-value");
  var shotsPriceEl = document.getElementById("shots-price");

  var stage = document.getElementById("video-stage");
  var videoBox = document.getElementById("video-box");
  var clip = document.getElementById("clip");
  var qrOverlay = document.getElementById("qr-overlay");
  var qrOverlayImg = document.getElementById("qr-overlay-img");

  if (window.Morphicons && Morphicons.defineMorphIcon) Morphicons.defineMorphIcon();
  var morphOK = !!(window.customElements && customElements.get("morph-icon"));

  var state = null;
  var lastIconKey = null;
  var countdownHandle = null;
  var videoBroken = false;
  var wantVideo = false;
  var lastQrDataUri = "";
  var lastSnap = null;

  // ── countdown ──
  function stopCountdown() {
    if (countdownHandle) { clearInterval(countdownHandle); countdownHandle = null; }
    timerEl.hidden = true;
  }
  function startCountdown(seconds) {
    stopCountdown();
    if (seconds == null || seconds <= 0) return;
    var left = Math.round(seconds);
    timerEl.hidden = false;
    timerEl.textContent = left + " с";
    countdownHandle = setInterval(function () {
      left -= 1;
      if (left <= 0) { stopCountdown(); return; }
      timerEl.textContent = left + " с";
    }, 1000);
  }

  // ── video ──
  var CLIP_POS_Y = 0.42;  // must match --clip-pos-y in style.css

  function layoutOverlay() {
    if (!clip.videoWidth || !clip.videoHeight) return;
    var box = videoBox.getBoundingClientRect();
    var scale = Math.min(box.width / clip.videoWidth, box.height / clip.videoHeight);
    var pw = clip.videoWidth * scale, ph = clip.videoHeight * scale;
    var offX = (box.width - pw) / 2, offY = (box.height - ph) * CLIP_POS_Y;
    qrOverlay.style.width = (pw * (QR_SIZE / 100)) + "px";
    qrOverlay.style.height = (ph * (QR_HEIGHT / 100)) + "px";
    qrOverlay.style.left = (offX + pw * (QR_LEFT / 100)) + "px";
    qrOverlay.style.top = (offY + ph * (QR_TOP / 100)) + "px";
    qrOverlay.style.borderRadius = (pw * (QR_RADIUS / 100)) + "px";
  }
  function playVideo(file, loop) {
    wantVideo = true;
    var src = "/static/media/" + file;
    if (clip.dataset.src !== src) { clip.dataset.src = src; clip.src = src; clip.load(); }
    clip.loop = !!loop;
    stage.hidden = false;
    screenLayer.style.display = "none";  // logo header stays — one fixed position
    var p = clip.play();
    if (p && p.catch) p.catch(function () {});
  }
  function hideVideo() {
    wantVideo = false;
    if (!stage.hidden) { stage.hidden = true; try { clip.pause(); } catch (e) {} }
    screenLayer.style.display = "";
  }
  clip.addEventListener("loadedmetadata", layoutOverlay);
  clip.addEventListener("error", function () {
    videoBroken = true; hideVideo(); if (lastSnap) render(lastSnap);
  });
  window.addEventListener("resize", layoutOverlay);

  function stalledMidway() {
    return wantVideo && clip.paused && !clip.ended && !clip.loop &&
           clip.currentTime < (clip.duration || Infinity) - 0.05;
  }
  setInterval(function () {
    if (stalledMidway() || (wantVideo && clip.loop && clip.paused)) {
      var p = clip.play(); if (p && p.catch) p.catch(function () {});
    }
  }, 3000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && (stalledMidway() || (wantVideo && clip.loop && clip.paused))) {
      clip.play().catch(function () {});
    }
  });

  // restart the CSS entrance animation on a container's children
  function replayIntro(container) {
    var kids = [].slice.call(container.children);
    kids.forEach(function (el) { el.style.opacity = ""; el.style.transform = ""; el.style.animation = "none"; });
    void container.offsetWidth;  // force reflow
    kids.forEach(function (el) { el.style.animation = ""; });
  }

  // gentle fade-up on an element when its content changes; removing the class
  // afterwards is also the safety net (element falls back to its visible base).
  function playEnter(el) {
    el.classList.remove("enter");
    void el.offsetWidth;
    el.classList.add("enter");
    setTimeout(function () { el.classList.remove("enter"); }, 500);
  }

  var lastHtmlState = null;

  // ── HTML layer rendering ──
  function showHtmlLayer(cfg, st) {
    hideVideo();
    var changed = st !== lastHtmlState;
    lastHtmlState = st;

    // awaiting (pricing + QR) vs badge/loader + copy
    var enteringAwaiting = cfg.awaiting && awaiting.hidden;
    awaiting.hidden = !cfg.awaiting;
    if (cfg.awaiting) {
      badge.hidden = true; loader.hidden = true; copy.hidden = true;
      if (lastQrDataUri) qrEl.src = lastQrDataUri;
      if (priceEl && PRICE) priceEl.textContent = PRICE;
      if (shotsPriceEl) shotsPriceEl.textContent = SHOTS;
      if (enteringAwaiting) replayIntro(awaiting);
      // safety: whatever the entrance animation does, elements must end visible
      setTimeout(function () {
        [].forEach.call(awaiting.children, function (el) {
          el.style.opacity = "1"; el.style.transform = "none";
        });
      }, 900);
      return;
    }

    var loaderWasHidden = loader.hidden, badgeWasHidden = badge.hidden;
    loader.hidden = !cfg.loader;
    badge.hidden = !cfg.icon;
    copy.hidden = !cfg.headline;

    if (changed && cfg.loader && loaderWasHidden) playEnter(loader);
    if (changed && cfg.icon && badgeWasHidden) playEnter(badge);
    if (changed && cfg.headline) playEnter(copy);

    if (supportEl) {
      if (cfg.support && SUPPORT_TG_URL) {
        // stranded guest can't tap text on a kiosk — give them a QR to scan
        supportEl.textContent = "Питання? Наведіть камеру на код"
          + (SUPPORT_TG_HANDLE ? "  ·  " + SUPPORT_TG_HANDLE : "");
        supportEl.hidden = false;
        if (supportQrImg && !supportQrImg.getAttribute("src")) {
          supportQrImg.src = "/qr?text=" + encodeURIComponent(SUPPORT_TG_URL);
        }
        if (supportQr) supportQr.hidden = false;
      } else if (cfg.support && SUPPORT_PHONE) {
        supportEl.textContent = (cfg.retry ? "Або зателефонуйте: " : "Підтримка: ") + SUPPORT_PHONE;
        supportEl.hidden = false;
        if (supportQr) supportQr.hidden = true;
      } else {
        supportEl.hidden = true;
        if (supportQr) supportQr.hidden = true;
      }
    }

    if (cfg.headline) {
      headlineEl.textContent = cfg.headline;
      if (cfg.sub) { subtextEl.textContent = cfg.sub; subtextEl.hidden = false; }
      else subtextEl.hidden = true;
    }

    if (cfg.icon) {
      badge.className = "badge badge--" + (cfg.badge || "pink");
      var node = ICON[cfg.icon];
      if (morphOK && badgeIcon) {
        badgeIcon.style.color = "";       // inherits from .badge
        if (lastIconKey && lastIconKey !== cfg.icon && st !== "CONNECTING") {
          badgeIcon.morphTo(node);
        } else {
          badgeIcon.set(node);
        }
      }
      lastIconKey = cfg.icon;
    }
  }

  // ── render ──
  function render(snap) {
    if (!snap || !snap.state) return;
    lastSnap = snap;
    if (snap.qr_data_uri) lastQrDataUri = snap.qr_data_uri;
    var st = snap.state;
    var cfg = UI[st] || UI.CONNECTING;

    if (st === "AWAITING_PAYMENT" && ATTRACT_VIDEO && !videoBroken) {
      playVideo(ATTRACT_VIDEO, false);
      if (lastQrDataUri) { qrOverlayImg.src = lastQrDataUri; qrOverlay.hidden = false; }
      layoutOverlay();
      stopCountdown();
      state = st;
      return;
    }
    if (st === "PAID" && PAID_VIDEO && !videoBroken) {
      qrOverlay.hidden = true;
      playVideo(PAID_VIDEO, false);
      stopCountdown();
      state = st;
      return;
    }

    qrOverlay.hidden = true;

    if (st === "SHOOTING") {
      renderShoot(snap);
    } else {
      if (state === "SHOOTING") leaveShoot();
      showHtmlLayer(cfg, st);
    }

    if (COUNTDOWN_STATES[st]) startCountdown(snap.seconds_left);
    else stopCountdown();
    state = st;
  }

  // ── SHOOTING sub-phases — driven by the server snapshot ─────────────
  //   snap.shoot_phase : get_ready | counting | capture | processing
  //   snap.shot (1-based) · snap.shots · snap.countdown_ms_left
  //   With dslrBooth Pro "Trigger" events these track the real camera; without
  //   them the backend still emits the same phases on an estimated schedule.
  var cdTimer = null;
  var shootReady = false;

  function clearCd() { if (cdTimer) { clearInterval(cdTimer); cdTimer = null; } }

  function pulseNum() {
    countdownNum.classList.remove("pulse");
    void countdownNum.offsetWidth;
    countdownNum.classList.add("pulse");
  }
  function flash() {
    flashEl.classList.remove("go");
    void flashEl.offsetWidth;
    flashEl.classList.add("go");
  }

  function setDots(total, done) {
    if (shotsProgress.childElementCount !== total) {
      shotsProgress.innerHTML = "";
      for (var i = 0; i < total; i++) shotsProgress.appendChild(document.createElement("i"));
    }
    var kids = shotsProgress.children;
    for (var k = 0; k < kids.length; k++) kids[k].classList.toggle("on", k < done);
    shotsProgress.hidden = total <= 0;
  }

  // true  -> big number takes the badge (solid pink)
  // false -> the morphed icon shows
  function showNumber(on) {
    badge.classList.toggle("counting", on);
    if (!on) { clearCd(); countdownNum.textContent = ""; }
  }

  // count 3·2·1 down locally from the ms the server reported; each new
  // "counting" snapshot (next shot) resyncs it.
  function localCountdown(msLeft) {
    clearCd();
    if (msLeft == null) { countdownNum.textContent = ""; return; }
    var end = Date.now() + msLeft;
    var paint = function () {
      var rem = end - Date.now();
      var n = rem > 0 ? Math.ceil(rem / 1000) : 0;
      var txt = n > 0 ? String(n) : "";
      if (txt !== countdownNum.textContent) {
        countdownNum.textContent = txt;
        if (n > 0) pulseNum();
      }
      if (rem <= -250) clearCd();
    };
    paint();
    cdTimer = setInterval(paint, 100);
  }

  function renderShoot(snap) {
    if (state !== "SHOOTING" || !shootReady) {
      showHtmlLayer(UI.SHOOTING, "SHOOTING");   // morph check -> camera, set headline
      countdownNum.hidden = false;
      shootReady = true;
    }
    var total = snap.shots || SHOTS;
    var shot = snap.shot || 0;
    var phase = snap.shoot_phase || "get_ready";

    if (phase === "get_ready") {
      showNumber(false);
      headlineEl.textContent = "Дивіться в камеру!";
      subtextEl.hidden = true;
      setDots(total, 0);
    } else if (phase === "counting") {
      headlineEl.textContent = "Дивіться в камеру!";
      subtextEl.textContent = "Кадр " + Math.max(1, shot) + " з " + total;
      subtextEl.hidden = false;
      setDots(total, Math.max(0, shot - 1));
      showNumber(true);
      localCountdown(typeof snap.countdown_ms_left === "number" ? snap.countdown_ms_left : null);
    } else if (phase === "capture") {
      badge.classList.add("counting");   // badge stays solid pink through the burst
      clearCd();
      countdownNum.textContent = "";
      flash();
      setDots(total, shot);
    } else if (phase === "processing") {
      showNumber(false);
      headlineEl.textContent = "Майже готово…";
      subtextEl.hidden = true;
      setDots(total, total);
    }
  }

  function leaveShoot() {
    clearCd();
    shootReady = false;
    badge.classList.remove("counting");
    countdownNum.hidden = true;
    countdownNum.textContent = "";
    shotsProgress.hidden = true;
    shotsProgress.innerHTML = "";
  }

  // ── SSE ──
  var es = null, reloadTimer = null;
  function scheduleReload() {
    if (!reloadTimer) reloadTimer = setTimeout(function () { location.reload(); }, 30000);
  }
  function cancelReload() {
    if (reloadTimer) { clearTimeout(reloadTimer); reloadTimer = null; }
  }
  function connect() {
    if (es) es.close();
    es = new EventSource("/events");
    es.onopen = cancelReload;
    es.onmessage = function (e) {
      cancelReload();
      try { render(JSON.parse(e.data)); } catch (err) {}
    };
    es.onerror = function () {
      hideVideo();
      render({ state: "CONNECTING" });
      scheduleReload();
    };
  }

  // ── Demo mode (no SSE) ── for client demos / screen recordings ──
  //   ?demo         tap anywhere on the screen = next screen (loops)
  //   ?demo=auto    plays through on its own, hands-off
  //   ?demo=STATE   freeze on one screen (e.g. ?demo=shooting)
  //   The SHOOTING step plays its real sub-phases (get-ready → 3·2·1 ×N →
  //   processing); every other screen is its own independent block.
  var params = new URLSearchParams(location.search);
  if (params.has("demo")) {
    var one = params.get("demo");
    lastQrDataUri = "/qr?text=" + encodeURIComponent("https://instabox.example/demo");
    var demoShootTimer = null;

    function demoShootFrames() {
      var n = SHOTS, cd = SHOT_COUNTDOWN * 1000;
      var f = [{ shoot_phase: "get_ready", shot: 0, hold: 1800 }];
      for (var i = 1; i <= n; i++) {
        f.push({ shoot_phase: "counting", shot: i, countdown_ms_left: cd, hold: cd });
        f.push({ shoot_phase: "capture", shot: i, hold: 460 });
      }
      f.push({ shoot_phase: "processing", shot: n, hold: 1100 });
      return f;
    }
    function demoShoot(onDone) {
      var frames = demoShootFrames(), i = 0;
      (function step() {
        if (i >= frames.length) { if (onDone) onDone(); return; }
        var fr = frames[i++];
        render({
          state: "SHOOTING", price: PRICE, shots: SHOTS,
          shoot_phase: fr.shoot_phase, shot: fr.shot,
          countdown_ms_left: fr.countdown_ms_left,
        });
        demoShootTimer = setTimeout(step, fr.hold);
      })();
    }

    if (one && one !== "auto") {
      var S = one.toUpperCase();
      if (S === "SHOOTING") { (function loop() { demoShoot(loop); })(); return; }
      render({ state: S, price: PRICE, shots: SHOTS, seconds_left: 12 });
      return;
    }

    // customer journey first, error screens last
    var TOUR = ["AWAITING_PAYMENT", "PAID", "SHOOTING", "PRINTING", "DONE",
                "REFUNDED", "OUT_OF_SERVICE"];
    var HOLD_MS = {
      AWAITING_PAYMENT: 4000, PAID: 3000,
      PRINTING: 3600, DONE: 3600, REFUNDED: 4000, OUT_OF_SERVICE: 4000,
    };
    var auto = one === "auto";
    var ti = 0, autoTimer = null, lastTap = 0;

    function show() {
      clearTimeout(autoTimer);
      clearTimeout(demoShootTimer);
      var st = TOUR[((ti % TOUR.length) + TOUR.length) % TOUR.length];
      document.getElementById("timer").hidden = true;

      if (st === "SHOOTING") {
        demoShoot(auto ? function () { ti++; show(); } : null);
        return;
      }
      render({ state: st, price: PRICE, shots: SHOTS, seconds_left: 12 });
      if (auto) {
        autoTimer = setTimeout(function () { ti++; show(); }, HOLD_MS[st] || 3000);
      }
    }
    show();

    if (!auto) {
      var advance = function () {
        var now = Date.now();
        if (now - lastTap < 300) return;   // debounce a stray double-fire
        lastTap = now;
        ti++;
        show();
      };
      // tap / click anywhere; arrow keys optional
      window.addEventListener("pointerdown", advance);
      window.addEventListener("keydown", function (e) {
        if (e.key === "ArrowLeft") { ti--; show(); }
        else if (e.key !== "F5") advance();
      });
    }
    return;
  }

  render({ state: "CONNECTING" });
  connect();
  setTimeout(function () { location.reload(); }, 6 * 3600 * 1000);
})();
