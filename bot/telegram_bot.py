"""Telegram approval surface — the only place decisions happen.

Flag cards with ✅ Approve / 🗑 Dismiss buttons. A tap calls
``flags.handle_decision`` (pure bookkeeping) and edits the card with the
outcome. This module sends messages to ONE chat (the admin's) and edits its
own cards. It never touches Discord/email content and contains no call that
could remove, restrict, or punish anyone.

python-telegram-bot is imported inside ``build_application`` so the rest of
the package imports cleanly without it (offline tests).
"""
from __future__ import annotations

import asyncio
import logging

from . import digest as digest_mod
from . import flags
from .config import AppConfig
from .database import Database

log = logging.getLogger(__name__)


def _keyboard(flag_id: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Approve rec.", callback_data=flags.callback_payload(flag_id, flags.APPROVE)
        ),
        InlineKeyboardButton(
            "🗑 Dismiss", callback_data=flags.callback_payload(flag_id, flags.DISMISS)
        ),
    ]])


async def send_flag_card(bot, config: AppConfig, db: Database, flag_id: int,
                         bridge=None):
    """Push one flag card to the admin surface(s) and mark it notified.

    Telegram card when ``bridge`` is None or ``bridge.use_telegram``;
    Bot HQ approval when ``bridge.use_web``. Returns the Bot HQ approval
    id when one was created, otherwise None (callers may ignore it).
    """
    row = db.flag_get(flag_id)
    if row is None or row["status"] != "pending":
        return None
    notified = False
    approval_id = None
    if bridge is None or bridge.use_telegram:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=flags.format_flag_card(row),
            reply_markup=_keyboard(flag_id),
        )
        notified = True
    if bridge is not None and bridge.use_web:
        approval_id = await asyncio.to_thread(
            bridge.request_approval,
            "flag",
            f"Flag #{flag_id} — {row['category']}",
            flags.format_flag_card(row),
            {"flag_id": flag_id},
        )
        if approval_id is not None:
            notified = True
    if notified:
        db.flag_mark_notified(flag_id)
    return approval_id


async def watch_flag_decision(config: AppConfig, db: Database, bridge,
                              flag_id: int, approval_id) -> None:
    """Wait for a Bot HQ dashboard decision and record it (bookkeeping).

    The dashboard acts as the admin, so the decision is applied with the
    configured admin id. Recording only — nothing is acted on anywhere.
    """
    try:
        decision = await asyncio.to_thread(
            bridge.poll_decision, approval_id, timeout_s=86400, interval_s=15
        )
        if decision is None:
            log.info("Bot HQ approval %s for flag #%d: no decision in 24h",
                     approval_id, flag_id)
            return
        status = str(decision.get("status", "")).lower()
        if status == "approved":
            action = flags.APPROVE
        elif status in ("dismissed", "skipped"):
            # Bot HQ calls it "skipped"; this bot's vocabulary is dismiss.
            action = flags.DISMISS
        elif status == "edited":
            # Treat an edit as approval. The edited_body is not applied:
            # flags have no editable body — a flag card is a fixed record.
            action = flags.APPROVE
        else:
            log.info("Bot HQ approval %s: unhandled status %r",
                     approval_id, status)
            return
        result = flags.handle_decision(
            db,
            flag_id=flag_id,
            action=action,
            decided_by=config.admin_user_id,
            admin_user_id=config.admin_user_id,
        )
        try:
            await asyncio.to_thread(
                bridge.log_event,
                "decision",
                f"Flag #{flag_id} decision via Bot HQ: {status}",
                result.message,
            )
        except Exception as exc:  # noqa: BLE001 - event logging is best-effort
            log.debug("Bot HQ decision event failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - watcher must never crash the bot
        log.warning("Flag decision watcher for #%d failed: %s", flag_id, exc)


CHAT_HELP = (
    "Community Manager — dashboard chat commands\n\n"
    "/pending — list flags waiting on your decision\n"
    "/stats — intake / FAQ / flag counts\n"
    "/digest — today's digest\n"
    "/approve <id> — record an approve decision for a flag\n"
    "/dismiss <id> — record a dismiss decision for a flag\n"
    "/faq_add Question | Answer — teach the FAQ a new answer\n\n"
    "Decisions only record your choice in the bot's books. "
    "This bot never acts on a flag itself, on any surface."
)


async def handle_chat_message(config: AppConfig, db: Database, bridge,
                              msg, faq_index=None) -> None:
    """Route one Bot HQ dashboard chat message and reply via the bridge.

    Pure routing over db/flags/digest; the only side effects are decision
    bookkeeping, FAQ additions, and the bridge reply itself.
    """
    body = str((msg or {}).get("body", "")).strip()
    msg_id = (msg or {}).get("id")

    async def _reply(text: str) -> None:
        await asyncio.to_thread(bridge.reply, text, msg_id)

    try:
        if body == "/pending":
            pending = db.flag_pending()
            if not pending:
                await _reply("No pending flags. 🎉")
            else:
                lines = ["Pending flags:"]
                for row in pending:
                    lines.append(f"Flag #{row['id']} — {row['category']}")
                await _reply("\n".join(lines))
            return
        if body == "/stats":
            c = db.counts()
            await _reply(
                "Community Manager stats\n"
                f"- intake items: {c['intake']}\n"
                f"- FAQ entries: {c['faq']} (auto-answered: {c['faq_answered']})\n"
                f"- flags pending: {c['flags_pending']}\n"
                f"- flags approved: {c['flags_approved']} · dismissed: {c['flags_dismissed']}"
            )
            return
        if body == "/digest":
            await _reply(digest_mod.build_digest(db))
            return
        if body.startswith("/approve") or body.startswith("/dismiss"):
            action = flags.APPROVE if body.startswith("/approve") else flags.DISMISS
            parts = body.split()
            flag_id = None
            if len(parts) == 2:
                try:
                    flag_id = int(parts[1].lstrip("#"))
                except ValueError:
                    flag_id = None
            if flag_id is None:
                await _reply(f"Format: /{action} <flag id>  (e.g. /{action} 3)")
                return
            result = flags.handle_decision(
                db,
                flag_id=flag_id,
                action=action,
                decided_by=config.admin_user_id,
                admin_user_id=config.admin_user_id,
            )
            await _reply(result.message)
            return
        if body.startswith("/faq_add"):
            raw = body.partition(" ")[2]
            question, sep, answer = raw.partition("|")
            if not sep or not question.strip() or not answer.strip():
                await _reply("Format: /faq_add Question | Answer")
                return
            db.faq_add(question.strip(), answer.strip())
            if faq_index is not None:
                faq_index.refresh()
                await _reply("FAQ added. It can be auto-answered from now on.")
            else:
                await _reply(
                    "FAQ added to the database. The live FAQ index was not "
                    "reachable from chat, so it takes effect on next restart."
                )
            return
        await _reply(CHAT_HELP)
    except Exception as exc:  # noqa: BLE001 - chat loop must survive bad input
        log.warning("Dashboard chat handling failed for %r: %s", body[:80], exc)
        try:
            await _reply("Something went wrong handling that — see the bot log.")
        except Exception:  # noqa: BLE001
            pass


def build_application(config: AppConfig, db: Database, faq_index=None):
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
    )

    def is_admin(update) -> bool:
        user = update.effective_user
        return user is not None and user.id == config.admin_user_id

    async def cmd_id(update, context) -> None:
        if not is_admin(update):
            return
        chat = update.effective_chat
        await update.message.reply_text(
            f"chat_id: {chat.id}\nyour user_id: {update.effective_user.id}"
        )

    async def cmd_help(update, context) -> None:
        if not is_admin(update):
            return
        await update.message.reply_text(
            "Community Manager — admin commands\n\n"
            "/pending — show flags waiting on your decision\n"
            "/stats — intake / FAQ / flag counts\n"
            "/faq_add Question | Answer — teach the FAQ a new answer\n"
            "/digest — send today's digest now\n"
            "/id — show this chat's ID and your user ID\n\n"
            "Reminder: approving a flag only records your decision. "
            "This bot never bans, mutes, or deletes — you act in the "
            "native app if you choose to."
        )

    async def cmd_stats(update, context) -> None:
        if not is_admin(update):
            return
        c = db.counts()
        await update.message.reply_text(
            "Community Manager stats\n"
            f"- intake items: {c['intake']}\n"
            f"- FAQ entries: {c['faq']} (auto-answered: {c['faq_answered']})\n"
            f"- flags pending: {c['flags_pending']}\n"
            f"- flags approved: {c['flags_approved']} · dismissed: {c['flags_dismissed']}"
        )

    async def cmd_pending(update, context) -> None:
        if not is_admin(update):
            return
        pending = db.flag_pending(limit=10)
        if not pending:
            await update.message.reply_text("No pending flags. 🎉")
            return
        for row in pending:
            await update.message.reply_text(
                flags.format_flag_card(row), reply_markup=_keyboard(int(row["id"]))
            )

    async def cmd_faq_add(update, context) -> None:
        if not is_admin(update):
            return
        raw = (update.message.text or "").partition(" ")[2]
        question, sep, answer = raw.partition("|")
        if not sep or not question.strip() or not answer.strip():
            await update.message.reply_text("Format: /faq_add Question | Answer")
            return
        db.faq_add(question.strip(), answer.strip())
        if faq_index is not None:
            faq_index.refresh()
        await update.message.reply_text("FAQ added. It can be auto-answered from now on.")

    async def cmd_digest(update, context) -> None:
        if not is_admin(update):
            return
        await update.message.reply_text(digest_mod.build_digest(db))

    async def on_flag_tap(update, context) -> None:
        query = update.callback_query
        parsed = flags.parse_callback(query.data if query else "")
        if parsed is None:
            await query.answer("Unknown button.")
            return
        flag_id, action = parsed
        result = flags.handle_decision(
            db,
            flag_id=flag_id,
            action=action,
            decided_by=query.from_user.id,
            admin_user_id=config.admin_user_id,
        )
        await query.answer(result.message[:180])
        if result.ok:
            bridge = None
            try:
                bridge = context.application.bot_data.get("bridge")
            except Exception:  # noqa: BLE001 - logging is best-effort
                bridge = None
            if bridge is not None and bridge.enabled:
                try:
                    await asyncio.to_thread(
                        bridge.log_event,
                        "decision",
                        f"Flag #{flag_id} decision via Telegram: {result.decision}",
                        result.message,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("Bot HQ decision event failed: %s", exc)
            row = db.flag_get(flag_id)
            try:
                await query.edit_message_text(
                    flags.format_flag_card(row) + f"\n\n— {result.message}"
                )
            except Exception as exc:  # noqa: BLE001 - card edit is cosmetic
                log.debug("Card edit failed: %s", exc)

    app = ApplicationBuilder().token(config.bot_token).build()
    app.bot_data["config"] = config
    app.bot_data["db"] = db
    app.bot_data["bridge"] = config.make_bridge()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("faq_add", cmd_faq_add))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CallbackQueryHandler(on_flag_tap, pattern=r"^flag:\d+:(approve|dismiss)$"))
    return app
