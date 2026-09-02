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
    SHOOTING:         { icon: "camera", badge: "pink", headline: "Дивіться в камеру!", sub: "Робимо " + SHOTS + " кадри поспіль" },
    PRINTING:         { icon: "printer", badge: "pink", headline: "Друкуємо ваше фото…" },
    DONE:             { icon: "check", badge: "ok", headline: "Готово!", sub: "Забирайте фото знизу" },
    REFUNDING:        { loader: true, headline: "Повертаємо кошти…" },
    REFUNDED:         { icon: "undo", badge: "warn", headline: "Сталася помилка", sub: "Кошти повернено на картку" },
    OUT_OF_SERVICE:   { icon: "wrench", badge: "warn", headline: "Тимчасово не працює" }
  };
  var COUNTDOWN_STATES = { SHOOTING: 1, PRINTING: 1 };

  // ── Elements ──
  var brand = document.querySelector(".brand");
  var screenLayer = document.getElementById("screen");
  var badge = document.getElementById("badge");
  var badgeIcon = document.getElementById("badge-icon");
  var loader = document.getElementById("loader");
  var copy = document.getElementById("copy");
  var headlineEl = document.getElementById("headline");
  var subtextEl = document.getElementById("subtext");
  var shotsProgress = document.getElementById("shots-progress");
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
  var CLIP_POS_Y = 0.55;  // must match --clip-pos-y in style.css

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

  // ── HTML layer rendering ──
  function showHtmlLayer(cfg, st) {
    hideVideo();

    // awaiting (pricing + QR) vs badge/loader + copy
    awaiting.hidden = !cfg.awaiting;
    if (cfg.awaiting) {
      badge.hidden = true; loader.hidden = true; copy.hidden = true;
      if (lastQrDataUri) qrEl.src = lastQrDataUri;
      if (priceEl && PRICE) priceEl.textContent = PRICE;
      if (shotsPriceEl) shotsPriceEl.textContent = SHOTS;
      return;
    }

    loader.hidden = !cfg.loader;
    badge.hidden = !cfg.icon;
    copy.hidden = !cfg.headline;

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
    showHtmlLayer(cfg, st);

    if (st === "SHOOTING" && state !== "SHOOTING") shotDots(SHOTS, snap.seconds_left || 30);
    else if (st !== "SHOOTING") clearShotDots();

    if (COUNTDOWN_STATES[st]) startCountdown(snap.seconds_left);
    else stopCountdown();
    state = st;
  }

  // ── shot progress: N dots fill one-by-one across the shooting window ──
  //   Approximate (no per-shot signal without dslrBooth Pro) — the real
  //   3·2·1 per frame shows on the camera screen.
  var shotTimers = [];
  function clearShotDots() {
    shotTimers.forEach(clearTimeout);
    shotTimers = [];
    shotsProgress.hidden = true;
    shotsProgress.innerHTML = "";
  }
  function shotDots(n, seconds) {
    clearShotDots();
    shotsProgress.hidden = false;
    for (var i = 0; i < n; i++) shotsProgress.appendChild(document.createElement("i"));
    var dots = shotsProgress.children;
    var gap = (seconds * 1000) / n;
    for (var k = 0; k < n; k++) {
      (function (idx) {
        shotTimers.push(setTimeout(function () {
          if (dots[idx]) dots[idx].classList.add("on");
        }, Math.round(gap * (idx + 0.55))));
      })(k);
    }
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

  // ── Demo mode (no SSE) ──────────────────────────────────
  //   ?demo=STATE   freeze on one screen  (design / QR calibration)
  //   ?demo         tour: tap / click / → advances through every screen
  var params = new URLSearchParams(location.search);
  if (params.has("demo")) {
    var one = params.get("demo");
    var qrSrc = "/qr?text=" + encodeURIComponent("https://instabox.example/demo");

    if (one) {
      lastQrDataUri = qrSrc;
      render({ state: one.toUpperCase(), price: PRICE, shots: SHOTS, seconds_left: 12 });
      return;
    }

    // ?demo        -> auto-plays every screen on a loop
    // ?demo=click  -> tap / click / → to advance
    var manual = one === "click";
    var TOUR = ["CONNECTING", "AWAITING_PAYMENT", "PAID", "SHOOTING",
                "PRINTING", "DONE", "REFUNDING", "REFUNDED", "OUT_OF_SERVICE"];
    var HOLD = { AWAITING_PAYMENT: 4600, PAID: 6000 };  // let the videos run
    var ti = 0;
    lastQrDataUri = qrSrc;

    var hint = document.createElement("div");
    hint.style.cssText = "position:fixed;left:50%;bottom:2vh;translate:-50% 0;z-index:99;" +
      "font:600 13px/1 system-ui,sans-serif;color:#b9b9bd;background:#fff;padding:7px 14px;" +
      "border-radius:999px;box-shadow:0 2px 12px rgba(0,0,0,.12);pointer-events:none;white-space:nowrap";
    document.body.appendChild(hint);

    var timer = null;
    function step() {
      var st = TOUR[ti % TOUR.length];
      hint.textContent = (ti % TOUR.length + 1) + " / " + TOUR.length + "  ·  " + st +
        (manual ? "  ·  тап →" : "");
      render({ state: st, price: PRICE, shots: SHOTS, seconds_left: 12 });
      ti++;
      if (!manual) {
        clearTimeout(timer);
        timer = setTimeout(step, HOLD[st] || 2800);
      }
    }
    step();

    if (manual) {
      var last = 0;
      var advance = function (e) {
        if (e.type === "keydown" && e.key !== "ArrowRight" && e.key !== " " && e.key !== "Enter") return;
        if (Date.now() - last < 450) return;
        last = Date.now();
        step();
      };
      document.addEventListener("pointerup", advance);
      document.addEventListener("keydown", advance);
    }
    return;
  }

  render({ state: "CONNECTING" });
  connect();
  setTimeout(function () { location.reload(); }, 6 * 3600 * 1000);
})();
