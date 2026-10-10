"""--check: would this start, and if not, what is the first thing to fix.

Reads the same settings a real start does and looks at what they point to. It connects
to nothing, so it says whether the bot has what it needs, not whether Discord will take
the token. The token is never printed, in whole or in part.
"""

import json
from pathlib import Path

from .local import relay_module


def _local(args, notes, problems):
    notes.append("mode: local, for one game server on this machine")

    if not args.guild:
        problems.append("ZBAY_GUILD isn't set. it is the id of your Discord server: turn on Developer Mode "
                        "in Discord, right-click the server, Copy Server ID")
    elif not str(args.guild).isdigit():
        problems.append("ZBAY_GUILD should be the server's id, which is all digits, and is " + repr(args.guild))
    else:
        notes.append("discord server: " + str(args.guild))

    if not args.zbay_dir:
        problems.append("ZBAY_DIR isn't set. it is the game server's Zomboid/Lua/zBay folder")
        return

    folder = Path(args.zbay_dir)
    if not folder.is_dir():
        problems.append("ZBAY_DIR isn't a folder: " + str(folder) + ". the mod makes it the first time "
                        "the game server starts with zBay installed")
        return

    notes.append("game folder: " + str(folder))
    relay = relay_module()

    settings = folder / "discord.json"
    if not settings.is_file():
        problems.append("no discord.json in " + str(folder) + ". start the game server once with zBay installed")
    else:
        try:
            data = json.loads(relay.strip_json_comments(settings.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
            problems.append("discord.json doesn't parse: " + str(err))
        else:
            if isinstance(data, dict) and data.get("enabled") is True:
                notes.append('discord.json: "enabled" is true')
            else:
                problems.append('"enabled" isn\'t true in discord.json, so the game writes nothing for the bot. '
                                "set it to true, then press Reload Settings in the zBay window or restart the server")

    files = relay.Bridge(folder).out_files()
    if files:
        notes.append("bridge: the game has written " + str(len(files)) + " file(s), newest from boot " + str(files[-1][0]))
    else:
        # not a problem: it is what a server looks like before its first start with the bridge on.
        notes.append("bridge: nothing from the game yet. it writes on its first start with the bridge on")


def check(args, token):
    """Returns (would it start, what was found, what has to be fixed)."""
    notes, problems = [], []

    if token:
        notes.append("token: set")
    else:
        problems.append("ZBAY_DISCORD_TOKEN isn't set. copy .env.example to .env and put the bot's token in it")

    if args.local:
        _local(args, notes, problems)
    else:
        notes.append("mode: hosted, for many game servers")
        notes.append("relays connect on " + str(args.host) + ":" + str(args.port))

    notes.append("database: " + str(args.db))

    return not problems, notes, problems


def report(args, token):
    """Print it. Returns the exit code."""
    ok, notes, problems = check(args, token)

    for note in notes:
        print("  ok   " + note)
    for problem in problems:
        print("  FIX  " + problem)

    print("ready to start" if ok else "not ready: " + str(len(problems)) + " thing(s) to fix")
    return 0 if ok else 1
