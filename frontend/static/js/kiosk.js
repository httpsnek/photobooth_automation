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

  var screens = {};
  document.querySelectorAll(".screen").forEach(function (el) {
    screens[el.dataset.state] = el;
  });

  var brand = document.querySelector(".brand");
  var screenLayer = document.getElementById("screen");
  var timerEl = document.getElementById("timer");
  var qrEl = document.getElementById("qr");
  var priceEl = document.getElementById("price-value");
  var shotsEl = document.getElementById("shots-count");

  var stage = document.getElementById("video-stage");
  var videoBox = document.getElementById("video-box");
  var clip = document.getElementById("clip");
  var qrOverlay = document.getElementById("qr-overlay");
  var qrOverlayImg = document.getElementById("qr-overlay-img");

  var COUNTDOWN_STATES = ["SHOOTING", "PRINTING", "DONE", "REFUNDED"];
  var current = null;
  var countdownHandle = null;
  var videoBroken = false;      // set if the browser can't play the clip
  var wantVideo = false;        // should the video layer be visible right now
  var lastQrDataUri = "";

  // ── HTML screen switcher ──────────────────────────────────
  function showScreen(state) {
    var el = screens[state] || screens.CONNECTING;
    if (current !== el) {
      for (var k in screens) screens[k].classList.remove("active");
      el.classList.add("active");
      current = el;
    }
  }

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

  // ── Video layer ───────────────────────────────────────────
  function layoutOverlay() {
    if (!clip.videoWidth || !clip.videoHeight) return;
    var box = videoBox.getBoundingClientRect();
    var scale = Math.min(box.width / clip.videoWidth, box.height / clip.videoHeight);
    var pw = clip.videoWidth * scale, ph = clip.videoHeight * scale;
    var offX = (box.width - pw) / 2, offY = (box.height - ph) / 2;
    qrOverlay.style.width = (pw * (QR_SIZE / 100)) + "px";
    qrOverlay.style.height = (ph * (QR_HEIGHT / 100)) + "px";
    qrOverlay.style.left = (offX + pw * (QR_LEFT / 100)) + "px";
    qrOverlay.style.top = (offY + ph * (QR_TOP / 100)) + "px";
    qrOverlay.style.borderRadius = (pw * (QR_RADIUS / 100)) + "px";
  }

  function playVideo(file, loop) {
    wantVideo = true;
    var src = "/static/media/" + file;
    if (clip.dataset.src !== src) {
      clip.dataset.src = src;
      clip.src = src;
      clip.load();
    }
    clip.loop = !!loop;
    stage.hidden = false;
    brand.style.display = "none";
    screenLayer.style.display = "none";
    var p = clip.play();
    if (p && p.catch) p.catch(function () { /* retried by watchdog */ });
  }

  function hideVideo() {
    wantVideo = false;
    if (!stage.hidden) {
      stage.hidden = true;
      try { clip.pause(); } catch (e) {}
    }
    brand.style.display = "";
    screenLayer.style.display = "";
  }

  clip.addEventListener("loadedmetadata", layoutOverlay);
  clip.addEventListener("error", function () {
    videoBroken = true;
    hideVideo();
    // re-render current state on the HTML layer
    if (lastSnap) render(lastSnap);
  });
  window.addEventListener("resize", layoutOverlay);

  // A non-looping clip is meant to freeze on its last frame — don't let the
  // watchdog restart it. Only nudge a clip that stalled mid-playback.
  function stalledMidway() {
    return wantVideo && clip.paused && !clip.ended && !clip.loop &&
           clip.currentTime < (clip.duration || Infinity) - 0.05;
  }
  setInterval(function () {
    if (stalledMidway() || (wantVideo && clip.loop && clip.paused)) {
      var p = clip.play();
      if (p && p.catch) p.catch(function () {});
    }
  }, 3000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && (stalledMidway() || (wantVideo && clip.loop && clip.paused))) {
      clip.play().catch(function () {});
    }
  });

  // ── Render ────────────────────────────────────────────────
  var lastSnap = null;

  function render(snap) {
    if (!snap || !snap.state) return;
    lastSnap = snap;

    if (priceEl && snap.price != null) priceEl.textContent = snap.price;
    if (shotsEl && snap.shots != null) shotsEl.textContent = snap.shots;
    if (snap.qr_data_uri) lastQrDataUri = snap.qr_data_uri;

    var st = snap.state;

    // AWAITING_PAYMENT — video loop + QR overlay, or HTML fallback
    if (st === "AWAITING_PAYMENT") {
      stopCountdown();
      if (ATTRACT_VIDEO && !videoBroken) {
        // play the entrance once, then hold on the last (static) frame
        playVideo(ATTRACT_VIDEO, false);
        if (lastQrDataUri) {
          qrOverlayImg.src = lastQrDataUri;
          qrOverlay.hidden = false;
        }
        layoutOverlay();
      } else {
        hideVideo();
        if (lastQrDataUri) qrEl.src = lastQrDataUri;
        showScreen(st);
      }
      return;
    }

    // PAID — play the "payment received" clip once, or HTML fallback
    if (st === "PAID") {
      stopCountdown();
      qrOverlay.hidden = true;
      if (PAID_VIDEO && !videoBroken) {
        playVideo(PAID_VIDEO, false);
      } else {
        hideVideo();
        showScreen(st);
      }
      return;
    }

    // Everything else is HTML only
    qrOverlay.hidden = true;
    hideVideo();
    showScreen(st);
    if (COUNTDOWN_STATES.indexOf(st) >= 0) startCountdown(snap.seconds_left);
    else stopCountdown();
  }

  // ── SSE ───────────────────────────────────────────────────
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
      try { render(JSON.parse(e.data)); } catch (err) { /* keepalive ping */ }
    };
    es.onerror = function () {
      hideVideo();
      showScreen("CONNECTING");
      scheduleReload();
    };
  }

  showScreen("CONNECTING");
  connect();

  setTimeout(function () { location.reload(); }, 6 * 3600 * 1000);
})();
