"""Bot HQ web bridge — stdlib-only client for the Bot HQ dashboard API.

Contract (Bot HQ, bot-key auth via the ``X-Bot-Key`` header):
  POST /api/heartbeat   {bot_id, name, status, detail, capabilities?}
  POST /api/approvals   {bot_id, kind, title, body, payload_json} -> {id}
  GET  /api/approvals/<id> -> {status: pending|approved|skipped|edited,
                               edited_body}
  POST /api/events      {bot_id, kind, title, detail}
  GET  /api/messages/inbox?bot_id=<id> -> [{id, body, created_at}]
       (messages are marked delivered on fetch)
  POST /api/messages    {bot_id, body, in_reply_to?}

Design rules:
  * The bridge is ENABLED only when both BOT_HQ_URL and BOT_HQ_KEY are set.
    When disabled, every method is a quiet no-op returning None/False/[].
  * Every method is FAIL-SILENT: network errors are logged and swallowed —
    Bot HQ being down must never crash or block the bot for long.
  * TRANSPORT (telegram|web|both) decides where approvals/prompts flow;
    heartbeats, events, and chat work whenever the bridge is enabled.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

BOT_ID = "community-manager"
BOT_NAME = "Community Manager"

TRANSPORTS = ("telegram", "web", "both")


def normalize_transport(value: str | None) -> str:
    """Validate a TRANSPORT env value; fall back to 'telegram'."""
    t = (value or "telegram").strip().lower()
    if t not in TRANSPORTS:
        log.warning("TRANSPORT=%r not one of %s; using 'telegram'",
                    value, "/".join(TRANSPORTS))
        return "telegram"
    return t


class Bridge:
    """One bot's connection to Bot HQ. Stateless; safe to construct often."""

    def __init__(self, bot_id: str = BOT_ID, name: str = BOT_NAME, *,
                 url: str = "", key: str = "", transport: str = "telegram",
                 timeout: float = 5.0,
                 capabilities: list[str] | None = None) -> None:
        self.bot_id = bot_id
        self.name = name
        self.url = (url or "").strip().rstrip("/")
        self.key = (key or "").strip()
        self.transport = normalize_transport(transport)
        self.timeout = timeout
        self.capabilities = list(capabilities or [])

    # ------------------------------------------------------------ state

    @property
    def enabled(self) -> bool:
        """True only when both BOT_HQ_URL and BOT_HQ_KEY are set."""
        return bool(self.url and self.key)

    @property
    def use_web(self) -> bool:
        """Approvals/prompts should flow to the web dashboard."""
        return self.enabled and self.transport in ("web", "both")

    @property
    def use_telegram(self) -> bool:
        """Approvals/prompts should flow to Telegram."""
        return self.transport in ("telegram", "both")

    # ------------------------------------------------------------ http

    def _request(self, method: str, path: str,
                 payload: dict | None = None) -> dict | None:
        """One HTTP call to Bot HQ. Returns parsed JSON dict or None.

        Never raises: any error (DNS, refused, timeout, bad JSON, HTTP
        error status) is logged and reported as None.
        """
        if not self.enabled:
            return None
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("X-Bot-Key", self.key)
        req.add_header("User-Agent", "GlenrideBotHQ/1.0 (+https://bots.dvaughnhouse.com)")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception as exc:  # noqa: BLE001 - fail-silent by design
            log.warning("Bot HQ %s %s failed: %s", method, path, exc)
            return None

    # ------------------------------------------------------------ api

    def heartbeat(self, status: str = "online", detail: str = "",
                  capabilities: list[str] | None = None) -> bool:
        """POST /api/heartbeat. ``capabilities`` defaults to the bridge's
        configured list (e.g. ["chat"] once the chat loop is wired)."""
        caps = self.capabilities if capabilities is None else capabilities
        payload: dict = {
            "bot_id": self.bot_id,
            "name": self.name,
            "status": status,
            "detail": detail,
        }
        if caps:
            payload["capabilities"] = list(caps)
        return self._request("POST", "/api/heartbeat", payload) is not None

    def request_approval(self, kind: str, title: str, body: str,
                         payload: dict | None = None) -> int | None:
        """POST /api/approvals. Returns the Bot HQ approval id, or None."""
        resp = self._request("POST", "/api/approvals", {
            "bot_id": self.bot_id,
            "kind": kind,
            "title": title,
            "body": body,
            "payload_json": json.dumps(payload or {}),
        })
        if resp is None:
            return None
        try:
            return int(resp.get("id"))
        except (TypeError, ValueError):
            log.warning("Bot HQ approvals response had no usable id: %r", resp)
            return None

    def get_decision(self, approval_id: int) -> dict | None:
        """GET /api/approvals/<id> -> {status, edited_body} or None."""
        resp = self._request("GET", f"/api/approvals/{int(approval_id)}")
        if resp is None:
            return None
        return {
            "status": str(resp.get("status", "pending")),
            "edited_body": resp.get("edited_body") or "",
        }

    def poll_decision(self, approval_id: int, *, timeout_s: float = 3600.0,
                      interval_s: float = 10.0) -> dict | None:
        """Block until the approval leaves 'pending' (or timeout).

        Returns the final decision dict ({status, edited_body}), or None
        on timeout. Transient network failures are retried until the
        deadline — a Bot HQ outage mid-wait does not lose the approval.
        Intended to run in a worker thread / executor, never on an event
        loop directly.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            decision = self.get_decision(approval_id)
            if decision is not None and decision["status"] != "pending":
                return decision
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(interval_s,
                           max(0.0, deadline - time.monotonic())))

    def log_event(self, kind: str, title: str, detail: str = "") -> bool:
        """POST /api/events — the bot's history feed on the dashboard."""
        return self._request("POST", "/api/events", {
            "bot_id": self.bot_id,
            "kind": kind,
            "title": title,
            "detail": detail,
        }) is not None

    def notify(self, title: str, detail: str = "") -> bool:
        """Convenience: an alert event (kind='alert'), no approval needed."""
        return self.log_event("alert", title, detail)

    # ------------------------------------------------------------ chat

    def fetch_inbox(self) -> list[dict]:
        """GET /api/messages/inbox?bot_id=<id> — undelivered dashboard
        messages for this bot. Fetching marks them delivered, so anything
        returned here must be handled (or at least answered) by the caller.
        Returns [] when disabled or on any error."""
        query = urllib.parse.urlencode({"bot_id": self.bot_id})
        resp = self._request("GET", f"/api/messages/inbox?{query}")
        if resp is None:
            return []
        items = resp if isinstance(resp, list) else resp.get("messages", [])
        out: list[dict] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and "id" in item and "body" in item:
                out.append({
                    "id": item["id"],
                    "body": str(item["body"]),
                    "created_at": item.get("created_at", ""),
                })
        return out

    def reply(self, body: str, in_reply_to=None) -> bool:
        """POST /api/messages — reply in the dashboard chat thread."""
        payload: dict = {"bot_id": self.bot_id, "body": body}
        if in_reply_to is not None:
            payload["in_reply_to"] = in_reply_to
        return self._request("POST", "/api/messages", payload) is not None
