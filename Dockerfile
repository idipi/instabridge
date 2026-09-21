FROM python:3.13-slim

# UID/GID 1000 matches the default first non-root user on most Linux hosts, so a
# bind-mounted ./data volume is writable without extra chown gymnastics on the host.
RUN groupadd --gid 1000 instabridge && useradd --uid 1000 --gid instabridge --create-home instabridge

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=instabridge:instabridge bridge/ ./bridge/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/bridge_state.db \
    IG_SESSION_FILE=/data/ig_session.json

RUN mkdir -p /data && chown instabridge:instabridge /data
VOLUME ["/data"]

USER instabridge

ENTRYPOINT ["python", "-m", "bridge.main"]
