"""Community Manager — entry point.

Run with:  python -m bot   (from the project root)

Wires everything together:
  config + SQLite + FAQ index + pipeline
  Telegram approval bot (long polling)
  intake tasks (Discord live/mock, email live/mock) feeding the pipeline
  daily digest via APScheduler

The pipeline's only outward effects: FAQ answers where a question was asked,
and Telegram cards to the admin chat. No enforcement exists in this process.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import digest as digest_mod
from . import telegram_bot
from .config import AppConfig, load_config
from .database import Database
from .discord_intake import IntakeItem, mock_discord_stream, run_discord_intake
from .email_intake import mock_email_stream, run_email_poller
from .faq import FaqIndex, seed_faq_if_empty
from .pipeline import Pipeline

log = logging.getLogger(__name__)


def build_pipeline(config: AppConfig, db: Database) -> Pipeline:
    seed_faq_if_empty(db)
    faq_index = FaqIndex(db)
    return Pipeline(
        db,
        faq_index,
        faq_threshold=config.faq_autoanswer_threshold,
        ollama_url=config.ollama_url,
        ollama_model=config.ollama_model,
    )


async def _consume(stream, handler) -> None:
    async for item in stream:
        await handler(item)


async def _bridge_heartbeat_loop(bridge) -> None:
    """Heartbeat to Bot HQ every 60s while the bridge is enabled."""
    while True:
        try:
            await asyncio.to_thread(
                bridge.heartbeat, "online", "Community Manager running"
            )
        except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
            log.debug("Bot HQ heartbeat failed: %s", exc)
        await asyncio.sleep(60)


async def _bridge_chat_loop(config: AppConfig, db: Database, bridge,
                            faq_index=None) -> None:
    """Poll the Bot HQ dashboard inbox every 15s and route chat commands."""
    while True:
        try:
            msgs = await asyncio.to_thread(bridge.fetch_inbox)
            for msg in msgs:
                await telegram_bot.handle_chat_message(
                    config, db, bridge, msg, faq_index=faq_index
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - chat loop must survive errors
            log.warning("Bot HQ chat loop failed: %s", exc)
        await asyncio.sleep(15)


def make_item_handler(config: AppConfig, pipeline: Pipeline, bot, bridge=None):
    """Glue: pipeline-process one item; notify Telegram about flags/drafts."""
    if bridge is None:
        bridge = config.make_bridge()
    watch_tasks: set[asyncio.Task] = set()

    async def _log_bridge_event(kind: str, title: str, detail: str) -> None:
        if bridge is None or not bridge.enabled:
            return
        try:
            await asyncio.to_thread(bridge.log_event, kind, title, detail)
        except Exception as exc:  # noqa: BLE001 - events are best-effort
            log.debug("Bot HQ event %s failed: %s", kind, exc)

    async def handle(item: IntakeItem):
        result = pipeline.process(item)
        if result.flag_id is not None:
            db = pipeline.db
            approval_id = None
            try:
                approval_id = await telegram_bot.send_flag_card(
                    bot, config, db, result.flag_id, bridge=bridge
                )
            except Exception as exc:  # noqa: BLE001 - card stays pending for digest
                log.warning("Flag card send failed (will appear in digest): %s", exc)
            if approval_id is not None:
                task = asyncio.create_task(
                    telegram_bot.watch_flag_decision(
                        config, db, bridge, result.flag_id, approval_id
                    )
                )
                watch_tasks.add(task)
                task.add_done_callback(watch_tasks.discard)
            await _log_bridge_event(
                "flag_raised",
                f"Flag #{result.flag_id} raised",
                f"category={result.flag_category} source={item.source} "
                f"author={item.author}",
            )
        elif result.outcome == "faq_answered" and item.source.endswith("email"):
            try:
                await bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=(
                        "📧 Suggested FAQ reply (not sent — send it yourself if it fits)\n"
                        f"From: {item.author}\n\nQ: {item.text[:400]}\n\nA: {result.faq_answer}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Draft card send failed: %s", exc)
        if result.outcome == "faq_answered":
            await _log_bridge_event(
                "faq_answered",
                "FAQ answered",
                f"source={item.source} author={item.author} "
                f"score={result.faq_score:.2f}",
            )
        return result

    return handle


async def _post_init(app) -> None:
    config: AppConfig = app.bot_data["config"]
    db: Database = app.bot_data["db"]
    pipeline: Pipeline = app.bot_data["pipeline"]
    bridge = app.bot_data.get("bridge") or config.make_bridge()
    app.bot_data["bridge"] = bridge
    handler = make_item_handler(config, pipeline, app.bot, bridge=bridge)
    tasks: list[asyncio.Task] = []

    bridge_tasks: list[asyncio.Task] = []
    if bridge.enabled:
        bridge_tasks.append(asyncio.create_task(_bridge_heartbeat_loop(bridge)))
        bridge_tasks.append(asyncio.create_task(
            _bridge_chat_loop(config, db, bridge, faq_index=pipeline.faq)
        ))
        log.info("Bot HQ bridge: ENABLED (transport=%s)", bridge.transport)
    app.bot_data["bridge_tasks"] = bridge_tasks

    if config.discord_live:
        tasks.append(asyncio.create_task(run_discord_intake(config, handler)))
        log.info("Discord intake: LIVE (read-only)")
    elif config.mock_mode:
        tasks.append(asyncio.create_task(_consume(mock_discord_stream(), handler)))
        log.info("Discord intake: MOCK stream")
    else:
        log.info("Discord intake: OFF (not configured, mock mode off)")

    if config.email_live:
        tasks.append(asyncio.create_task(run_email_poller(config, handler)))
        log.info("Email intake: LIVE (IMAP polling)")
    elif config.mock_mode:
        tasks.append(asyncio.create_task(_consume(mock_email_stream(), handler)))
        log.info("Email intake: MOCK stream")
    else:
        log.info("Email intake: OFF (not configured, mock mode off)")

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    hour, _, minute = config.digest_time.partition(":")
    scheduler = AsyncIOScheduler(timezone=config.timezone)

    async def send_digest() -> None:
        await app.bot.send_message(
            chat_id=config.admin_chat_id, text=digest_mod.build_digest(db)
        )

    scheduler.add_job(
        send_digest,
        CronTrigger(hour=int(hour or 21), minute=int(minute or 0),
                    timezone=config.timezone),
        id="daily-digest",
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    app.bot_data["intake_tasks"] = tasks
    log.info("Digest scheduled daily at %s %s", config.digest_time, config.timezone)


async def _post_shutdown(app) -> None:
    for task in app.bot_data.get("intake_tasks", []):
        task.cancel()
    for task in app.bot_data.get("bridge_tasks", []):
        task.cancel()
    scheduler = app.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        config = load_config()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    db = Database(config.db_path)
    pipeline = build_pipeline(config, db)

    if not config.bot_token:
        # Offline/demo path: run the pipeline over the mock streams without
        # Telegram, printing what WOULD be sent as cards. Ctrl+C to stop.
        log.info("No BOT_TOKEN — running headless mock demo (no Telegram).")
        return _run_headless(config, pipeline)

    app = telegram_bot.build_application(config, db, faq_index=pipeline.faq)
    app.bot_data["pipeline"] = pipeline
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    log.info("Community Manager starting (long polling, mock_mode=%s)", config.mock_mode)
    app.run_polling(drop_pending_updates=True)
    return 0


def _run_headless(config: AppConfig, pipeline: Pipeline) -> int:
    """Mock pipeline demo with no Telegram token: prints cards to stdout."""

    class _StdoutBot:
        async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
            print(f"\n--- Telegram card to {chat_id} ---\n{text}\n")

    handler = make_item_handler(config, pipeline, _StdoutBot())

    async def _amain() -> None:
        tasks = [
            asyncio.create_task(_consume(mock_discord_stream(5.0), handler)),
            asyncio.create_task(_consume(mock_email_stream(9.0), handler)),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
