"""Daily digest: pending flags + activity summary (plain text).

Built as a pure function over the database so it is testable offline; the
Telegram side just sends the string.
"""
from __future__ import annotations

from .database import Database


def _theme_section() -> list[str]:
    """Weekly theme summary from the fleet wiki mirror, if present.
    Admin-digest only; never affects triage taxonomy or verdicts."""
    try:
        with open("/app/fleet-wiki/cm-theme-healthy-rooms.md", encoding="utf-8") as _tf:
            text = _tf.read()
    except OSError:
        return []
    lines: list[str] = []
    in_summary = False
    for line in text.splitlines():
        if line.startswith("## Summary"):
            in_summary = True
            continue
        if in_summary and line.startswith("## "):
            break
        if in_summary and line.strip():
            lines.append(line.strip())
    if not lines:
        return []
    return ["", "This week's theme \u2014 healthy rooms:", ""] + lines


def build_digest(db: Database, limit: int = 20) -> str:
    counts = db.counts()
    pending = db.flag_pending(limit=limit)
    lines = [
        "Community Manager — daily digest",
        "",
        f"Intake (all time): {counts['intake']} items · "
        f"FAQ-answered: {counts['faq_answered']} · "
        f"flags approved: {counts['flags_approved']} · "
        f"dismissed: {counts['flags_dismissed']}",
        "",
    ]
    if not pending:
        lines.append("No pending flags. Inbox zero. 🎉")
        return "\n".join(lines + _theme_section())
    lines.append(f"Pending flags needing your decision: {len(pending)}")
    for row in pending:
        excerpt = row["text"].replace("\n", " ")
        if len(excerpt) > 90:
            excerpt = excerpt[:90] + "…"
        lines.append(
            f"- #{row['id']} [{row['category']}] {row['source']}/"
            f"{row['channel'] or '-'} · {row['author'] or 'unknown'}: {excerpt}"
        )
    lines.append("")
    lines.append("Open the bot chat and tap through the cards, or use /pending.")
    return "\n".join(lines + _theme_section())
