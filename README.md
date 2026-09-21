# instabridge

Personal Instagram DM ↔ Telegram bridge for a single dialog. Polls Instagram Direct through `instagrapi`, forwards messages and media to a Telegram chat, and sends replies back to Instagram when you reply to a forwarded message in Telegram.

Instagram has no public API for personal DMs, so this runs the private mobile API. Use a separate reader account, not your main one, and keep the poll interval slow.

## Requirements

- Docker and Docker Compose
- A Telegram bot ([@BotFather](https://t.me/BotFather)) and the target chat ID
- A separate Instagram account with access to the DM thread you want to bridge

## Setup

```bash
cp .env.example .env   # fill in credentials
docker compose up -d --build
```

State (`ig_session.json`, `bridge_state.db`) lives in `./data`, mounted into the container, so rebuilding or recreating it doesn't lose the Instagram session.

### Bare Python, without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m bridge.main
```

## Operating

```bash
./scripts/set-secret.sh IG_SESSIONID '...'   # update one .env value and restart
./scripts/restart.sh
./scripts/logs.sh
```

All three detect whether you're running under Docker or the bare-Python/systemd setup.

## Notes

- Instagram often restricts Direct API access from datacenter IPs even after a valid login. If polling starts failing with 403/467 on `direct_v2/*`, set `IG_PROXY` in `.env` to a residential/mobile proxy.
- If password login fails with "Your version of Instagram is out of date," get a fresh `sessionid` from a logged-in browser session and set it as `IG_SESSIONID` (or the full cookie header as `IG_COOKIE_TOKEN`).
- Messages use Telegram's HTML `parse_mode`; a multi-photo/video Instagram message forwards as one Telegram album instead of several separate messages.

## License

Personal project, no license file — do not assume you can redistribute this.
