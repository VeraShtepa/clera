"""Business-connection handlers — the secretary core.

Two update types drive everything:
  * business_connection   -> owner enabled/edited/disabled the bot in their chats
  * business_message      -> a contact messaged the owner; we draft a reply

Replies are sent with ``business_connection_id`` so they go out AS THE OWNER.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from app.agent.secretary import DraftResult, draft_reply, summarize_contact
from app.config import settings
from app.policy.policy import Decision, decide, is_stale
from app.store import repo as store
from app.store.models import Connection, Contact

log = logging.getLogger(__name__)


def _can_reply_of(bc: object) -> bool:
    """Read reply permission across Bot API versions."""
    rights = getattr(bc, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(bc, "can_reply", False))


async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner connected, edited, or removed the bot as their business chatbot."""
    bc = update.business_connection
    if bc is None:
        return

    if bc.is_enabled:
        conn = store.upsert_connection(
            business_connection_id=bc.id,
            owner_user_id=bc.user.id,
            can_reply=_can_reply_of(bc),
        )
        log.info(
            "Business connection enabled: %s (owner %s, can_reply=%s)",
            bc.id,
            bc.user.id,
            conn.can_reply,
        )
        await context.bot.send_message(
            chat_id=_control_chat(conn.owner_user_id),
            text=(
                "✅ **Clera подключена.**\n"
                "Я буду автоматически отвечать на ваши чаты от вашего имени. "
                "Если сообщение потребует вашего личного участия (деньги, "
                "обязательства или вопросы, в которых я не разбираюсь), "
                "я пришлю вам уведомление сюда."
            ),
            parse_mode="Markdown",
        )
    else:
        store.disable_connection(bc.id)
        log.info("Business connection disabled: %s", bc.id)


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a message in a connected chat."""
    msg = update.business_message
    if msg is None:
        return
    text = msg.text
    unheard_voice = False
    if not text and msg.voice is not None:
        transcript = await _voice_to_text(context, msg)
        if transcript is None:
            log.warning("Voice message in chat %s could not be transcribed", msg.chat.id)
            text = "[voice] (audio archived, no transcript yet)"
            unheard_voice = True
        else:
            text = f"[voice] {transcript}"
    elif not text and msg.photo:
        description = None
        if not settings.collect_only:
            description = await _photo_to_text(context, msg)
        parts = ["[photo]"]
        if description:
            parts.append(description)
        if msg.caption:
            parts.append(f"(caption: {msg.caption})")
        text = " ".join(parts)
    if not text:
        return

    bc_id = msg.business_connection_id
    conn = store.get_connection(bc_id)
    if conn is None:
        try:
            bc = await context.bot.get_business_connection(bc_id)
            conn = store.upsert_connection(
                business_connection_id=bc.id,
                owner_user_id=bc.user.id,
                can_reply=_can_reply_of(bc),
            )
            log.info("Recovered business connection %s on demand", bc_id)
        except Exception:
            log.exception("Could not resolve business connection %s", bc_id)
            return

    sender_id = msg.from_user.id if msg.from_user else None
    chat_id = msg.chat.id
    ts = int(msg.date.timestamp()) if msg.date else 0
    from_owner = sender_id is not None and sender_id == conn.owner_user_id

    store.record_message(
        business_connection_id=bc_id,
        chat_id=chat_id,
        direction="out" if from_owner else "in",
        sender_id=sender_id,
        text=text,
        ts=ts,
    )
    contact_name = None if from_owner else (msg.from_user.first_name if msg.from_user else None)
    contact = store.bump_contact(bc_id, chat_id, name=contact_name, ts=ts)

    if settings.collect_only:
        log.info("Collect-only: recorded message on %s chat %s", bc_id, chat_id)
        return

    if from_owner:
        log.info("Recorded owner message on %s chat %s (not drafting)", bc_id, chat_id)
        await _maybe_refresh_profile(conn, chat_id, contact)
        return

    if contact.muted:
        log.info("Contact %s is muted; recorded only", chat_id)
        return

    if is_stale(ts, int(time.time()), settings.stale_after_seconds):
        log.info("Message on %s chat %s is stale (backlog); recorded, not replying", bc_id, chat_id)
        return

    if unheard_voice:
        await context.bot.send_message(
            chat_id=_control_chat(conn.owner_user_id),
            text=(
                f"🎙 *{contact.name or 'Контакт'}* прислал голосовое сообщение, "
                "которое я не смог расшифровать. Пожалуйста, прослушайте и ответьте сами."
            ),
            parse_mode="Markdown",
        )
        store.record_activity(
            bc_id, chat_id, "escalated", f"{contact.name or chat_id}: voice (no transcript)", ts
        )
        return

    local_hour = msg.date.hour if msg.date else 12
    outcome = decide(conn, sender_id, local_hour)
    if outcome.decision is Decision.IGNORE:
        log.info("Ignoring message on %s: %s", bc_id, outcome.reason)
        return

    history = store.recent_messages(bc_id, chat_id, settings.history_limit)
    exemplar_history = store.recent_messages(bc_id, chat_id, settings.exemplar_history_limit)
    result = await asyncio.to_thread(
        draft_reply,
        history=history,
        contact_name=contact.name,
        profile=contact.profile,
        tone=conn.settings.tone,
        tier=conn.settings.tier,
        exemplar_history=exemplar_history,
    )
    store.record_spend(bc_id, result.cost_usd)

    if outcome.decision is Decision.AUTO_SEND:
        await _handle_auto(context, conn, chat_id, msg, text, result, ts)
        await _maybe_refresh_profile(conn, chat_id, contact)
        return

    contact_label = contact.name or f"чат {chat_id}"

    if result.action == "silent":
        store.record_activity(bc_id, chat_id, "silent", f"{contact_label}: {text[:80]}", ts)
        log.info("Agent stayed silent on %s chat %s (review mode)", bc_id, chat_id)
        return
    if result.action == "notify":
        await context.bot.send_message(
            chat_id=_control_chat(conn.owner_user_id),
            text=f"👋 *Контакт требует вашего внимания:*\n{text}\n\n_{result.text}_",
            parse_mode="Markdown",
        )
        store.record_activity(bc_id, chat_id, "escalated", f"{contact_label}: {text[:80]}", ts)
        log.info("Escalated message on %s chat %s to owner (review mode)", bc_id, chat_id)
        return

    draft = store.create_draft(
        business_connection_id=bc_id,
        target_chat_id=chat_id,
        incoming_text=text,
        proposed_text=result.text,
        cost_usd=result.cost_usd,
    )
    demo = " _(заглушка — установите ANTHROPIC_API_KEY для реальных ответов)_" if result.placeholder else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Отправить", callback_data=f"send:{draft.draft_id}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"discard:{draft.draft_id}"),
            ]
        ]
    )
    await context.bot.send_message(
        chat_id=_control_chat(conn.owner_user_id),
        text=(
            f"✉️ *Новое сообщение от контакта:*\n{text}\n\n"
            f"🤖 *Предлагаемый ответ* (~${result.cost_usd:.4f}, {result.model}){demo}:\n"
            f"{result.text}"
        ),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    store.record_activity(bc_id, chat_id, "drafted", f"{contact_label}: {text[:80]}", ts)
    log.info(
        "Drafted reply on %s (draft %s, ~$%.4f, awaiting approval)",
        bc_id,
        draft.draft_id,
        result.cost_usd,
    )
    await _maybe_refresh_profile(conn, chat_id, contact)


async def _voice_to_text(context, msg) -> str | None:
    from app.agent import transcribe
    tg_file = await context.bot.get_file(msg.voice.file_id)
    if not transcribe.available():
        archive_dir = os.path.join(os.path.dirname(settings.sqlite_path) or ".", "voices")
        os.makedirs(archive_dir, exist_ok=True)
        path = os.path.join(archive_dir, f"{msg.chat.id}_{msg.message_id}.oga")
        try:
            await tg_file.download_to_drive(custom_path=path)
            log.info("No transcriber on this host; archived voice note to %s", path)
        except Exception:
            log.exception("Could not archive voice note for chat %s", msg.chat.id)
        return None
    fd, path = tempfile.mkstemp(suffix=".oga")
    os.close(fd)
    try:
        await tg_file.download_to_drive(custom_path=path)
        return await asyncio.to_thread(transcribe.transcribe, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


async def _photo_to_text(context, msg) -> str | None:
    from app.agent import vision
    if not vision.available():
        return None
    tg_file = await context.bot.get_file(msg.photo[-1].file_id)
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        await tg_file.download_to_drive(custom_path=path)
        return await asyncio.to_thread(vision.describe, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


async def _handle_auto(
    context, conn: Connection, chat_id: int, msg, incoming_text: str, result: DraftResult, ts: int
) -> None:
    """Fully automatic mode: act on the agent's reply/silent/notify decision."""
    bc_id = conn.business_connection_id
    contact_label = (msg.from_user.first_name if msg.from_user else None) or "контакт"
    control = _control_chat(conn.owner_user_id)

    if result.action == "silent":
        store.record_activity(
            bc_id, chat_id, "silent", f"{contact_label}: {incoming_text[:80]}", ts
        )
        log.info("Agent stayed silent on %s chat %s", bc_id, chat_id)
        return

    if result.action == "notify" or result.placeholder:
        note = (
            result.text
            if result.action == "notify"
            else "LLM не настроен (проверьте ANTHROPIC_API_KEY), поэтому я не могу ответить за вас."
        )
        await context.bot.send_message(
            chat_id=control,
            text=f"👋 *{contact_label} требует вашего внимания:*\n{incoming_text}\n\n_{note}_",
            parse_mode="Markdown",
        )
        store.record_activity(
            bc_id, chat_id, "escalated", f"{contact_label}: {incoming_text[:80]}", ts
        )
        log.info("Escalated message on %s chat %s to owner", bc_id, chat_id)
        return

    latest = store.recent_messages(bc_id, chat_id, 1)
    if latest and latest[-1].direction == "out":
        log.info("Owner replied on %s chat %s while drafting; staying silent", bc_id, chat_id)
        return

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=result.text,
        business_connection_id=bc_id,
    )
    store.record_message(
        business_connection_id=bc_id,
        chat_id=chat_id,
        direction="out",
        sender_id=conn.owner_user_id,
        text=result.text,
        ts=int(sent.date.timestamp()) if sent.date else ts,
    )
    store.record_activity(bc_id, chat_id, "replied", f"{contact_label}: {incoming_text[:80]}", ts)
    log.info("Auto-replied on %s chat %s (cost $%.4f)", bc_id, chat_id, result.cost_usd)

    if settings.notify_auto_replies:
        await context.bot.send_message(
            chat_id=control,
            text=f"↩️ *{contact_label}:* {incoming_text}\n*Я:* {result.text}",
            parse_mode="Markdown",
        )


def _control_chat(owner_user_id: int) -> int:
    return settings.control_chat_id or owner_user_id


async def _maybe_refresh_profile(conn: Connection, chat_id: int, contact: Contact) -> None:
    every = settings.profile_refresh_every
    if every <= 0 or contact.message_count % every != 0:
        return

    bc_id = conn.business_connection_id
    history = store.recent_messages(bc_id, chat_id, settings.history_limit)

    def _work() -> str | None:
        return summarize_contact(
            history=history,
            contact_name=contact.name,
            existing_profile=contact.profile,
            tier=conn.settings.tier,
        )

    profile = await asyncio.to_thread(_work)
    if profile:
        store.update_contact_profile(bc_id, chat_id, profile)
        log.info(
            "Refreshed profile for %s chat %s (%d msgs)", bc_id, chat_id, contact.message_count
        )
