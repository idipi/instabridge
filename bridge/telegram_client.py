from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramMessage:
    message_id: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class MediaGroupItem:
    kind: str  # "photo" or "video"
    source: str | Path  # URL, or a local file path
    caption: str | None = None
    parse_mode: str | None = "HTML"


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout + 10))

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def get_updates(
        self,
        offset: int | None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates:
            payload["allowed_updates"] = allowed_updates
        return await self.request("getUpdates", payload, timeout=timeout + 5)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool = False,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> TelegramMessage:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        _add_reply(data, reply_to_message_id)
        result = await self.request(
            "sendMessage",
            data,
        )
        return TelegramMessage(int(result["message_id"]), result)

    async def send_photo(
        self,
        chat_id: int,
        photo: str | Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> TelegramMessage:
        return await self._send_media(
            "sendPhoto", chat_id, "photo", photo, caption, reply_to_message_id, parse_mode=parse_mode
        )

    async def send_video(
        self,
        chat_id: int,
        video: str | Path,
        caption: str | None = None,
        supports_streaming: bool = True,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> TelegramMessage:
        return await self._send_media(
            "sendVideo",
            chat_id,
            "video",
            video,
            caption,
            reply_to_message_id,
            parse_mode=parse_mode,
            extra={"supports_streaming": supports_streaming},
        )

    async def send_voice(
        self,
        chat_id: int,
        voice: str | Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> TelegramMessage:
        return await self._send_media(
            "sendVoice", chat_id, "voice", voice, caption, reply_to_message_id, parse_mode=parse_mode
        )

    async def send_animation(
        self,
        chat_id: int,
        animation: str | Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> TelegramMessage:
        return await self._send_media(
            "sendAnimation",
            chat_id,
            "animation",
            animation,
            caption,
            reply_to_message_id,
            parse_mode=parse_mode,
        )

    async def send_media_group(
        self,
        chat_id: int,
        items: list[MediaGroupItem],
        reply_to_message_id: int | None = None,
    ) -> list[TelegramMessage]:
        if not items:
            raise ValueError("send_media_group requires at least one item")
        session = self._require_session()
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        _add_reply_form(form, reply_to_message_id)

        media_payload: list[dict[str, Any]] = []
        opened_files = []
        try:
            for index, item in enumerate(items):
                source_text = str(item.source)
                entry: dict[str, Any] = {"type": item.kind}
                if source_text.startswith(("http://", "https://")):
                    entry["media"] = source_text
                else:
                    attach_name = f"file{index}"
                    handle = Path(source_text).open("rb")
                    opened_files.append(handle)
                    form.add_field(attach_name, handle, filename=Path(source_text).name)
                    entry["media"] = f"attach://{attach_name}"
                if item.caption:
                    entry["caption"] = item.caption
                    if item.parse_mode:
                        entry["parse_mode"] = item.parse_mode
                media_payload.append(entry)
            form.add_field("media", json.dumps(media_payload, ensure_ascii=False))

            async with session.post(f"{self.base_url}/sendMediaGroup", data=form) as response:
                payload = await response.json(content_type=None)
                if not payload.get("ok"):
                    raise TelegramError(f"sendMediaGroup failed: {payload}")
                return [TelegramMessage(int(item["message_id"]), item) for item in payload["result"]]
        finally:
            for handle in opened_files:
                handle.close()

    async def download_file(self, file_id: str, destination: str | Path) -> None:
        file_info = await self.request("getFile", {"file_id": file_id})
        file_path = file_info["file_path"]
        session = self._require_session()
        async with session.get(f"{self.file_url}/{file_path}") as response:
            response.raise_for_status()
            with Path(destination).open("wb") as handle:
                async for chunk in response.content.iter_chunked(1024 * 512):
                    handle.write(chunk)

    async def request(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        session = self._require_session()
        async with session.post(
            f"{self.base_url}/{method}",
            data=_encode_form(data or {}),
            timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
        ) as response:
            payload = await response.json(content_type=None)
            if not payload.get("ok"):
                raise TelegramError(f"{method} failed: {payload}")
            return payload.get("result")

    async def _send_media(
        self,
        method: str,
        chat_id: int,
        field: str,
        media: str | Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        extra: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> TelegramMessage:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
            if parse_mode:
                data["parse_mode"] = parse_mode
        _add_reply(data, reply_to_message_id)
        if extra:
            data.update(extra)

        media_text = str(media)
        path = Path(media_text)
        if not media_text.startswith(("http://", "https://")) and path.exists():
            session = self._require_session()
            form = aiohttp.FormData()
            for key, value in data.items():
                form.add_field(key, str(value).lower() if isinstance(value, bool) else str(value))
            with path.open("rb") as handle:
                form.add_field(field, handle, filename=path.name)
                async with session.post(f"{self.base_url}/{method}", data=form) as response:
                    payload = await response.json(content_type=None)
                    if not payload.get("ok"):
                        raise TelegramError(f"{method} failed: {payload}")
                    result = payload["result"]
                    return TelegramMessage(int(result["message_id"]), result)

        data[field] = media_text
        result = await self.request(method, data)
        return TelegramMessage(int(result["message_id"]), result)

    def _require_session(self) -> aiohttp.ClientSession:
        if not self.session:
            raise RuntimeError("Telegram client is not started")
        return self.session


def _encode_form(data: dict[str, Any]) -> dict[str, str]:
    encoded: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, bool):
            encoded[key] = str(value).lower()
        elif isinstance(value, (list, dict)):
            encoded[key] = json.dumps(value, ensure_ascii=False)
        else:
            encoded[key] = str(value)
    return encoded


def _add_reply(data: dict[str, Any], reply_to_message_id: int | None) -> None:
    if reply_to_message_id:
        data["reply_to_message_id"] = int(reply_to_message_id)
        data["allow_sending_without_reply"] = True


def _add_reply_form(form: aiohttp.FormData, reply_to_message_id: int | None) -> None:
    if reply_to_message_id:
        form.add_field("reply_to_message_id", str(int(reply_to_message_id)))
        form.add_field("allow_sending_without_reply", "true")
