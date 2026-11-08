"""Flag queue service + decision handling — pure logic, no I/O side effects.

This is the heart of the human-in-the-loop design:

- ``create_flag`` files a classifier verdict for review.
- ``handle_decision`` is what the Telegram callback calls. It verifies the
  decider is the admin, flips the flag status, and writes the decision log.

``handle_decision`` deliberately returns a *record* of the human's decision.
There is no enforcement step after it — no function in this package can act
on Discord, email, or Telegram messages. Approved recommendations are carried
out by D'Vaughn himself in the native apps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .classifier import Classification
from .database import Database

log = logging.getLogger(__name__)

APPROVE = "approve"
DISMISS = "dismiss"


@dataclass
class DecisionResult:
    ok: bool
    flag_id: int
    decision: str = ""        # approved | dismissed
    message: str = ""         # human-readable outcome for the card edit


def create_flag(db: Database, *, intake_id: int | None, source: str,
                author: str, channel: str, text: str,
                verdict: Classification) -> int:
    flag_id = db.flag_create(
        intake_id=intake_id,
        source=source,
        author=author,
        channel=channel,
        text=text,
        category=verdict.category,
        confidence=verdict.confidence,
        recommendation=verdict.recommendation,
    )
    log.info("Flag #%d created [%s %.2f] from %s",
             flag_id, verdict.category, verdict.confidence, source)
    return flag_id


def callback_payload(flag_id: int, action: str) -> str:
    """Inline-button payload: flag:<id>:approve | flag:<id>:dismiss."""
    return f"flag:{flag_id}:{action}"


def parse_callback(data: str) -> tuple[int, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "flag":
        return None
    try:
        flag_id = int(parts[1])
    except ValueError:
        return None
    if parts[2] not in (APPROVE, DISMISS):
        return None
    return flag_id, parts[2]


def handle_decision(db: Database, *, flag_id: int, action: str,
                    decided_by: int, admin_user_id: int) -> DecisionResult:
    """Record the admin's tap. The ONLY effect is bookkeeping in SQLite."""
    if decided_by != admin_user_id:
        return DecisionResult(False, flag_id, message="Only the admin can decide.")
    flag = db.flag_get(flag_id)
    if flag is None:
        return DecisionResult(False, flag_id, message="Flag not found.")
    if flag["status"] != "pending":
        return DecisionResult(
            False, flag_id, message=f"Already {flag['status']} — nothing changed."
        )
    new_status = "approved" if action == APPROVE else "dismissed"
    if not db.flag_set_status(flag_id, new_status):
        return DecisionResult(False, flag_id, message="Flag changed under you; refresh.")
    db.decision_record(flag_id, new_status, decided_by)
    if new_status == "approved":
        msg = ("Decision recorded: APPROVED. The bot takes no action — "
               "carry out the recommendation yourself if you still want to.")
    else:
        msg = "Decision recorded: DISMISSED. No action taken."
    log.info("Flag #%d %s by %d", flag_id, new_status, decided_by)
    return DecisionResult(True, flag_id, decision=new_status, message=msg)


def format_flag_card(flag_row) -> str:
    """Plain-text card shown in Telegram for one pending flag."""
    excerpt = flag_row["text"]
    if len(excerpt) > 600:
        excerpt = excerpt[:600] + "…"
    return (
        f"🚩 Flag #{flag_row['id']} — {flag_row['category'].upper()} "
        f"(confidence {flag_row['confidence']:.0%})\n"
        f"Source: {flag_row['source']} · {flag_row['channel'] or '-'}\n"
        f"Author: {flag_row['author'] or 'unknown'}\n\n"
        f"{excerpt}\n\n"
        f"{flag_row['recommendation']}"
    )
