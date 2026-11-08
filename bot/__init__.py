"""Community Manager — triage + FAQ + human-decision flags (Hermes Bot #3).

Modules:
    config          Load .env secrets into a typed dataclass.
    database        SQLite persistence (intake, faq, flags, decisions).
    faq             FAQ store + TF-IDF retrieval (pure stdlib).
    classifier      Rule-based flag taxonomy (+ optional Ollama re-score).
    flags           Flag queue service + decision handling (pure logic).
    pipeline        Intake -> FAQ match / flag creation orchestration.
    discord_intake  Read-only Discord listener (mock mode included).
    email_intake    IMAP inbox poller (mock mode included).
    telegram_bot    Approval surface: flag cards, Approve/Dismiss, admin cmds.
    digest          Daily pending-flags digest text.

HARD RULE: this package contains no enforcement calls anywhere. It cannot
ban, mute, kick, timeout, or delete. It flags; D'Vaughn decides in Telegram;
he executes (or not) in the native apps himself.
"""

__version__ = "1.0.0"
