from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import instagrapi.extractors as _ig_extractors
from curl_cffi.requests import Session as _CurlSession

from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    ClientLoginRequired,
    LoginRequired,
    TwoFactorRequired,
)
from instagrapi.types import DirectMessage

from .config import Config


logger = logging.getLogger(__name__)


# instagrapi drops the `action_log` field because DirectMessage has no such attribute.
# Patch the extractor once at import time to pull action_log.description into text
# so forwarder can display reaction events ("Reacted 😮 to your message").
_orig_extract_dm = _ig_extractors.extract_direct_message


def _extract_dm_patched(data: dict) -> DirectMessage:
    if data.get("item_type") == "action_log":
        description = (data.get("action_log") or {}).get("description", "").strip()
        if description and not data.get("text"):
            data["text"] = description
    return _orig_extract_dm(data)


_ig_extractors.extract_direct_message = _extract_dm_patched


# instagrapi's curl_cffi transport disables browser impersonation, so its TLS/HTTP2
# fingerprint doesn't match a real Android client. Impersonate mobile Chrome instead
# to blend in with genuine app traffic on Instagram's private API.
_orig_curl_session_request = _CurlSession.request


def _curl_session_request_impersonated(self, method, url, *args, **kwargs):
    kwargs.setdefault("impersonate", "chrome131_android")
    return _orig_curl_session_request(self, method, url, *args, **kwargs)


_CurlSession.request = _curl_session_request_impersonated


class InstagramClient:
    def __init__(self, config: Config):
        self.config = config
        self.cl: Client | None = None
        self._thread_id: str | None = None

    @property
    def user_id(self) -> str:
        self._require_client()
        return str(self.cl.user_id)

    def login(self) -> None:
        cl = self._new_client()
        self.cl = cl
        sessionid = self._sessionid()
        if self.config.ig_session_file.exists():
            logger.info("loading Instagram session from %s", self.config.ig_session_file)
            try:
                cl.load_settings(self.config.ig_session_file)
                cl.login(
                    self.config.ig_username,
                    self.config.ig_password,
                    relogin=False,
                    verification_code=self._verification_code(),
                )
                return
            except (LoginRequired, ClientLoginRequired, BadPassword):
                logger.warning("Instagram session expired, recreating it")
                self.config.ig_session_file.unlink(missing_ok=True)
            except TwoFactorRequired:
                logger.warning("Instagram session needs a fresh 2FA code")
                raise

        logger.info("logging into Instagram as %s", self.config.ig_username)
        try:
            cl.login(
                self.config.ig_username,
                self.config.ig_password,
                verification_code=self._verification_code(),
            )
            self._dump_settings(cl)
            return
        except TwoFactorRequired:
            logger.warning("Instagram password login needs a fresh 2FA code")
            raise
        except (BadPassword, LoginRequired, ClientLoginRequired, ChallengeRequired) as exc:
            if not sessionid:
                raise
            logger.warning(
                "Instagram password login failed with %s; falling back to sessionid",
                exc.__class__.__name__,
            )
        except Exception as exc:
            if not sessionid:
                raise
            logger.warning(
                "Instagram password login failed unexpectedly with %s; falling back to sessionid",
                exc.__class__.__name__,
            )

        cl = self._new_client()
        self.cl = cl
        logger.info("logging into Instagram with sessionid")
        self._login_by_sessionid(cl)
        self._dump_settings(cl)

    def get_thread_id(self) -> str:
        if self._thread_id:
            return self._thread_id

        cl = self._require_client()
        target = self.config.ig_target_username
        threads = cl.direct_threads(amount=30, thread_message_limit=1)
        for thread in threads:
            for user in getattr(thread, "users", []) or []:
                username = str(getattr(user, "username", "")).lower()
                if username == target:
                    self._thread_id = str(getattr(thread, "id", None) or getattr(thread, "pk"))
                    logger.info("matched Instagram thread %s for @%s", self._thread_id, target)
                    return self._thread_id
        raise RuntimeError(f"Could not find Instagram DM thread with @{target}")

    def fetch_messages(self, thread_id: str, amount: int = 20) -> list[DirectMessage]:
        cl = self._require_client()
        return cl.direct_messages(int(thread_id), amount=amount)

    def media_from_url(self, url: str) -> Any:
        cl = self._require_client()
        media_pk = cl.media_pk_from_url(url)
        return cl.media_info(media_pk, use_cache=False)

    def send_text(
        self,
        thread_id: str,
        text: str,
        reply_to_item_id: str | None = None,
        reply_to_client_context: str | None = None,
    ) -> DirectMessage:
        cl = self._require_client()
        reply_to_message = None
        if reply_to_item_id:
            reply_to_message = DirectMessage(
                id=str(reply_to_item_id),
                timestamp=datetime.now(timezone.utc),
                client_context=reply_to_client_context or "",
            )
        try:
            return cl.direct_send(
                text,
                thread_ids=[int(thread_id)],
                reply_to_message=reply_to_message,
            )
        except TypeError:
            logger.warning("installed instagrapi does not support reply_to_message")
            return cl.direct_send(text, thread_ids=[int(thread_id)])

    def send_photo(self, thread_id: str, path: str | Path) -> DirectMessage:
        cl = self._require_client()
        return cl.direct_send_photo(Path(path), thread_ids=[int(thread_id)])

    def send_video(self, thread_id: str, path: str | Path) -> DirectMessage:
        cl = self._require_client()
        return cl.direct_send_video(Path(path), thread_ids=[int(thread_id)])

    def mark_seen(self, thread_id: str) -> None:
        cl = self._require_client()
        try:
            cl.direct_send_seen(int(thread_id))
        except Exception:
            logger.exception("failed to mark Instagram thread as seen")

    def invalidate_thread_cache(self) -> None:
        self._thread_id = None

    def _new_client(self) -> Client:
        cl = Client()
        cl.delay_range = [2, 5]
        if self.config.ig_proxy:
            cl.set_proxy(self.config.ig_proxy)
        if self.config.ig_device:
            cl.set_device(self.config.ig_device)
        return cl

    def _dump_settings(self, cl: Client) -> None:
        self.config.ig_session_file.parent.mkdir(parents=True, exist_ok=True)
        cl.dump_settings(self.config.ig_session_file)

    def _login_by_sessionid(self, cl: Client) -> None:
        sessionid = self._sessionid()
        if not sessionid:
            raise RuntimeError("IG_SESSIONID or cookie header is required for sessionid login")
        try:
            cl.login_by_sessionid(sessionid)
        except AssertionError as exc:
            raise RuntimeError("Instagram sessionid must be a raw cookie value starting with the numeric user id") from exc

    def _verification_code(self) -> str:
        if self.config.ig_verification_code:
            return self.config.ig_verification_code
        if self.config.ig_totp_secret:
            return _totp(self.config.ig_totp_secret)
        return ""

    def _sessionid(self) -> str | None:
        if self.config.ig_sessionid:
            return _parse_cookie_header(self.config.ig_sessionid).get("sessionid")
        cookies = _parse_cookie_header(self.config.ig_cookie_header or "")
        return cookies.get("sessionid")

    def _require_client(self) -> Client:
        if not self.cl:
            raise RuntimeError("Instagram client is not logged in")
        return self.cl


def is_challenge_error(exc: Exception) -> bool:
    return isinstance(exc, ChallengeRequired)


def is_login_required_error(exc: Exception) -> bool:
    return isinstance(exc, (LoginRequired, ClientLoginRequired))


def is_ip_checkpoint_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 467


def _totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    normalized = secret.replace(" ", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding)
    counter = int(time.time() // interval)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def _parse_cookie_header(header: str) -> dict[str, str]:
    header = header.strip()
    if not header:
        return {}
    if "=" not in header and ";" not in header:
        return {"sessionid": header}

    cookies: dict[str, str] = {}
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            cookies[key] = value
    return cookies
