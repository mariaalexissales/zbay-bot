"""Run the zBay bot.

    python -m zbay_service

Settings come from .env (see .env.example at the top of the repo), from the real
environment, which wins over the file, and from flags, which win over both.

Hosted, for many servers, is the default. Relays connect over HTTP on ZBAY_HOST and
ZBAY_PORT. Local, for one server on the machine the game server runs on, is ZBAY_LOCAL
or --local: no relay and no HTTP, the bot reads the bridge folder itself.

The bot's token is ZBAY_DISCORD_TOKEN. There is no flag for it: a flag is on show in the
process list, and in a shell's history, for as long as either lasts.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web

from . import env
from .api import make_app
from .bot import ZBayBot
from .local import Direct
from .store import Store


async def run_hosted(args, token):
    bot = ZBayBot(Store(args.db), setup_url=args.setup_url)

    runner = web.AppRunner(make_app(bot.hub))
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    print(f"relay api on {args.host}:{args.port}")

    try:
        await bot.start(token)
    finally:
        await runner.cleanup()


async def run_local(args, token):
    # the relay is one file meant to be handed out on its own, so it is imported from
    # where it sits rather than copied into this package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "relay"))
    import zbay_relay

    store = Store(args.db)
    bot = ZBayBot(store, setup_url=args.setup_url, guild_only=args.guild)
    hub = bot.hub

    # one tenant, made on the spot. its key is never used: nothing connects from outside.
    if not hub.is_set_up(args.guild):
        hub.setup(args.guild)

    bridge = zbay_relay.Bridge(args.zbay_dir)
    direct = Direct(hub, args.guild)

    async def pump():
        await bot.wait_until_ready()
        print("reading " + str(bridge.dir))
        while not bot.is_closed():
            try:
                # file work off the event loop, so a slow disk never stalls the bot.
                await asyncio.to_thread(zbay_relay.step, bridge, direct)
            except Exception as err:
                print("bridge pass failed: " + repr(err))
            await asyncio.sleep(args.interval)

    async with bot:
        task = asyncio.create_task(pump())
        try:
            await bot.start(token)
        finally:
            task.cancel()


def _number(environ, name, fallback, kind):
    raw = environ.get(name)
    if raw in (None, ""):
        return fallback
    try:
        return kind(raw)
    except ValueError:
        raise SystemExit(name + " should be a number, and is " + repr(raw))


def read_settings(argv=None, environ=None, cwd=None):
    """Flags over the environment over .env. Returns the settings and the token."""
    environ = os.environ if environ is None else environ

    # the file has to be read before the flags are defined: it supplies their defaults.
    first = argparse.ArgumentParser(add_help=False)
    first.add_argument("--env-file")
    named = first.parse_known_args(argv)[0].env_file

    path = env.find(named, cwd)
    if named and path is None:
        raise SystemExit("no settings file at " + named)
    if path:
        env.load(path, environ)

    parser = argparse.ArgumentParser(prog="zbay_service", description="The zBay Discord bot.")
    parser.add_argument("--env-file", help="settings file to read (default: .env here, or at the top of the repo)")
    parser.add_argument("--db", default=environ.get("ZBAY_DB") or "zbay.sqlite",
                        help="where pairings and links are kept [ZBAY_DB]")
    parser.add_argument("--setup-url", default=environ.get("ZBAY_SETUP_URL") or None,
                        help="the setup page, shown to admins by /zbay setup [ZBAY_SETUP_URL]")
    parser.add_argument("--host", default=environ.get("ZBAY_HOST") or "127.0.0.1",
                        help="hosted: address the relay api listens on [ZBAY_HOST]")
    parser.add_argument("--port", type=int, default=_number(environ, "ZBAY_PORT", 8080, int),
                        help="hosted: port the relay api listens on [ZBAY_PORT]")
    parser.add_argument("--local", action=argparse.BooleanOptionalAction, default=env.flag(environ.get("ZBAY_LOCAL")),
                        help="serve one game server on this machine [ZBAY_LOCAL]")
    parser.add_argument("--zbay-dir", default=environ.get("ZBAY_DIR") or None,
                        help="local: the server's Zomboid/Lua/zBay folder [ZBAY_DIR]")
    parser.add_argument("--guild", default=environ.get("ZBAY_GUILD") or None,
                        help="local: the id of the one Discord server this is for [ZBAY_GUILD]")
    parser.add_argument("--interval", type=float, default=_number(environ, "ZBAY_INTERVAL", 2.0, float),
                        help="local: seconds between passes [ZBAY_INTERVAL]")
    args = parser.parse_args(argv)

    token = environ.get("ZBAY_DISCORD_TOKEN")
    if not token:
        raise SystemExit("ZBAY_DISCORD_TOKEN isn't set. copy .env.example to .env and put the bot's token in it")

    if args.local and (not args.zbay_dir or not args.guild):
        raise SystemExit("local mode needs ZBAY_DIR and ZBAY_GUILD, or --zbay-dir and --guild")

    return args, token


def main(argv=None):
    args, token = read_settings(argv)

    if args.local:
        asyncio.run(run_local(args, token))
    else:
        asyncio.run(run_hosted(args, token))


if __name__ == "__main__":
    main()
