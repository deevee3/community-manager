"""Offline smoke test — no Telegram, no Discord, no network, no third-party
packages required. Exercises, in mock mode:

  1. FAQ seeding + TF-IDF retrieval (a real question gets the seeded answer).
  2. Pipeline: FAQ question -> faq_answered; scam message -> flag created;
     benign chatter -> ignored; unmatched email -> needs_human flag.
  3. Approval callback logic: payload round-trip, admin-only decision,
     approve + dismiss paths, double-decision rejection.
  4. Structural guarantee: no enforcement call exists anywhere in bot/.

Run:  python3 tests/smoke_test.py     (prints PASS/FAIL per check)
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import flags  # noqa: E402
from bot.database import Database  # noqa: E402
from bot.discord_intake import IntakeItem, MOCK_DISCORD_MESSAGES  # noqa: E402
from bot.email_intake import MOCK_EMAIL_MESSAGES  # noqa: E402
from bot.faq import FaqIndex, seed_faq_if_empty  # noqa: E402
from bot.pipeline import Pipeline  # noqa: E402
from bot.digest import build_digest  # noqa: E402

ADMIN_ID = 8698221315  # test fixture only — not a credential
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cm-smoke-")
    db = Database(os.path.join(tmp, "test.db"))

    # ---------------------------------------------------------- 1. FAQ
    seeded = seed_faq_if_empty(db)
    check("FAQ seed loads", seeded >= 5, f"{seeded} entries")
    index = FaqIndex(db)
    match = index.best_match("what are the rules of this community?")
    check(
        "FAQ retrieval finds the rules answer",
        match is not None and "No spam" in match.answer and match.score >= 0.4,
        f"score={match.score if match else None}",
    )
    miss = index.best_match("zqxjkvb blorp unrelated nonsense words")
    check(
        "FAQ retrieval rejects nonsense",
        miss is None or miss.score < 0.55,
        f"score={miss.score if miss else None}",
    )

    # ---------------------------------------------------------- 2. Pipeline
    pipe = Pipeline(db, index, faq_threshold=0.55)

    r = pipe.process(IntakeItem(
        "mock-discord", "curious_newbie", "#general",
        "Hi! When is the daily digest posted?",
    ))
    check("Pipeline FAQ-answers a known question",
          r.outcome == "faq_answered" and "9:00 PM" in r.faq_answer,
          f"outcome={r.outcome}")

    r = pipe.process(IntakeItem(
        "mock-discord", "xX_pumpking_Xx", "#general",
        "SEND 1 ETH GET 2 ETH — double your crypto, guaranteed profit! "
        "Verify your wallet and share your seed phrase to claim your airdrop now!",
    ))
    check("Pipeline flags a scam message",
          r.outcome == "flagged" and r.flag_category == "scam" and r.flag_id is not None,
          f"category={r.flag_category}")
    scam_flag_id = r.flag_id

    r = pipe.process(IntakeItem(
        "mock-discord", "dev_dana", "#general",
        "Shipped the new landing page today, pretty happy with how the hero turned out.",
    ))
    check("Pipeline ignores benign chatter", r.outcome == "ignored", f"outcome={r.outcome}")

    r = pipe.process(IntakeItem(
        "mock-email", "investor@fund.example", "inbox",
        "Hello, we run a small fund and would love D'Vaughn to speak at our "
        "builder night next month. Who should we coordinate with?",
    ))
    check("Unmatched email becomes a needs_human flag",
          r.outcome == "flagged" and r.flag_category == "needs_human",
          f"category={r.flag_category}")
    email_flag_id = r.flag_id

    # Mock fixtures all parse through the pipeline without exceptions.
    ok = True
    for author, channel, text in MOCK_DISCORD_MESSAGES:
        pipe.process(IntakeItem("mock-discord", author, channel, text))
    for author, _subj, text in MOCK_EMAIL_MESSAGES:
        pipe.process(IntakeItem("mock-email", author, "inbox", text))
    check("All bundled mock fixtures process cleanly", ok)

    # ---------------------------------------------------------- 3. Decisions
    payload = flags.callback_payload(scam_flag_id, flags.APPROVE)
    check("Callback payload round-trips",
          flags.parse_callback(payload) == (scam_flag_id, flags.APPROVE),
          payload)
    check("Callback parser rejects junk",
          flags.parse_callback("flag:abc:approve") is None
          and flags.parse_callback("other:1:approve") is None
          and flags.parse_callback("") is None)

    res = flags.handle_decision(db, flag_id=scam_flag_id, action=flags.APPROVE,
                                decided_by=111111, admin_user_id=ADMIN_ID)
    check("Non-admin tap is refused", not res.ok)
    check("Flag still pending after refused tap",
          db.flag_get(scam_flag_id)["status"] == "pending")

    res = flags.handle_decision(db, flag_id=scam_flag_id, action=flags.APPROVE,
                                decided_by=ADMIN_ID, admin_user_id=ADMIN_ID)
    check("Admin approve records the decision",
          res.ok and res.decision == "approved"
          and db.flag_get(scam_flag_id)["status"] == "approved")
    check("Decision is in the audit log",
          len(db.decisions_for_flag(scam_flag_id)) == 1)

    res = flags.handle_decision(db, flag_id=scam_flag_id, action=flags.DISMISS,
                                decided_by=ADMIN_ID, admin_user_id=ADMIN_ID)
    check("Second decision on a closed flag is rejected", not res.ok)

    res = flags.handle_decision(db, flag_id=email_flag_id, action=flags.DISMISS,
                                decided_by=ADMIN_ID, admin_user_id=ADMIN_ID)
    check("Admin dismiss works", res.ok and res.decision == "dismissed")

    digest = build_digest(db)
    check("Digest builds and reports counts",
          "Community Manager" in digest and "FAQ-answered" in digest)

    # ---------------------------------------------------------- 4. No enforcement
    bot_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot"
    )
    forbidden = [
        r"ban_chat_member", r"restrict_chat_member", r"delete_message",
        r"\.ban\s*\(", r"\.kick\s*\(", r"\.timeout\s*\(", r"member\.edit",
        r"purge", r"bulk_delete",
    ]
    offenders = []
    for name in os.listdir(bot_dir):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(bot_dir, name), encoding="utf-8").read()
        # Strip docstrings/comments mentions: only call-shaped hits count.
        code = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith(("#", '"', "'"))
        )
        for pat in forbidden:
            if re.search(pat, code):
                offenders.append(f"{name}: {pat}")
    check("No enforcement calls exist in bot/ source", not offenders,
          "; ".join(offenders))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("SMOKE TEST: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
