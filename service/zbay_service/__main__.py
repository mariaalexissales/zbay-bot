"""Run the zBay bot.

    python -m zbay_service

Settings come from .env (see .env.example at the top of the repo), from the real
environment, which wins over the file, and from flags, which win over both.

Hosted, for many servers, is the default. Relays connect over HTTP on ZBAY_HOST and
ZBAY_PORT. Local, for one server, is ZBAY_LOCAL or --local: no relay and no HTTP, the bot
reads the game's bridge folder itself. That folder is on this machine, or, with
ZBAY_SFTP_HOST set, on a rented game host and read over SFTP.

The bot's token is ZBAY_DISCORD_TOKEN. There is no flag for it: a flag is on show in the
process list, and in a shell's history, for as long as either lasts.

    python -m zbay_service --check

says whether it has what it needs to start, and the first thing to fix if not, without
connecting to anything.
"""

import argparse
import asyncio
import os
import sys

from aiohttp import web

from . import env
from .api import make_app
from .bot import ZBayBot
from .check import report
from .local import Direct, relay_module
from .store import Store


# how long between looks at a folder that could not be read last time.
OUTAGE_SECONDS = 15


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


def open_bridge(args, zbay_relay):
    """The game's bridge folder: on this disk, or over SFTP when a host is named.

    Over SFTP the relay's own state file stays here, on the bot's machine. It is the
    bot's memory of where it got to, not something of the game's.
    """
    if not args.sftp_host:
        return zbay_relay.Bridge(args.zbay_dir)

    from .remote import SftpFolder

    folder = SftpFolder(args.sftp_host, args.sftp_port, args.sftp_user, args.sftp_password,
                        args.zbay_dir, args.sftp_fingerprint)
    return zbay_relay.Bridge(folder=folder, state_path=args.state)


async def run_local(args, token):
    """One game server: the bot reads its bridge folder itself.

    The one tenant is made on the spot. Its key is never used: nothing connects from
    outside. The file work runs off the event loop, so a slow disk or a slow host never
    stalls the bot. A folder that can't be read is said once, not on every pass, and
    looked at less often until it can be: nothing is lost meanwhile, since the place in
    the game's file only moves after the bot has what was read.
    """
    zbay_relay = relay_module()

    store = Store(args.db)
    bot = ZBayBot(store, setup_url=args.setup_url, guild_only=args.guild)
    hub = bot.hub

    if not hub.is_set_up(args.guild):
        hub.setup(args.guild)

    bridge = open_bridge(args, zbay_relay)
    direct = Direct(hub, args.guild)

    async def pump():
        await bot.wait_until_ready()
        print("reading " + str(bridge.folder))
        failing = False

        while not bot.is_closed():
            try:
                await asyncio.to_thread(zbay_relay.step, bridge, direct)
                if failing:
                    print("reading the game's folder again")
                    failing = False
            except Exception as err:
                if not failing:
                    print("can't read the game's folder, will keep trying: " + repr(err))
                    failing = True

            await asyncio.sleep(max(args.interval, OUTAGE_SECONDS) if failing else args.interval)

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
    """Flags over the environment over .env. Returns the settings and the token.

    The file is read before the flags are defined, because it supplies their defaults.
    With --check nothing is refused here: a check is for finding out everything that is
    missing, so it is not stopped at the first thing. The SFTP password, like the token,
    comes from the environment alone and has no flag; it rides on the settings as
    sftp_password.
    """
    environ = os.environ if environ is None else environ

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
                        help="local: the server's Zomboid/Lua/zBay folder, or its path on the sftp host [ZBAY_DIR]")
    parser.add_argument("--guild", default=environ.get("ZBAY_GUILD") or None,
                        help="local: the id of the one Discord server this is for [ZBAY_GUILD]")
    parser.add_argument("--interval", type=float, default=_number(environ, "ZBAY_INTERVAL", 2.0, float),
                        help="local: seconds between passes [ZBAY_INTERVAL]")
    parser.add_argument("--sftp-host", default=environ.get("ZBAY_SFTP_HOST") or None,
                        help="local: the game host's sftp address, when the folder is not on this machine "
                             "[ZBAY_SFTP_HOST]")
    parser.add_argument("--sftp-port", type=int, default=_number(environ, "ZBAY_SFTP_PORT", 22, int),
                        help="local: its sftp port [ZBAY_SFTP_PORT]")
    parser.add_argument("--sftp-user", default=environ.get("ZBAY_SFTP_USER") or None,
                        help="local: the sftp username [ZBAY_SFTP_USER]")
    parser.add_argument("--sftp-fingerprint", default=environ.get("ZBAY_SFTP_FINGERPRINT") or None,
                        help="local: the host key the sftp host must present, as SHA256:... [ZBAY_SFTP_FINGERPRINT]")
    parser.add_argument("--state", default=environ.get("ZBAY_STATE") or "zbay_relay_state.json",
                        help="local, over sftp: where the bot keeps its place in the game's files [ZBAY_STATE]")
    parser.add_argument("--check", action="store_true",
                        help="say whether it would start and what to fix if not, then stop")
    args = parser.parse_args(argv)
    args.sftp_password = environ.get("ZBAY_SFTP_PASSWORD") or None

    token = environ.get("ZBAY_DISCORD_TOKEN")

    if args.check:
        return args, token

    if not token:
        raise SystemExit("ZBAY_DISCORD_TOKEN isn't set. copy .env.example to .env and put the bot's token in it")

    if args.local and (not args.zbay_dir or not args.guild):
        raise SystemExit("local mode needs ZBAY_DIR and ZBAY_GUILD, or --zbay-dir and --guild")

    if args.local and args.sftp_host and not (args.sftp_user and args.sftp_password and args.sftp_fingerprint):
        raise SystemExit("a game folder over sftp needs ZBAY_SFTP_USER, ZBAY_SFTP_PASSWORD and "
                         "ZBAY_SFTP_FINGERPRINT. python -m zbay_service --check shows the fingerprint the host presents")

    return args, token


def main(argv=None):
    args, token = read_settings(argv)

    if args.check:
        sys.exit(report(args, token))

    if args.local:
        asyncio.run(run_local(args, token))
    else:
        asyncio.run(run_hosted(args, token))


if __name__ == "__main__":
    main()
