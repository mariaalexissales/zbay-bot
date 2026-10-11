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

`--check` connects to nothing. It reads the settings the way a real start does and lists everything that has to be fixed first: a missing token, a `ZBAY_GUILD` that isn't a number, a `ZBAY_DIR` that isn't there, a `discord.json` that doesn't parse or has `enabled` off. It never prints the token. It can't tell you whether Discord will accept the token; the real start does that.

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

- `test_relay.py`: the relay against a bridge folder on disk. A line caught mid-write, a failed send, a restart, a backlog, an old file being removed, the inbox being cleared only once the mod has acknowledged all of it.
- `test_service.py`: the hub and the API with Discord played by a list. Events going to the feed, sending a batch twice, a newer boot, links, a balance question, a mailbox question, search, keys, and the real relay talking to the real API.
- `test_env.py`: reading `.env`, the real environment beating the file, a flag beating both, and what is said when the token or a local-mode setting is missing.
- `test_check.py`: `--check` on a setup that's ready, on one with everything wrong at once, and that the token never appears in what it prints.
- `test_bot.py`: every slash command, called the way Discord calls it with a stand-in for the interaction. Who may run what, what each one says, that all of it is said privately, and that a copy in local mode hands out no key.
- `test_with_mod.py`: the bot against the mod itself. It runs the mod's Lua behind the stub of the game its own tests use, and lets the relay carry what it writes to the hub and back. This is the one place that fails if the two repos stop agreeing on a file name or a field. It needs the mod at `~/Zomboid/Workshop/zBay` (or `ZBAY_MOD_DIR`), the harness in `estral-tools/zbay/harness` beside it, and `lupa`; without them it's skipped.

No test here talks to Discord. `test_bot.py` covers what each command decides and says; connecting, registering the commands, posting to a channel and sending a direct message are discord.py's side of `service/zbay_service/bot.py` and have not yet been run against a real Discord server.
