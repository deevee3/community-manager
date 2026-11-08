"""Configuration loading.

Secrets come from ``.env`` only (BOT_TOKEN, ADMIN_USER_ID, Discord/email
credentials as placeholders he fills in). Nothing is hardcoded. Behavior
knobs are env vars with safe defaults so mock mode runs with zero setup.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .bridge import Bridge, normalize_transport

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")


def _load_dotenv() -> None:
    """Load .env if python-dotenv is present; env vars work either way."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH)
    except ImportError:  # pragma: no cover - dotenv is in requirements
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("Env %s is not a number; using default %d", name, default)
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("Env %s is not a number; using default %s", name, default)
        return default


@dataclass
class AppConfig:
    # --- Telegram approval surface (required for live mode) ---
    bot_token: str = ""
    admin_user_id: int = 0
    admin_chat_id: int = 0

    # --- Discord intake (read-only; empty token => mock intake) ---
    discord_bot_token: str = ""
    discord_channel_ids: list[int] = field(default_factory=list)

    # --- Email intake (empty host => mock intake) ---
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_user: str = ""
    email_password: str = ""
    email_poll_seconds: int = 300

    # --- Behavior knobs ---
    mock_mode: bool = True
    faq_autoanswer_threshold: float = 0.55
    timezone: str = "America/New_York"
    digest_time: str = "21:00"  # HH:MM local
    db_path: str = ""

    # --- Optional Ollama re-scoring on the Mac Studio (never required) ---
    ollama_url: str = ""
    ollama_model: str = ""

    # --- Bot HQ web bridge (optional; empty URL/key => disabled) ---
    transport: str = "telegram"
    bot_hq_url: str = ""
    bot_hq_key: str = ""

    def make_bridge(self) -> Bridge:
        return Bridge(
            url=self.bot_hq_url,
            key=self.bot_hq_key,
            transport=self.transport,
            capabilities=["chat"],
        )

    @property
    def discord_live(self) -> bool:
        return bool(self.discord_bot_token) and not self.mock_mode

    @property
    def email_live(self) -> bool:
        return bool(self.email_imap_host and self.email_user) and not self.mock_mode


def load_config() -> AppConfig:
    _load_dotenv()

    admin_user_id = _int_env("ADMIN_USER_ID", 0)
    cfg = AppConfig(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        admin_user_id=admin_user_id,
        admin_chat_id=_int_env("ADMIN_CHAT_ID", admin_user_id),
        discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
        discord_channel_ids=[
            int(part)
            for part in os.environ.get("DISCORD_CHANNEL_IDS", "").replace(";", ",").split(",")
            if part.strip().lstrip("-").isdigit()
        ],
        email_imap_host=os.environ.get("EMAIL_IMAP_HOST", "").strip(),
        email_imap_port=_int_env("EMAIL_IMAP_PORT", 993),
        email_user=os.environ.get("EMAIL_USER", "").strip(),
        email_password=os.environ.get("EMAIL_PASSWORD", ""),
        email_poll_seconds=_int_env("EMAIL_POLL_SECONDS", 300),
        mock_mode=os.environ.get("MOCK_MODE", "1").strip() not in ("0", "false", "False"),
        faq_autoanswer_threshold=_float_env("FAQ_AUTOANSWER_THRESHOLD", 0.55),
        timezone=os.environ.get("TIMEZONE", "America/New_York").strip() or "America/New_York",
        digest_time=os.environ.get("DIGEST_TIME", "21:00").strip() or "21:00",
        db_path=os.environ.get(
            "DB_PATH", os.path.join(BASE_DIR, "data", "bot.db")
        ).strip(),
        ollama_url=os.environ.get("OLLAMA_URL", "").strip(),
        ollama_model=os.environ.get("OLLAMA_MODEL", "").strip(),
        transport=normalize_transport(os.environ.get("TRANSPORT")),
        bot_hq_url=os.environ.get("BOT_HQ_URL", "").strip(),
        bot_hq_key=os.environ.get("BOT_HQ_KEY", "").strip(),
    )

    if cfg.bot_token.startswith("123456") or cfg.bot_token == "<redacted>":
        log.warning("BOT_TOKEN looks like the .env.example placeholder.")
    if not cfg.mock_mode and (not cfg.bot_token or not cfg.admin_user_id):
        raise RuntimeError(
            "Live mode needs BOT_TOKEN and ADMIN_USER_ID in .env "
            "(or set MOCK_MODE=1 to run the demo pipeline)."
        )
    return cfg
