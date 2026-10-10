# zbay-bot

The Discord side of zBay, the Project Zomboid marketplace mod: one bot that serves many game servers, a relay that each server owner runs beside their game server, and the setup page that walks them through it.

```
mod (Lua)  <->  Zomboid/Lua/zBay/bridge/  <->  relay/zbay_relay.py  <-- HTTPS, Bearer serverKey -->  service  <->  Discord
```

The game can't open a network connection from Lua, so the mod writes to files and reads from files. A hosted bot can't read a game server's disk, so the relay sits between them. The file formats are documented in the mod's README under "Discord bridge".

| Folder | What it is | Needs |
|---|---|---|
| `relay/` | `zbay_relay.py`, the one file a server owner downloads and runs. | Python 3.9+, standard library only |
| `service/` | The bot and the API relays talk to. | `discord.py`, `aiohttp` |
| `site/` | The setup page. Static, no build. | |
| `tests/` | | `pytest`, `pytest-asyncio`, and `lupa` for the tests that run the mod itself |

## Running it

Settings live in `.env`. Copy `.env.example` to `.env` and fill it in; every setting is explained there.

```bash
pip install -r service/requirements.txt
cd service
python -m zbay_service
```

- `.env` is git-ignored. `.env.example` is not, so it never holds a real value.
- The bot reads `.env` from the folder it's run in, or failing that from the top of this repo. `--env-file` names another.
- Three layers, later ones winning: `.env`, then the real environment, then command-line flags. So a host that sets `ZBAY_DISCORD_TOKEN` itself needs no file at all, and a file left lying around can't override it.
- Every setting but the token has a flag (`python -m zbay_service --help`). The token has none on purpose: a flag shows in the process list and in shell history.
- Values are taken as written. No quotes needed, and a backslash stays a backslash, so a Windows path goes in as it is.

### For one server, on the machine the game server runs on

`ZBAY_LOCAL=true`, with `ZBAY_DIR` and `ZBAY_GUILD`. No relay and no HTTP: the bot reads the bridge folder itself, so this is also how to try the whole thing before anything is hosted.

`ZBAY_GUILD` is the id of the one Discord server this copy is for. Its slash commands register there at once instead of taking up to an hour. Set `"enabled": true` in the mod's `discord.json`; `endpoint` and `serverKey` aren't used in this mode.

### Hosted, for many servers

The default. Relays connect on `ZBAY_HOST` and `ZBAY_PORT`. Put it behind something that terminates TLS. Each Discord server pairs itself with `/zbay setup`, which shows an admin a server key once. Only the key's hash is stored, so a copy of the database isn't a set of working keys. Kicking the bot from a server deletes that server's pairing, links and queued commands.

### The Discord application

Create one in the Discord developer portal with a bot user. It needs no privileged intents: the bot asks for `guilds` only, and never sees message content or the member list. The invite needs the `bot` and `applications.commands` scopes with View Channels and Send Messages (permissions `3072`), which is the link the setup page builds from `site/config.js`.

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

Publishing it without the repo's owner showing: keep this repo private and publish `site/` with Cloudflare Pages, which builds from a private GitHub repo and serves it at a `*.pages.dev` address or a custom domain. Build command `cp relay/zbay_relay.py site/`, output directory `site`. A `*.github.io` address would carry the account's handle, and so would a CNAME pointing at one.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

- `test_relay.py`: the relay against a bridge folder on disk. A line caught mid-write, a failed send, a restart, a backlog, an old file being removed, the inbox being cleared only once the mod has acknowledged all of it.
- `test_service.py`: the hub and the API with Discord played by a list. Events going to the feed, sending a batch twice, a newer boot, links, a balance question, search, keys, and the real relay talking to the real API.
- `test_env.py`: reading `.env`, the real environment beating the file, a flag beating both, and what is said when the token or a local-mode setting is missing.
- `test_with_mod.py`: the bot against the mod itself. It runs the mod's Lua behind the stub of the game its own tests use, and lets the relay carry what it writes to the hub and back. This is the one place that fails if the two repos stop agreeing on a file name or a field. It needs the mod at `~/Zomboid/Workshop/zBay` (or `ZBAY_MOD_DIR`), the harness in `estral-tools/zbay/harness` beside it, and `lupa`; without them it's skipped.

The Discord half, `service/zbay_service/bot.py`, has no tests that talk to Discord. Everything it does it asks the hub for, and the hub is what's tested. It has not yet been run against a real Discord server.
