from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .config import Config
from .ig_client import InstagramClient
from .state import State
from .telegram_client import MediaGroupItem, TelegramClient, TelegramError, TelegramMessage


logger = logging.getLogger(__name__)


class InvalidMediaDownload(RuntimeError):
    pass


class Forwarder:
    def __init__(self, bot: TelegramClient, state: State, config: Config, ig_client: InstagramClient):
        self.bot = bot
        self.state = state
        self.config = config
        self.ig_client = ig_client

    async def forward_message(self, msg: Any, thread_id: str) -> list[TelegramMessage]:
        sent: list[TelegramMessage] = []
        item_type = str(getattr(msg, "item_type", "") or "unknown")
        is_own_message = str(getattr(msg, "user_id", "")) == self.ig_client.user_id
        sender_header = _sender_header(self.config, is_own_message)
        reply_to_message_id = self._reply_to_message_id(msg)
        caption = self._caption_for(msg)
        decorated_caption = _decorate_with_sender(sender_header, caption)

        if item_type == "text" and getattr(msg, "text", None):
            text = str(msg.text)
            sent.append(
                await self._send_text(
                    _decorate_with_sender(sender_header, text),
                    reply_to_message_id,
                )
            )
        elif item_type == "voice_media":
            voice_caption = _decorate_with_sender(sender_header, "🎤 Голосовое сообщение")
            sent.extend(
                await self._send_media_object(
                    getattr(msg, "media", None),
                    voice_caption,
                    reply_to_message_id,
                )
            )
        elif item_type == "media":
            sent.extend(
                await self._send_media_object(
                    getattr(msg, "media", None),
                    decorated_caption,
                    reply_to_message_id,
                )
            )
        elif item_type in {"clip", "reel_share", "media_share", "story_share", "visual_media"}:
            sent.extend(await self._send_shared_media(msg, decorated_caption, reply_to_message_id))
        elif item_type == "xma" or getattr(msg, "xma_share", None):
            sent.extend(
                await self._send_xma(
                    getattr(msg, "xma_share", None),
                    decorated_caption,
                    reply_to_message_id,
                )
            )
        elif item_type == "action_log":
            text = getattr(msg, "text", None) or ""
            if text:
                sent.append(
                    await self._send_text(
                        _decorate_with_sender(sender_header, text),
                        reply_to_message_id,
                    )
                )
        elif item_type == "like":
            text = "\u2764\ufe0f"
            sent.append(
                await self._send_text(
                    _decorate_with_sender(sender_header, text),
                    reply_to_message_id,
                )
            )
        elif item_type == "link":
            link_body = self._link_body_html(msg)
            sent.append(
                await self._send_text(
                    _decorate_with_sender_html(sender_header, link_body),
                    reply_to_message_id,
                )
            )
        elif item_type == "animated_media":
            sent.extend(
                await self._send_animation(
                    getattr(msg, "animated_media", None),
                    decorated_caption,
                    reply_to_message_id,
                )
            )

        if not sent:
            sent.append(
                await self._send_text(
                    _decorate_with_sender(sender_header, f"[unsupported Instagram item_type={item_type}]"),
                    reply_to_message_id,
                )
            )

        for tg_message in sent:
            self.state.save_mapping(
                tg_msg_id=tg_message.message_id,
                ig_msg_id=str(msg.id),
                ig_thread_id=str(thread_id),
                ig_client_context=getattr(msg, "client_context", "") or "",
            )
        return sent

    def _reply_to_message_id(self, msg: Any) -> int | None:
        reply_id = getattr(getattr(msg, "reply", None), "id", None)
        if not reply_id:
            return None
        mapping = self.state.get_mapping_by_ig(str(reply_id))
        if not mapping:
            logger.info("no Telegram mapping for Instagram reply target %s", reply_id)
            return None
        return int(mapping["tg_msg_id"])

    async def _send_shared_media(
        self,
        msg: Any,
        caption: str,
        reply_to_message_id: int | None,
    ) -> list[TelegramMessage]:
        if getattr(msg, "clip", None):
            return await self._send_media_object(msg.clip, caption, reply_to_message_id)
        if getattr(msg, "media_share", None):
            return await self._send_media_object(msg.media_share, caption, reply_to_message_id)
        if getattr(msg, "visual_media", None):
            return await self._send_media_object(
                _get(msg.visual_media, "media"),
                caption,
                reply_to_message_id,
            )
        if getattr(msg, "xma_share", None):
            return await self._send_xma(msg.xma_share, caption, reply_to_message_id)
        reel_share = getattr(msg, "reel_share", None)
        story_share = getattr(msg, "story_share", None)
        media = _get(reel_share, "media") or _get(story_share, "media")
        if media:
            return await self._send_media_object(media, caption, reply_to_message_id)
        link = _get(reel_share, "reel_url") or _get(story_share, "url")
        if link:
            body = _html_link(str(link))
            text = f"{caption}\n{body}" if caption else body
            return [await self._send_text(text, reply_to_message_id)]
        return []

    async def _send_xma(
        self,
        xma: Any,
        caption: str,
        reply_to_message_id: int | None,
    ) -> list[TelegramMessage]:
        if not xma:
            return []
        target_url = _url(_get(xma, "video_url"))
        preview_url = _url(_get(xma, "preview_url"))
        title = caption or _esc(_get(xma, "title") or "")

        if target_url and _is_instagram_permalink(target_url):
            try:
                media = await asyncio.to_thread(self.ig_client.media_from_url, target_url)
                sent = await self._send_media_object(media, title, reply_to_message_id)
                if sent:
                    return sent
            except Exception:
                logger.exception("failed to resolve Instagram share URL %s", target_url)
            return await self._send_preview_with_link(preview_url, title, target_url, reply_to_message_id)

        if target_url and target_url.startswith("http"):
            try:
                return [await self._send_downloaded_video(target_url, title, reply_to_message_id)]
            except (aiohttp.ClientError, asyncio.TimeoutError, TelegramError, InvalidMediaDownload):
                logger.exception("xma target URL is not a downloadable video")
                return await self._send_preview_with_link(preview_url, title, target_url, reply_to_message_id)

        if preview_url and preview_url.startswith("http"):
            return [await self._send_photo(preview_url, title, reply_to_message_id)]
        if target_url:
            return [await self._send_text(f"{title}\n{_html_link(target_url)}", reply_to_message_id)]
        return []

    async def _send_preview_with_link(
        self,
        preview_url: str | None,
        title: str,
        link: str,
        reply_to_message_id: int | None,
    ) -> list[TelegramMessage]:
        link_html = _html_link(link)
        caption = f"{title}\n{link_html}" if title else link_html
        if preview_url and preview_url.startswith("http"):
            try:
                return [await self._send_photo(preview_url, caption, reply_to_message_id)]
            except (aiohttp.ClientError, asyncio.TimeoutError, TelegramError, InvalidMediaDownload):
                logger.exception("failed to send Instagram share preview")
        return [await self._send_text(caption, reply_to_message_id)]

    async def _send_media_object(
        self,
        media: Any,
        caption: str,
        reply_to_message_id: int | None,
    ) -> list[TelegramMessage]:
        if not media:
            return []

        resources = list(_get(media, "resources") or [])
        items = resources if resources else [media]

        plan: list[tuple[str, str, str]] = []  # (kind, url, item_caption)
        for index, item in enumerate(items):
            item_caption = caption if index == 0 else ""
            audio_url = _audio_url(item)
            video_url = _video_url(item)
            photo_url = _photo_url(item)
            media_type = _get(item, "media_type")
            if audio_url:
                plan.append(("voice", audio_url, item_caption))
            elif video_url or media_type == 2:
                if video_url:
                    plan.append(("video", video_url, item_caption))
                elif photo_url:
                    plan.append(("photo", photo_url, item_caption))
            elif photo_url:
                plan.append(("photo", photo_url, item_caption))

        # A carousel (several photos/videos in one Instagram message) reads much
        # better as one Telegram album than as N disconnected messages where only
        # the first one carries the caption/sender header.
        groupable = len(plan) > 1 and all(kind in {"photo", "video"} for kind, _, _ in plan)
        if groupable:
            try:
                return await self._send_as_group(plan, reply_to_message_id)
            except (aiohttp.ClientError, asyncio.TimeoutError, TelegramError, InvalidMediaDownload):
                logger.exception("failed to send Instagram carousel as a Telegram album; sending items separately")

        sent: list[TelegramMessage] = []
        for kind, url, item_caption in plan:
            try:
                if kind == "voice":
                    sent.append(await self._send_downloaded_voice(url, item_caption, reply_to_message_id))
                elif kind == "video":
                    sent.append(await self._send_downloaded_video(url, item_caption, reply_to_message_id))
                else:
                    sent.append(await self._send_photo(url, item_caption, reply_to_message_id))
            except (aiohttp.ClientError, asyncio.TimeoutError, TelegramError, InvalidMediaDownload):
                logger.exception("failed to forward Instagram media item")
                sent.append(await self._send_text("[Instagram media unavailable or expired]", reply_to_message_id))
        return sent

    async def _send_as_group(
        self,
        plan: list[tuple[str, str, str]],
        reply_to_message_id: int | None,
    ) -> list[TelegramMessage]:
        # sendMediaGroup needs every item attached to the same multipart request, so
        # (unlike single sends) we always download first instead of trying the
        # remote URL directly.
        paths: list[str] = []
        try:
            group_items: list[MediaGroupItem] = []
            for kind, url, item_caption in plan:
                suffix = ".mp4" if kind == "video" else ".jpg"
                expected = "video" if kind == "video" else "image"
                path = await self._download_temp(url, suffix=suffix, expected=expected)
                paths.append(path)
                group_items.append(
                    MediaGroupItem(kind=kind, source=path, caption=_truncate_html(item_caption, 1024) or None)
                )
            return await self.bot.send_media_group(
                self.config.tg_chat_id,
                group_items,
                reply_to_message_id=reply_to_message_id,
            )
        finally:
            for path in paths:
                Path(path).unlink(missing_ok=True)

    async def _send_animation(
        self,
        animated_media: Any,
        caption: str,
        reply_to_message_id: int | None,
    ) -> list[TelegramMessage]:
        url = _url(
            _get(animated_media, "images", "fixed_height", "url")
            or _get(animated_media, "images", "fixed_width", "url")
        )
        if not url:
            return []
        try:
            return [
                await self.bot.send_animation(
                    self.config.tg_chat_id,
                    animation=url,
                    caption=_truncate_html(caption, 1024) or None,
                    reply_to_message_id=reply_to_message_id,
                )
            ]
        except TelegramError:
            logger.exception("Telegram rejected animation URL, falling back to text")
            return [await self._send_text(_html_link(url), reply_to_message_id)]

    async def _send_text(self, text: str, reply_to_message_id: int | None = None) -> TelegramMessage:
        return await self.bot.send_message(
            self.config.tg_chat_id,
            _truncate_html(text or "[empty Instagram message]", 4096),
            disable_web_page_preview=False,
            reply_to_message_id=reply_to_message_id,
        )

    async def _send_photo(
        self,
        url: str,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> TelegramMessage:
        try:
            return await self.bot.send_photo(
                self.config.tg_chat_id,
                photo=url,
                caption=_truncate_html(caption, 1024) or None,
                reply_to_message_id=reply_to_message_id,
            )
        except TelegramError:
            logger.info("Telegram could not fetch photo URL directly; downloading first")
            path = await self._download_temp(url, suffix=".jpg", expected="image")
            try:
                return await self.bot.send_photo(
                    self.config.tg_chat_id,
                    photo=path,
                    caption=_truncate_html(caption, 1024) or None,
                    reply_to_message_id=reply_to_message_id,
                )
            finally:
                Path(path).unlink(missing_ok=True)

    async def _send_downloaded_video(
        self,
        url: str,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> TelegramMessage:
        path = await self._download_temp(url, suffix=".mp4", expected="video")
        try:
            return await self.bot.send_video(
                self.config.tg_chat_id,
                video=path,
                caption=_truncate_html(caption, 1024) or None,
                supports_streaming=True,
                reply_to_message_id=reply_to_message_id,
            )
        finally:
            Path(path).unlink(missing_ok=True)

    async def _send_downloaded_voice(
        self,
        url: str,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> TelegramMessage:
        path = await self._download_temp(url, suffix=".m4a", expected="audio")
        try:
            return await self.bot.send_voice(
                self.config.tg_chat_id,
                voice=path,
                caption=_truncate_html(caption, 1024) or None,
                reply_to_message_id=reply_to_message_id,
            )
        finally:
            Path(path).unlink(missing_ok=True)

    async def _download_temp(self, url: str, suffix: str, expected: str | None = None) -> str:
        # The file is created up front (not inside the download's `with` block) so a
        # mid-download failure still leaves a `path` we can clean up in `except` below
        # instead of orphaning a partial file in the temp dir.
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            timeout = aiohttp.ClientTimeout(total=self.config.media_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    with open(path, "wb") as handle:
                        async for chunk in response.content.iter_chunked(1024 * 512):
                            handle.write(chunk)
            _validate_download(path, expected)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        return path

    def _caption_for(self, msg: Any) -> str:
        text = (
            _get(msg, "clip", "caption_text")
            or _get(msg, "media_share", "caption_text")
            or _get(msg, "xma_share", "title")
            or _get(msg, "text")
            or ""
        )
        return str(text).strip()

    def _link_body_html(self, msg: Any) -> str:
        context = _get(msg, "link", "link_context")
        link = _get(context, "link_url") or _get(msg, "link", "text")
        title = _get(context, "link_title")
        if not link:
            return "[Instagram link]"
        return _html_link(str(link), str(title) if title else None)

def _video_url(media: Any) -> str | None:
    return _url(
        _get(media, "video_url")
        or _get(media, "video_versions", 0, "url")
        or _get(media, "video_versions", 0, "fallback", "url")
    )


def _audio_url(media: Any) -> str | None:
    return _url(_get(media, "audio_url") or _get(media, "audio", "audio_src"))


def _photo_url(media: Any) -> str | None:
    return _url(
        _get(media, "thumbnail_url")
        or _get(media, "preview_url")
        or _get(media, "image_versions2", "candidates", 0, "url")
        or _get(media, "image_versions2", "candidates", 0, "fallback", "url")
    )


def _url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _sender_header(config: Config, is_own_message: bool) -> str:
    username = config.ig_username if is_own_message else config.ig_target_username
    arrow = "➡️" if is_own_message else "⬅️"
    label = "sent from Instagram" if is_own_message else "Instagram"
    return f"<b>{arrow} @{_esc(username)} · {label}</b>"


def _decorate_with_sender(sender_header: str, text: str) -> str:
    """Join a sender header with plain, untrusted text (escaped for HTML parse_mode)."""
    text = text.strip()
    return _decorate_with_sender_html(sender_header, _esc(text) if text else "")


def _decorate_with_sender_html(sender_header: str, html_body: str) -> str:
    """Join a sender header with a body that is already safe HTML (not re-escaped)."""
    return f"{sender_header}\n{html_body}" if html_body else sender_header


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _html_link(url: str, label: str | None = None) -> str:
    display = _esc(label) if label else _esc(url)
    return f'<a href="{_esc(url)}">{display}</a>'


def _truncate_html(escaped_text: str, limit: int) -> str:
    """Truncate text that is already HTML-escaped without cutting an entity in half
    (e.g. leaving a dangling `&am` from `&amp;`), which would make Telegram reject
    the whole message as unparsable HTML."""
    if len(escaped_text) <= limit:
        return escaped_text
    ellipsis = "…"
    cut = escaped_text[: limit - len(ellipsis)]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut + ellipsis


def _is_instagram_permalink(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host not in {"instagram.com", "www.instagram.com"}:
        return False
    first_part = next((part for part in parsed.path.split("/") if part), "")
    return first_part in {"p", "reel", "reels", "tv"}


def _validate_download(path: str, expected: str | None) -> None:
    if not expected:
        return
    with Path(path).open("rb") as handle:
        head = handle.read(16 * 1024)
    if head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise InvalidMediaDownload("downloaded HTML instead of media")
    if expected in {"video", "audio"} and not _looks_like_mp4(head):
        raise InvalidMediaDownload("downloaded file is not an MP4/M4A container")
    if expected == "image" and not _looks_like_image(head):
        raise InvalidMediaDownload("downloaded file is not a supported image")


def _looks_like_mp4(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp"


def _looks_like_image(head: bytes) -> bool:
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"GIF87a")
        or head.startswith(b"GIF89a")
        or head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    )


def _get(obj: Any, *path: Any) -> Any:
    current = obj
    for key in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(key, int) and isinstance(current, (list, tuple)):
            current = current[key] if len(current) > key else None
        else:
            current = getattr(current, str(key), None)
    return current

