"""External watchdog (watchdog.py) — decision logic against a fake /health."""
import http.server
import importlib.util
import json
import pathlib
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_wd():
    spec = importlib.util.spec_from_file_location("wd", ROOT / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    return wd


class _Health:
    def __init__(self):
        self.state = "AWAITING_PAYMENT"
        self.sid = "s1"
        self.alive = True
        h = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if not h.alive:
                    self.send_error(503)
                    return
                body = json.dumps({"state": h.state, "session_id": h.sid}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def wd_env(monkeypatch):
    fake = _Health()
    wd = _load_wd()
    wd.URL = f"http://127.0.0.1:{fake.port}/health"
    wd.INTERVAL = 0.3
    wd.HTTP_TIMEOUT = 1
    wd.FAIL_LIMIT = 2
    wd.STUCK_SEC = 1.5
    wd.COOLDOWN_SEC = 0.3
    wd.REBOOT_AFTER = 0
    wd.notify = lambda *a, **k: None
    wd.single_instance = lambda: True
    kills = []
    wd.kill_backend = lambda reason: kills.append(reason)
    threading.Thread(target=wd.main, daemon=True).start()
    yield fake, kills
    fake.stop()


def test_healthy_rotation_no_kill(wd_env):
    fake, kills = wd_env
    for sid in ("s2", "s3", "s4"):
        time.sleep(0.8)
        fake.sid = sid
    time.sleep(0.8)
    assert kills == []


def test_frozen_work_state_is_killed(wd_env):
    fake, kills = wd_env
    fake.state, fake.sid = "PRINTING", "frozen"
    time.sleep(3)
    assert kills and "PRINTING" in kills[0]


def test_out_of_service_hold_never_killed(wd_env):
    fake, kills = wd_env
    fake.state, fake.sid = "OUT_OF_SERVICE", "oos"
    time.sleep(3)
    assert kills == []


def test_unreachable_twice_is_killed(wd_env):
    fake, kills = wd_env
    fake.alive = False
    time.sleep(2)
    assert kills and "відповіда" in kills[0]
