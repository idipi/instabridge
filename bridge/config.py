from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    tg_bot_token: str
    tg_chat_id: int
    ig_username: str
    ig_password: str
    ig_target_username: str
    poll_interval: int = 45
    initial_history: int = 10
    db_path: Path = ROOT_DIR / "bridge_state.db"
    ig_session_file: Path = ROOT_DIR / "ig_session.json"
    ig_device: dict[str, Any] | None = None
    ig_proxy: str | None = None
    ig_verification_code: str | None = None
    ig_totp_secret: str | None = None
    ig_sessionid: str | None = None
    ig_cookie_header: str | None = None
    ig_mark_seen: bool = False
    media_timeout: int = 30
    log_level: str = "INFO"


def load_config(env_path: str | Path = ROOT_DIR / ".env") -> Config:
    load_dotenv(env_path)

    config = Config(
        tg_bot_token=_required("TG_BOT_TOKEN"),
        tg_chat_id=_int("TG_CHAT_ID"),
        ig_username=_required("IG_USERNAME"),
        ig_password=_required("IG_PASSWORD"),
        ig_target_username=_required("IG_TARGET_USERNAME").lstrip("@").lower(),
        poll_interval=_int("POLL_INTERVAL", 45),
        initial_history=_int("INITIAL_HISTORY", 10),
        db_path=_path("DB_PATH", ROOT_DIR / "bridge_state.db"),
        ig_session_file=_path("IG_SESSION_FILE", ROOT_DIR / "ig_session.json"),
        ig_device=_json_dict("IG_DEVICE_JSON"),
        ig_proxy=_optional("IG_PROXY"),
        ig_verification_code=_optional("IG_VERIFICATION_CODE"),
        ig_totp_secret=_optional("IG_TOTP_SECRET"),
        ig_sessionid=_optional("IG_SESSIONID"),
        ig_cookie_header=_optional("IG_COOKIE_HEADER") or _optional("IG_COOKIE_TOKEN"),
        ig_mark_seen=_bool("IG_MARK_SEEN", False),
        media_timeout=_int("MEDIA_TIMEOUT", 30),
        log_level=_optional("LOG_LEVEL", "INFO").upper(),
    )

    if config.poll_interval < 30:
        raise ValueError("POLL_INTERVAL must be >= 30 to reduce Instagram rate-limit risk")
    if config.initial_history < 0:
        raise ValueError("INITIAL_HISTORY must be >= 0")
    return config


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required in .env")
    return value


def _optional(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int | None = None) -> int:
    raw = _optional(name, None if default is None else str(default))
    if raw is None:
        raise RuntimeError(f"{name} is required in .env")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = _optional(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _path(name: str, default: Path) -> Path:
    raw = _optional(name)
    return Path(raw).expanduser().resolve() if raw else default


def _json_dict(name: str) -> dict[str, Any] | None:
    raw = _optional(name)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be a valid JSON object") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return value
