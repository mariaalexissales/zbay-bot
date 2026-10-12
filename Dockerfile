# zBay's Discord bot as a container, for a host that runs one: Fly.io is the one written up
# in the README. It listens on nothing. It keeps its database and its place in the game's
# files in /data, which wants to be a volume, or both are lost at every restart.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/service

WORKDIR /app

COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt

# the relay is not copied into the service: the bot imports it from where it sits.
COPY relay relay
COPY service service
COPY deploy/entrypoint.py /entrypoint.py

RUN useradd --system --no-create-home zbay && mkdir /data

# relative paths in the settings land here, so the default database and state file are on
# the volume without anybody having to say so. the entrypoint hands this folder to the
# bot's user and starts the command as that user.
WORKDIR /data

# --local, and as a flag: a flag wins over the settings, and the settings file a container
# is handed is often .env.example filled in, which says ZBAY_LOCAL=false. without it the bot
# would start hosted, wait for relays that cannot reach it, and never read a game folder.
ENTRYPOINT ["python", "/entrypoint.py"]
CMD ["python", "-m", "zbay_service", "--local"]
