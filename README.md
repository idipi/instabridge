# Instagram -> Telegram Bridge

Personal Instagram DM bridge for one target dialog. It polls Instagram Direct with `instagrapi`, forwards inbound media/text to a Telegram chat through the Telegram Bot API, and lets you reply from Telegram by replying to forwarded messages.

## Current Reality Check

Instagram personal DMs are not exposed through a stable public API. This project uses the private mobile API through `instagrapi`, so use a separate reader account, keep polling slow, and expect occasional login challenges.

TikTok DMs are not implemented. TikTok's public developer surface currently covers products such as Login Kit, Share Kit, Content Posting, Data Portability, Display, and Research APIs, but not personal DM inbox bridging.

## Setup (Docker, preferred)

```bash
cp .env.example .env   # fill it in
docker compose up -d --build
```

State (`ig_session.json`, `bridge_state.db`) persists in `./data`, mounted into the
container — rebuilding or recreating the container does not lose your Instagram
session. `.env` itself is read at container start; it is never baked into the image.

## Setup (bare Python, alternative)

Use Python 3.10+. The current `instagrapi` release line no longer supports Python 3.9.

```bash
python3 --version
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`, then run:

```bash
.venv/bin/python -m bridge.main
```

## Operating it day to day

```bash
./scripts/set-secret.sh IG_SESSIONID '73998782341:XXXX:27:YYYY'  # update one .env value + restart, one command
./scripts/restart.sh                                              # just restart
./scripts/logs.sh                                                 # tail logs
```

All three auto-detect Docker vs. the bare-Python/systemd deployment.

If Instagram rejects password login from this host even with the correct password, set `IG_SESSIONID` in `.env` from a logged-in browser cookie and run again. You can also paste a full browser Cookie header into `IG_COOKIE_HEADER`; `IG_COOKIE_TOKEN` is accepted as an alias for that header or for the raw `sessionid`. The bridge extracts `sessionid` and uses `instagrapi.login_by_sessionid()` for this fallback. After a successful login it writes `ig_session.json`; remove the cookie env var afterwards.

## GCP/VPS Login Gotcha

Instagram often treats datacenter IPs as risky Android logins. If `accounts/login/` returns `BadPassword` while the password is definitely correct, assume the IP is the problem.

If `direct_v2/inbox/` returns HTTP `467`, the cookie was accepted far enough to identify the account, but Instagram rejected the server/IP for Direct access. Cookies alone will not fix that state.

Preferred fixes:

1. Use a stable residential or mobile proxy with `IG_PROXY`.
2. Run the bridge from a home network or route Instagram traffic through a home exit node.
3. Use `IG_SESSIONID` only as a fallback; it can still fail if Instagram dislikes the server IP.

Proxy examples:

```env
IG_PROXY=http://user:pass@host:port
IG_PROXY=socks5://user:pass@host:port
IG_PROXY=socks5://127.0.0.1:1080
```

## Telegram Usage

Reply to a forwarded Telegram message with text, photo, video, animation, or image/video document to send it back into the Instagram thread. Text replies are sent as real Instagram replies when `instagrapi` provides enough message context. A normal non-reply Telegram message in the bridge chat is sent as a standalone Instagram DM.

Instagram replies are mirrored back as Telegram replies when the original Instagram item is known to the bridge. Messages sent from Telegram are also mapped to their Instagram item ids so later Instagram replies can point back to the original Telegram message.

Every forwarded Instagram message/caption starts with a bold sender line such as `⬅️ @target · Instagram` or `➡️ @me · sent from Instagram`. Messages are sent with Telegram's HTML `parse_mode`; a multi-photo/video Instagram message (carousel) is forwarded as one Telegram album instead of separate messages.
