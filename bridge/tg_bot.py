from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .ig_client import InstagramClient
from .state import State
from .telegram_client import TelegramClient


logger = logging.getLogger(__name__)


async def telegram_update_loop(
    config: Config,
    state: State,
    ig_client: InstagramClient,
    bot: TelegramClient,
) -> None:
    offset: int | None = None
    allowed_updates = ["message"]
    while True:
        try:
            updates = await bot.get_updates(offset, timeout=30, allowed_updates=allowed_updates)
            for update in updates:
                offset = int(update["update_id"]) + 1
                await handle_update(update, config, state, ig_client, bot)
        except Exception:
            logger.exception("Telegram polling failed")
            await asyncio.sleep(5)


async def handle_update(
    update: dict[str, Any],
    config: Config,
    state: State,
    ig_client: InstagramClient,
    bot: TelegramClient,
) -> None:
    if "message" in update:
        await _handle_reply(update["message"], config, state, ig_client, bot)


async def _handle_reply(
    message: dict[str, Any],
    config: Config,
    state: State,
    ig_client: InstagramClient,
    bot: TelegramClient,
) -> None:
    chat_id = _chat_id(message)
    message_id = int(message.get("message_id", 0) or 0)
    if chat_id != config.tg_chat_id:
        logger.info("ignored Telegram message %s from chat %s", message_id, chat_id)
        return

    raw_text = (message.get("text") or "").strip()
    if raw_text == "/status":
        await _handle_status(config, state, bot)
        return

    if "reply_to_message" in message:
        reply_to = message["reply_to_message"]
        reply_to_message_id = int(reply_to["message_id"])
        logger.info("handling Telegram reply %s to %s", message_id, reply_to_message_id)
        mapping = state.get_mapping(reply_to_message_id)
        if not mapping:
            logger.warning("no Instagram mapping for Telegram message %s", reply_to_message_id)
            await bot.send_message(config.tg_chat_id, "Cannot find the original Instagram message for this reply.")
            return

        thread_id = str(mapping["ig_thread_id"])
        reply_to_item_id = str(mapping["ig_msg_id"])
        reply_context = str(mapping.get("ig_client_context") or "")
    else:
        logger.info("handling standalone Telegram message %s", message_id)
        thread_id = await asyncio.to_thread(ig_client.get_thread_id)
        reply_to_item_id = None
        reply_context = None
    temp_paths: list[str] = []
    sent_ig_messages: list[Any] = []
    sent_anything = False

    try:
        file_job = _telegram_file_job(message)
        if file_job:
            file_id, suffix, sender = file_job
            path = await _download_telegram_file(bot, file_id, suffix)
            temp_paths.append(path)
            sent_ig_messages.append(await asyncio.to_thread(sender, ig_client, thread_id, path))
            sent_anything = True

        text = message.get("text") or message.get("caption")
        if text:
            sent_ig_messages.append(
                await asyncio.to_thread(
                    ig_client.send_text,
                    thread_id,
                    text,
                    reply_to_item_id,
                    reply_context,
                )
            )
            sent_anything = True

        if not sent_anything:
            logger.warning("unsupported Telegram reply message %s", message_id)
            await bot.send_message(config.tg_chat_id, "This Telegram message type is not supported for Instagram yet.")
            return

        sent_ids: list[str] = []
        for sent_ig in sent_ig_messages:
            sent_ig_id = getattr(sent_ig, "id", None)
            if sent_ig_id:
                state.save_known_ig_message(str(sent_ig_id))
                state.save_ig_to_tg_mapping(
                    ig_msg_id=str(sent_ig_id),
                    tg_msg_id=message_id,
                    ig_thread_id=thread_id,
                    ig_client_context=getattr(sent_ig, "client_context", "") or "",
                )
                sent_ids.append(str(sent_ig_id))

        logger.info("sent Telegram reply %s to Instagram ids %s", message_id, sent_ids)
    except Exception:
        logger.exception("failed to send Telegram reply to Instagram")
        await bot.send_message(config.tg_chat_id, "Instagram send failed; check service logs.")
    finally:
        for path in temp_paths:
            Path(path).unlink(missing_ok=True)


async def _handle_status(config: Config, state: State, bot: TelegramClient) -> None:
    last_poll = state.get_cursor("last_poll_at") or "—"
    stats = state.get_stats()
    lines = [
        "🟢 Bridge is alive",
        f"Instagram target: @{config.ig_target_username}",
        f"Last poll: {last_poll} UTC",
        f"Mapped messages: {stats['mapped']}",
        f"Sent from TG: {stats['sent_from_tg']}",
    ]
    await bot.send_message(config.tg_chat_id, "\n".join(lines))


def _telegram_file_job(message: dict[str, Any]) -> tuple[str, str, Any] | None:
    if message.get("photo"):
        return message["photo"][-1]["file_id"], ".jpg", _send_photo
    if message.get("video"):
        return message["video"]["file_id"], ".mp4", _send_video
    if message.get("animation"):
        return message["animation"]["file_id"], ".mp4", _send_video

    document = message.get("document")
    mime_type = (document or {}).get("mime_type") or ""
    if document and mime_type.startswith("image/"):
        return document["file_id"], ".jpg", _send_photo
    if document and mime_type.startswith("video/"):
        return document["file_id"], ".mp4", _send_video
    return None


def _send_photo(ig_client: InstagramClient, thread_id: str, path: str) -> None:
    return ig_client.send_photo(thread_id, path)


def _send_video(ig_client: InstagramClient, thread_id: str, path: str) -> None:
    return ig_client.send_video(thread_id, path)


async def _download_telegram_file(bot: TelegramClient, file_id: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        await bot.download_file(file_id, path)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def _chat_id(payload: dict[str, Any]) -> int | None:
    chat = payload.get("chat") or {}
    chat_id = chat.get("id")
    return int(chat_id) if chat_id is not None else None
