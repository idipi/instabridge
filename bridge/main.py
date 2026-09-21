from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .config import Config, load_config
from .forwarder import Forwarder
from .ig_client import (
    InstagramClient,
    is_challenge_error,
    is_ip_checkpoint_error,
    is_login_required_error,
)
from .state import State
from .telegram_client import TelegramClient
from .tg_bot import telegram_update_loop


CURSOR_LAST_IG_MESSAGE_ID = "last_ig_message_id"
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    state = State(config.db_path)
    state.init_db()

    ig_client = InstagramClient(config)
    await asyncio.to_thread(ig_client.login)

    bot = TelegramClient(token=config.tg_bot_token, timeout=config.media_timeout)
    await bot.start()
    forwarder = Forwarder(bot, state, config, ig_client)

    poll_task = asyncio.create_task(poll_loop(config, state, ig_client, forwarder, bot))
    tg_task = asyncio.create_task(telegram_update_loop(config, state, ig_client, bot))
    try:
        await asyncio.gather(poll_task, tg_task)
    finally:
        poll_task.cancel()
        tg_task.cancel()
        await bot.close()


async def poll_loop(
    config: Config,
    state: State,
    ig_client: InstagramClient,
    forwarder: Forwarder,
    bot: TelegramClient,
) -> None:
    while True:
        try:
            await poll_once(config, state, ig_client, forwarder)
        except Exception as exc:
            if is_challenge_error(exc):
                logger.exception("Instagram challenge required; pausing polling for 5 minutes")
                await _notify(bot, config, "⚠️ Instagram challenge — нужно пройти вручную в браузере. Поллинг на паузе 5 мин.")
                await asyncio.sleep(300)
            elif is_ip_checkpoint_error(exc):
                logger.exception(
                    "Instagram Direct API returned 467; check IG_PROXY and IG_DEVICE_JSON app version"
                )
                await _notify(bot, config, "⚠️ Instagram 467 (IP блок). Проверь IG_PROXY. Поллинг на паузе 5 мин.")
                await asyncio.sleep(300)
            elif is_login_required_error(exc):
                logger.exception("Instagram session expired; trying to login again")
                ig_client.invalidate_thread_cache()
                try:
                    await asyncio.to_thread(ig_client.login)
                    await _notify(bot, config, "ℹ️ Instagram сессия обновлена, поллинг продолжается.")
                except Exception:
                    logger.exception("Instagram re-login failed")
                    await _notify(bot, config, "⚠️ Instagram перелогин не удался. Поллинг на паузе 5 мин.")
                await asyncio.sleep(300)
            else:
                logger.exception("Instagram poll failed")
                ig_client.invalidate_thread_cache()
        await asyncio.sleep(config.poll_interval)


async def _notify(bot: TelegramClient, config: Config, text: str) -> None:
    try:
        await bot.send_message(config.tg_chat_id, text)
    except Exception:
        logger.exception("failed to send Telegram notification")


async def poll_once(
    config: Config,
    state: State,
    ig_client: InstagramClient,
    forwarder: Forwarder,
) -> None:
    thread_id = await asyncio.to_thread(ig_client.get_thread_id)
    raw_messages = await asyncio.to_thread(ig_client.fetch_messages, thread_id, 20)
    messages = list(reversed(raw_messages))

    candidates = _new_candidates(messages, state, config)
    for msg in candidates:
        msg_id = str(getattr(msg, "id", ""))
        if not msg_id:
            continue

        if state.get_mapping_by_ig(msg_id) or state.is_known_ig_message(msg_id):
            state.set_cursor(CURSOR_LAST_IG_MESSAGE_ID, msg_id)
            continue

        try:
            await forwarder.forward_message(msg, thread_id)
            state.set_cursor(CURSOR_LAST_IG_MESSAGE_ID, msg_id)
        except Exception:
            logger.exception("failed to forward Instagram message %s", msg_id)

    if config.ig_mark_seen and candidates:
        await asyncio.to_thread(ig_client.mark_seen, thread_id)
    state.set_cursor("last_poll_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _new_candidates(messages: list[Any], state: State, config: Config) -> list[Any]:
    if not messages:
        return []

    last_seen = state.get_cursor(CURSOR_LAST_IG_MESSAGE_ID)
    if not last_seen:
        return messages[-config.initial_history :] if config.initial_history else []

    for index, msg in enumerate(messages):
        if str(getattr(msg, "id", "")) == last_seen:
            return messages[index + 1 :]

    logger.warning("last Instagram cursor %s was not in the recent batch", last_seen)
    return [msg for msg in messages if not state.get_mapping_by_ig(str(getattr(msg, "id", "")))]


if __name__ == "__main__":
    asyncio.run(main())
