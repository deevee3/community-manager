"""Offline Bot HQ bridge test — stdlib only, localhost stub server.

Spins up a stub http.server on an ephemeral port that records every
request (method, path, X-Bot-Key, JSON body) and answers the Bot HQ
contract, then checks the real ``bot.bridge.Bridge`` client against it:

  1. Heartbeat shape + capabilities.
  2. Approval POST shape (kind "flag", payload_json carries flag_id).
  3. poll_decision follows pending -> approved.
  4. log_event / notify shape.
  5. fetch_inbox parsing + reply shape.
  6. Disabled bridge: zero HTTP calls, None/False/[] results.
  7. Closed-port bridge: every method fails silent.

Run:  python3 tests/bridge_test.py     (prints PASS/FAIL per check)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.bridge import Bridge  # noqa: E402
from bot.config import AppConfig  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
REQUESTS: list[dict] = []
LOCK = threading.Lock()
APPROVAL_GETS = {"n": 0}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def recorded(method: str, path_prefix: str) -> list[dict]:
    with LOCK:
        return [
            r for r in REQUESTS
            if r["method"] == method and urlparse(r["path"]).path == path_prefix
        ]


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence server logs
        pass

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = None
        with LOCK:
            REQUESTS.append({
                "method": method,
                "path": self.path,
                "key": self.headers.get("X-Bot-Key"),
                "body": body,
            })
        path = urlparse(self.path).path
        payload: object = {}
        status = 200
        if method == "POST" and path == "/api/heartbeat":
            payload = {}
        elif method == "POST" and path == "/api/approvals":
            payload = {"id": 42}
        elif method == "GET" and path == "/api/approvals/42":
            with LOCK:
                APPROVAL_GETS["n"] += 1
                n = APPROVAL_GETS["n"]
            payload = {"status": "pending"} if n == 1 else {"status": "approved"}
        elif method == "POST" and path == "/api/events":
            payload = {}
        elif method == "GET" and path == "/api/messages/inbox":
            payload = [{"id": 7, "body": "/stats",
                        "created_at": "2026-10-20T00:00:00Z"}]
        elif method == "POST" and path == "/api/messages":
            payload = {}
        else:
            status = 404
            payload = {"error": "not found"}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    key = "test-bot-key"
    try:
        bridge = Bridge(url=base, key=key, transport="both",
                        capabilities=["chat"], timeout=2.0)
        check("Bridge enabled with URL+key", bridge.enabled)
        check("Bridge use_web + use_telegram under both",
              bridge.use_web and bridge.use_telegram)

        # ------------------------------------------------------ heartbeat
        check("Heartbeat returns True", bridge.heartbeat("online", "up") is True)
        hits = recorded("POST", "/api/heartbeat")
        body = hits[-1]["body"] if hits else {}
        check(
            "Heartbeat shape + capabilities",
            bool(hits) and hits[-1]["key"] == key
            and body.get("bot_id") == "community-manager"
            and body.get("name") == "Community Manager"
            and body.get("status") == "online"
            and body.get("detail") == "up"
            and body.get("capabilities") == ["chat"],
            json.dumps(body),
        )

        # ------------------------------------------------------ approvals
        approval_id = bridge.request_approval(
            "flag", "Flag #123 — scam", "card body", {"flag_id": 123}
        )
        check("request_approval returns stub id 42", approval_id == 42,
              f"id={approval_id}")
        hits = recorded("POST", "/api/approvals")
        body = hits[-1]["body"] if hits else {}
        payload = {}
        try:
            payload = json.loads(body.get("payload_json", ""))
        except ValueError:
            pass
        check(
            "Approval POST shape (kind flag, payload_json has flag_id)",
            bool(hits) and hits[-1]["key"] == key
            and body.get("bot_id") == "community-manager"
            and body.get("kind") == "flag"
            and payload.get("flag_id") == 123,
            json.dumps(body),
        )

        decision = bridge.poll_decision(42, timeout_s=5.0, interval_s=0.05)
        check(
            "poll_decision follows pending -> approved",
            decision is not None and decision.get("status") == "approved"
            and APPROVAL_GETS["n"] >= 2,
            f"decision={decision} gets={APPROVAL_GETS['n']}",
        )

        # ------------------------------------------------------ events
        check("log_event returns True",
              bridge.log_event("flag_raised", "Flag #123 raised", "scam") is True)
        hits = recorded("POST", "/api/events")
        body = hits[-1]["body"] if hits else {}
        check(
            "log_event shape",
            bool(hits) and body.get("bot_id") == "community-manager"
            and body.get("kind") == "flag_raised"
            and body.get("title") == "Flag #123 raised"
            and body.get("detail") == "scam",
            json.dumps(body),
        )
        check("notify returns True", bridge.notify("Heads up", "detail") is True)
        hits = recorded("POST", "/api/events")
        body = hits[-1]["body"] if hits else {}
        check("notify is an alert event", body.get("kind") == "alert",
              json.dumps(body))

        # ------------------------------------------------------ chat
        inbox = bridge.fetch_inbox()
        check(
            "fetch_inbox parsing",
            inbox == [{"id": 7, "body": "/stats",
                       "created_at": "2026-10-20T00:00:00Z"}],
            f"inbox={inbox}",
        )
        check("reply returns True", bridge.reply("stats text", 7) is True)
        hits = recorded("POST", "/api/messages")
        body = hits[-1]["body"] if hits else {}
        check(
            "reply shape",
            bool(hits) and body.get("bot_id") == "community-manager"
            and body.get("body") == "stats text"
            and body.get("in_reply_to") == 7,
            json.dumps(body),
        )

        # ------------------------------------------------------ config glue
        cfg_bridge = AppConfig(
            bot_hq_url=base, bot_hq_key=key, transport="both"
        ).make_bridge()
        check(
            "AppConfig.make_bridge wires url/key/transport/capabilities",
            cfg_bridge.enabled and cfg_bridge.use_web and cfg_bridge.use_telegram
            and cfg_bridge.capabilities == ["chat"],
        )
    finally:
        server.shutdown()
        server.server_close()

    # ---------------------------------------------------------- disabled
    with LOCK:
        before = len(REQUESTS)
    off = Bridge(url="", key="", transport="both", capabilities=["chat"])
    results = (
        off.enabled, off.use_web,
        off.heartbeat("online", "x"), off.request_approval("flag", "t", "b"),
        off.get_decision(42), off.log_event("k", "t"), off.notify("t"),
        off.fetch_inbox(), off.reply("hi", 1),
    )
    with LOCK:
        after = len(REQUESTS)
    check(
        "Disabled bridge: zero HTTP calls + None/False/[]",
        results == (False, False, False, None, None, False, False, [], False)
        and after == before,
        f"results={results} calls {before}->{after}",
    )
    check("Disabled bridge still allows telegram transport",
          Bridge(url="", key="", transport="telegram").use_telegram is True)

    # ---------------------------------------------------------- closed port
    dead = Bridge(url=f"http://127.0.0.1:{_closed_port()}", key=key,
                  transport="web", timeout=0.5, capabilities=["chat"])
    silent = True
    try:
        silent = (
            dead.heartbeat("online", "x") is False
            and dead.request_approval("flag", "t", "b") is None
            and dead.get_decision(42) is None
            and dead.log_event("k", "t") is False
            and dead.fetch_inbox() == []
            and dead.reply("hi", 1) is False
        )
    except Exception as exc:  # noqa: BLE001 - must never raise
        silent = False
        check("Closed-port bridge fails silent", False, repr(exc))
    if silent:
        check("Closed-port bridge fails silent", True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("BRIDGE TEST: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
