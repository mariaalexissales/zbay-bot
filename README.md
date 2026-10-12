# zbay-bot

The Discord side of zBay, the Project Zomboid marketplace mod: one bot that serves many game servers, a relay that each server owner runs beside their game server, and the setup page that walks them through it.

```
mod (Lua)  <->  Zomboid/Lua/zBay/bridge/  <->  relay/zbay_relay.py  <-- HTTPS, Bearer serverKey -->  service  <->  Discord
```

The game can't open a network connection from Lua, so the mod writes to files and reads from files. A hosted bot can't read a game server's disk, so the relay sits between them. The file formats are documented in the mod's README under "Discord bridge".

| Folder | What it is | Needs |
|---|---|---|
| `relay/` | `zbay_relay.py`, the one file a server owner downloads and runs. | Python 3.9+, standard library only |
| `service/` | The bot and the API relays talk to. | `discord.py`, `aiohttp`, and `paramiko` for a game folder read over SFTP |
| `deploy/` | A systemd unit for a machine that stays on, and the container's entrypoint. `Dockerfile` and `fly.toml` at the top are for Fly.io. | |
| `site/` | The setup page. Static, no build. | |
| `tests/` | | `pytest`, `pytest-asyncio`, and `lupa` for the tests that run the mod itself |

## First run

The quickest way to see it work is local mode: the bot on the same machine as your game server, for one Discord server. Nothing to host, no relay, no key.

**In Discord**

1. In the developer portal, make a New Application and add a Bot. Leave the three privileged intents off.
2. On the Bot page, Reset Token and copy it. It goes in `.env` and nowhere else.
3. On General Information, copy the Application ID.
4. Invite the bot to a server you can test in, with your Application ID in place of `APP_ID`:
   `https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot+applications.commands&permissions=3072`
5. In Discord's settings, Advanced, turn on Developer Mode. Right-click that server and Copy Server ID.

**On the game server's machine**

6. Copy `.env.example` to `.env` and set `ZBAY_DISCORD_TOKEN`, `ZBAY_LOCAL=true`, `ZBAY_DIR` (the server's `Zomboid/Lua/zBay` folder) and `ZBAY_GUILD` (the id from step 5).
7. In that `zBay` folder, set `"enabled": true` in `discord.json`, then press Reload Settings in the zBay window or restart the game server.
8. Install, check, start:

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r service/requirements.txt
```

```bash
cd service
```

```bash
../.venv/Scripts/python -m zbay_service --check
```

```bash
../.venv/Scripts/python -m zbay_service
```

`--check` doesn't connect to Discord. It reads the settings the way a real start does and lists everything that has to be fixed first: a missing token, a `ZBAY_GUILD` that isn't a number, a `ZBAY_DIR` that isn't there, a `discord.json` that doesn't parse or has `enabled` off. It never prints the token. It can't tell you whether Discord will accept the token; the real start does that.

Then, in Discord: `/zbay channel` to pick where sales are announced, and sell something in game. In local mode `/zbay setup` has nothing to do and says so.

Players have `/zbay link` to tie their Discord account to their character, then `/zbay balance` and `/zbay mailbox` for what's in their bank and their mail, and `/zbay search`, `/zbay listings` and `/zbay auctions` for the market. Every answer is shown only to whoever asked.

## Running it

Settings live in `.env`. Copy `.env.example` to `.env` and fill it in; every setting is explained there.

```bash
cd service
../.venv/Scripts/python -m zbay_service
```

- `.env` is git-ignored. `.env.example` is not, so it never holds a real value.
- The bot reads `.env` from the folder it's run in, or failing that from the top of this repo. `--env-file` names another.
- Three layers, later ones winning: `.env`, then the real environment, then command-line flags. So a host that sets `ZBAY_DISCORD_TOKEN` itself needs no file at all, and a file left lying around can't override it.
- Every setting but the token has a flag (`python -m zbay_service --help`). The token has none on purpose: a flag shows in the process list and in shell history.
- Values are taken as written. No quotes needed, and a backslash stays a backslash, so a Windows path goes in as it is.

### For one server, on the machine the game server runs on

`ZBAY_LOCAL=true`, with `ZBAY_DIR` and `ZBAY_GUILD`. No relay and no HTTP: the bot reads the bridge folder itself, so this is also how to try the whole thing before anything is hosted.

`ZBAY_GUILD` is the id of the one Discord server this copy is for. Its slash commands register there at once instead of taking up to an hour. Set `"enabled": true` in the mod's `discord.json`; `endpoint` and `serverKey` aren't used in this mode.

### For one server on a rented game host

A rented host gives you the server's files over SFTP and no way to run a program beside the game. So the bot runs somewhere else and reads the folder over SFTP. It is local mode with five more settings, all from your host's panel:

```
ZBAY_LOCAL=true
ZBAY_GUILD=<your Discord server's id>
ZBAY_DIR=/Zomboid/Lua/zBay
ZBAY_SFTP_HOST=<the sftp address>
ZBAY_SFTP_PORT=2022
ZBAY_SFTP_USER=<the sftp username>
ZBAY_SFTP_PASSWORD=<the sftp password>
ZBAY_SFTP_FINGERPRINT=SHA256:...
```

- **`ZBAY_DIR` is the path as SFTP shows it**, not as the game sees it. Hosts differ: `/Zomboid/Lua/zBay`, `/.cache/Lua/zBay`, or under a `server-data` folder. `--check` lists what is on the host when the path is wrong.
- **The fingerprint pins the host.** Run `--check` with it empty: it prints the key the host presents and signs in to nothing. Compare that with your panel if the panel shows one, then put it in `.env`. From then on the password is only ever sent to a host presenting that key. If your host changes its key the bot stops and says so.
- **In the game's folder**, through the panel's file manager, set `"enabled": true` in `discord.json`, then press Reload Settings in the zBay window or restart the server.
- **The bot's place in the game's files** is kept in `ZBAY_STATE`, on the bot's machine. Nothing but the mod's own files is written on the host.
- `ZBAY_INTERVAL=5` is plenty. A command from Discord reaches the game at the mod's next poll, a few seconds later.
- The bot only connects outwards, to Discord and to the game host. The machine it is on needs no open port, address or certificate.

When the host can't be reached the bot says so once, keeps trying, and says when it is reading again. Nothing is lost in between: its place in the game's file only moves once it has what it read.

### Hosted, for many servers

The default. Relays connect on `ZBAY_HOST` and `ZBAY_PORT`. Put it behind something that terminates TLS. Each Discord server pairs itself with `/zbay setup`, which shows an admin a server key once. Only the key's hash is stored, so a copy of the database isn't a set of working keys. Kicking the bot from a server deletes that server's pairing, links and queued commands.

### The Discord application

Create one in the Discord developer portal with a bot user. It needs no privileged intents: the bot asks for `guilds` only, and never sees message content or the member list. The invite needs the `bot` and `applications.commands` scopes with View Channels and Send Messages (permissions `3072`), which is the link the setup page builds from `site/config.js`.

## Hosting it

For one server, the bot is a small Python program that has to stay running. Anything that stays on will do: a spare PC, a Raspberry Pi, a small cloud VM. `deploy/zbay-bot.service` runs it under systemd on Linux and starts it again if it stops.

On the machine, with the repo copied to `/opt/zbay-bot` (leave `.venv` and `.env` behind when copying; both are made fresh). Ubuntu's cloud images come without the venv module, so that first:

```bash
sudo apt update && sudo apt install -y python3-venv
```

```bash
sudo useradd --system --home /opt/zbay-bot zbay
```

```bash
python3 -m venv /opt/zbay-bot/.venv
```

```bash
/opt/zbay-bot/.venv/bin/python -m pip install -r /opt/zbay-bot/service/requirements.txt
```

Write `/opt/zbay-bot/.env` from `.env.example`, then keep it to the bot's user, since it holds the token and the SFTP password:

```bash
sudo chown -R zbay: /opt/zbay-bot && sudo chmod 600 /opt/zbay-bot/.env
```

Check it, as the user it will run as:

```bash
cd /opt/zbay-bot/service && sudo -u zbay ../.venv/bin/python -m zbay_service --check
```

Then install the service, with the four commands at the top of `deploy/zbay-bot.service`.

### On Fly.io

One small Machine and a 1 GB volume, a little over $2 a month. Fly has no free tier for new accounts and wants a card on file once its short trial ends. `Dockerfile` and `fly.toml` are ready; nothing about your server is in either.

1. **One settings file for this server.** Copy `.env.example` to `fly.env` (git-ignored) and fill it in as under "For one server on a rented game host". Leave `ZBAY_DB` and `ZBAY_STATE` as they are: relative paths land on the volume. If the SFTP password has a `#` in it, put it in double quotes.

2. **Check it from your own machine first.** This is where the fingerprint and the path get sorted out, before anything is deployed:

```bash
cd service && ../.venv/bin/python -m zbay_service --env-file ../fly.env --check
```

3. **Install `flyctl` and sign in**, from Fly's own instructions. Then, at the top of this repo, make the app without deploying it. The name has to be unique across Fly; the region is a three-letter code such as `iad`:

```bash
fly launch --copy-config --no-deploy --ha=false --no-public-ips --name <name> --region <region>
```

4. **Hand it the settings**, as secrets, from that same file:

```bash
fly secrets import --stage < fly.env
```

5. **Deploy**, and watch it start:

```bash
fly deploy --ha=false
```

```bash
fly logs
```

What the log says, in order, when all is well:

```
local mode: reading sftp://<user>@<host>:<port>/<path> for discord server <id>
... logging in using static token
... Shard ID None has connected to Gateway
zbay bot ready as <name> in 1 server(s)
connected to the game's folder: 1 file(s) from the game, newest from boot <n>
[<id>] the game server says hello: world '<world>', boot <n>
20:14:03  2 event(s) from the game, snapshot, 0 command(s) to the game
```

- **`the game server says hello`** is the game itself speaking. It is the one line that proves the bot is connected to a running server with the bridge on.
- After that there is one line for every pass that carried something, so a quiet log is a quiet market.
- `nothing from the game in ... yet` means the bot reached the folder and the game has written nothing: `enabled` isn't true in `discord.json`, or the server hasn't been reloaded since.
- `can't read the game's folder, will keep trying` is the SFTP side: the reason follows it. `reading the game's folder again` is it coming back.
- `local mode needs ZBAY_DIR and ZBAY_GUILD` and the bot restarting over and over means `fly.env` was imported with those empty.

- **One Machine, always.** `--ha=false` matters on every deploy: a second Machine would be a second copy of the bot on the same token, and the two would both answer every command.
- **The volume is the bot's memory**: its database and its place in the game's files. Without it the bot would read the game's file from the top at every restart and announce all of it again. Deleting the app deletes the volume.
- **To change a setting**, edit `fly.env` and run the import again without `--stage`; it restarts the bot with the new values. **To update the bot**, `fly deploy --ha=false` again.
- In PowerShell the import is `Get-Content fly.env | fly secrets import --stage`.
- The container starts as root only long enough to hand the volume to its own user (`deploy/entrypoint.py`), and runs the bot as that user.

## The relay API

Every request carries `Authorization: Bearer <serverKey>`. The key decides which Discord server a request is for; nothing in a body can say otherwise.

| Call | Body | Answer |
|---|---|---|
| `POST /v1/events` | `{"boot": 1790000000, "records": [...]}`: lines from the mod's out file, in order | `{"ok": true, "seq": n}` |
| `PUT /v1/snapshot` | the mod's `snapshot.json` as it is | `{"ok": true}` |
| `GET /v1/commands?after=<id>&wait=<seconds>` | | `{"commands": [{"id": 7, "type": "link_offer", ...}]}` |
| `GET /healthz` | | `{"ok": true}` |

- **Events are safe to send twice.** The mod numbers its records and never reuses a number, so the service drops anything at or below the last `seq` it has for that boot. A newer `boot` is always listened to from its first record, which is what makes a rolled-back or wiped world work.
- **Asking for commands is the receipt for older ones.** `after=7` deletes everything up to 7. A relay that crashed before writing a batch asks again with the old number and gets it again.
- `wait` holds the request open for up to 25 seconds until a command arrives. The relay as written polls every two seconds instead; the option is there for one that wants to hold a connection.
- The relay only moves its place in the out file after a send has landed, and removes an out file once it is drained and a newer boot's file exists.

## The setup page

`site/` is three files and no build step: `index.html`, `config.js`, and `zbay_relay.py`, which has to be copied in from `relay/` when it's published so the download link has something to point at.

Fill in `site/config.js` first: the application's client id for the invite button, and the hosted bot's address, which goes into the `discord.json` the page writes for an owner. The page builds that file in the browser; the server key an owner pastes in is never sent anywhere.

### Publishing it

The page tells a server owner to run a relay against the hosted bot, so it waits until that bot has an address. Local mode needs no page.

It goes up by direct upload to Cloudflare Pages. No GitHub repo is connected, so nothing about a GitHub account is involved: a `*.github.io` address would carry the account's handle, and so would a CNAME pointing at one.

1. Fill in `site/config.js`: `clientId` is the Application ID, `endpoint` is the hosted bot's public address.
2. Sign in once. It opens a browser for you to approve:

```bash
npx wrangler login
```

3. Copy the relay in beside the page, so the download link has something to give:

```bash
cp relay/zbay_relay.py site/
```

4. Upload. The project name becomes `<name>.pages.dev`:

```bash
npx wrangler pages deploy site --project-name <name>
```

5. Put the page's address in `ZBAY_SETUP_URL`, so `/zbay setup` points admins at it.

Run steps 3 and 4 again whenever the page or the relay changes. `site/zbay_relay.py` is git-ignored: the one in `relay/` is the real one.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

- `test_relay.py`: the relay against a bridge folder, every test twice: on disk, and on an SFTP host played in memory (`folders.py`). A line caught mid-write, a failed send, a restart, a backlog, an old file being removed, the inbox being cleared only once the mod has acknowledged all of it.
- `test_service.py`: the hub and the API with Discord played by a list. Events going to the feed, sending a batch twice, a newer boot, links, a balance question, a mailbox question, search, keys, and the real relay talking to the real API.
- `test_env.py`: reading `.env`, the real environment beating the file, a flag beating both, and what is said when the token or a local-mode setting is missing.
- `test_check.py`: `--check` on a setup that's ready, on one with everything wrong at once, and that the token never appears in what it prints. For a rented host: the fingerprint shown before anything is signed in to, a host presenting another key, a wrong path, a refused sign-in, and that the password is never printed.
- `test_remote.py`: what is particular to a folder over SFTP. A dropped connection never taken for a file having gone, a new session after one, and the password never sent to a host showing the wrong key.
- `test_remote_loopback.py`: the same code against a real SFTP server started on a local port, so the handshake, the host key, the sign-in and every file operation cross a real wire. What it can't show is that a given game host's SFTP server behaves the same.
- `test_bot.py`: every slash command, called the way Discord calls it with a stand-in for the interaction. Who may run what, what each one says, that all of it is said privately, and that a copy in local mode hands out no key.
- `test_with_mod.py`: the bot against the mod itself. It runs the mod's Lua behind the stub of the game its own tests use, and lets the relay carry what it writes to the hub and back. This is the one place that fails if the two repos stop agreeing on a file name or a field. It needs the mod at `~/Zomboid/Workshop/zBay` (or `ZBAY_MOD_DIR`), the harness in `estral-tools/zbay/harness` beside it, and `lupa`; without them it's skipped.

No test here talks to Discord or to a real game host. `test_bot.py` covers what each command decides and says; connecting, registering the commands, posting to a channel and sending a direct message are discord.py's side of `service/zbay_service/bot.py` and have not yet been run against a real Discord server.
