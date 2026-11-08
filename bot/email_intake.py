"""Email intake: IMAP poller for the community inbox (stdlib only).

Reads UNSEEN messages, hands them to the pipeline, and marks them seen so
they are processed once. It never sends, moves, or deletes mail — replies to
FAQ-matching email are surfaced to D'Vaughn in Telegram as drafts, not sent.

Mock mode (no IMAP config, or MOCK_MODE=1): yields sample inbox traffic.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from email.header import decode_header

from .discord_intake import IntakeItem

log = logging.getLogger(__name__)

MOCK_EMAIL_MESSAGES: list[tuple[str, str, str]] = [
    ("Sarah <sarah@example.com>", "Question about the community",
     "Hi! I found the Prism community through a friend. What are the community rules before I join? Thanks, Sarah"),
    ("promo@bulkoffers.example", "PARTNERSHIP $$$",
     "Dear admin, make $9000/week from your mailing list!! Just share your seed phrase to verify your wallet and claim your airdrop now. Limited time!!"),
    ("tom@example.com", "Scam DM report",
     "Someone in the Discord DM'd me pretending to be support and asked me to verify my wallet. I didn't. How do I report a scammer properly?"),
    ("investor@fund.example", "Speaking opportunity",
     "Hello, we run a small fund and would love D'Vaughn to speak at our (virtual) builder night next month. No rush — wanted to ask if that's something he'd consider and who to coordinate with."),
]


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = ""
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out += chunk.decode(enc or "utf-8", errors="replace")
        else:
            out += chunk
    return out


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                payload = part.get_payload(decode=True) or b""
                return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode("utf-8", errors="replace")


def fetch_unseen(config, limit: int = 20) -> list[IntakeItem]:
    """One synchronous IMAP pass. Called via asyncio.to_thread in live mode."""
    items: list[IntakeItem] = []
    conn = imaplib.IMAP4_SSL(config.email_imap_host, config.email_imap_port)
    try:
        conn.login(config.email_user, config.email_password)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return items
        ids = (data[0] or b"").split()[-limit:]
        for msg_id in ids:
            status, fetched = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = email.message_from_bytes(fetched[0][1])
            subject = _decode(msg.get("Subject"))
            body = _body_text(msg).strip()
            text = f"{subject}\n\n{body}".strip() if subject else body
            items.append(IntakeItem(
                source="email",
                author=_decode(msg.get("From")),
                channel="inbox",
                text=text[:4000],
            ))
            conn.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return items


async def mock_email_stream(interval: float = 45.0):
    """Async generator of sample inbox messages (demo/offline mode)."""
    idx = 0
    while True:
        author, _subject, text = MOCK_EMAIL_MESSAGES[idx % len(MOCK_EMAIL_MESSAGES)]
        idx += 1
        yield IntakeItem(source="mock-email", author=author, channel="inbox", text=text)
        await asyncio.sleep(interval)


async def run_email_poller(config, on_item) -> None:
    """Live IMAP polling loop. Not started in mock mode."""
    while True:
        try:
            for item in await asyncio.to_thread(fetch_unseen, config):
                await on_item(item)
        except Exception as exc:  # noqa: BLE001 - keep polling through errors
            log.warning("Email poll failed: %s", exc)
        await asyncio.sleep(config.email_poll_seconds)
