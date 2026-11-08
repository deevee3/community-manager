"""Discord intake: READ-ONLY listener.

The bot only *reads* messages in configured channels and forwards them to the
pipeline. It requests no moderation permissions and calls no moderation or
message-management endpoints — enforcement is structurally impossible here.

Mock mode (no token, or MOCK_MODE=1): emits a rotating set of sample messages
so the full pipeline can be exercised offline.

discord.py is imported lazily inside ``run_discord_intake`` so the rest of
the package (and the smoke test) works without it installed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class IntakeItem:
    source: str          # discord | email | mock-discord | mock-email
    author: str
    channel: str
    text: str


MOCK_DISCORD_MESSAGES: list[tuple[str, str, str]] = [
    ("new_builder_kai", "#general", "Hey everyone, just joined — what is Prism exactly?"),
    ("luna.builds", "#general", "When is the daily digest posted? Want to set a reminder."),
    ("xX_pumpking_Xx", "#general", "🚀🚀 SEND 1 ETH GET 2 ETH BACK!! double your crypto guaranteed profit, claim your airdrop now — just verify your wallet and share your seed phrase 🚀🚀"),
    ("quietreader", "#help", "How do I report a scammer? Someone DM'd me asking for my private key."),
    ("marcus_t", "#general", "you are pathetic, shut up idiot, nobody wants you here"),
    ("spammy_sam", "#general", "Make $5000/week work from home!! Join my telegram group for trading signals and profit https://spam.example/a https://spam.example/b https://spam.example/c"),
    ("dev_dana", "#general", "Can I promote my project here? Building a small analytics tool for the community."),
]


async def mock_discord_stream(interval: float = 20.0):
    """Async generator of sample Discord messages (demo/offline mode)."""
    idx = 0
    while True:
        author, channel, text = MOCK_DISCORD_MESSAGES[idx % len(MOCK_DISCORD_MESSAGES)]
        idx += 1
        yield IntakeItem(source="mock-discord", author=author, channel=channel, text=text)
        await asyncio.sleep(interval)


async def run_discord_intake(config, on_item) -> None:
    """Live read-only listener. ``on_item`` is an async IntakeItem callback.

    Only invoked when a real token is configured; in mock mode __main__
    consumes ``mock_discord_stream`` instead.
    """
    import discord  # lazy: optional at import time, required for live mode

    intents = discord.Intents.default()
    intents.message_content = True  # read message text; no member/mod intents
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:  # noqa: D401
        log.info("Discord intake connected as %s (read-only)", client.user)

    @client.event
    async def on_message(message) -> None:  # noqa: D401
        if message.author == client.user:
            return
        if config.discord_channel_ids and message.channel.id not in config.discord_channel_ids:
            return
        result = await on_item(IntakeItem(
            source="discord",
            author=str(message.author),
            channel=f"#{getattr(message.channel, 'name', message.channel.id)}",
            text=message.content or "",
        ))
        # FAQ auto-answer: posting an *answer* is the bot's one permitted
        # write. It is not moderation — no message is ever altered or removed.
        if result is not None and result.outcome == "faq_answered" and result.faq_answer:
            try:
                await message.channel.send(result.faq_answer)
            except Exception as exc:  # noqa: BLE001 - answer is best-effort
                log.warning("FAQ answer post failed: %s", exc)

    await client.start(config.discord_bot_token)
