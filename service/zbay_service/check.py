"""--check: would this start, and if not, what is the first thing to fix.

Reads the same settings a real start does and looks at what they point to. It does not
connect to Discord, so it says whether the bot has what it needs, not whether Discord
will take the token. With the game's folder on an SFTP host it does connect there, since
that is the only way to look. Neither the token nor a password is ever printed, in whole
or in part.
"""

import json
from pathlib import Path

from .local import relay_module


def _remote(args, notes, problems):
    """The game's folder on an SFTP host, signed in to, or None with the reason added.

    The host's fingerprint is looked at first, without signing in. Until the pinned one
    matches it the password is not sent, here or in a real start.
    """
    where = str(args.sftp_host) + ":" + str(args.sftp_port)

    unset = [name for name, value in (("ZBAY_SFTP_USER", args.sftp_user),
                                      ("ZBAY_SFTP_PASSWORD", args.sftp_password)) if not value]
    for name in unset:
        problems.append(name + " isn't set. it is in your game host's panel, beside the sftp address")

    try:
        from . import remote
    except ImportError:
        problems.append("the sftp library isn't installed. run pip install -r service/requirements.txt")
        return None

    try:
        shown = remote.presented(args.sftp_host, args.sftp_port)
    except OSError as err:
        problems.append("can't reach the game host's sftp at " + where + ": " + str(err))
        return None

    if not args.sftp_fingerprint:
        problems.append("ZBAY_SFTP_FINGERPRINT isn't set. " + where + " presents " + shown + ". if your host's "
                        "panel shows a fingerprint, see that it is this one, then put it in .env")
        return None
    if not remote.same_fingerprint(shown, args.sftp_fingerprint):
        problems.append(where + " presents " + shown + ", which is not the one in ZBAY_SFTP_FINGERPRINT. if your "
                        "host has changed its key, update .env. if it hasn't, something is sitting in between")
        return None

    notes.append("sftp: " + where + " is the host in ZBAY_SFTP_FINGERPRINT")
    if unset:
        return None

    folder = remote.SftpFolder(args.sftp_host, args.sftp_port, args.sftp_user, args.sftp_password,
                               args.zbay_dir, args.sftp_fingerprint)
    try:
        there = folder.listing(folder.root)
        top = folder.listing("/") if there is None else None
    except OSError as err:
        problems.append("couldn't sign in to " + where + " as " + str(args.sftp_user) + ": " + str(err))
        folder.close()
        return None

    if there is None:
        problems.append("there is no " + folder.root + " on the game host. ZBAY_DIR is the path to Lua/zBay as "
                        "sftp shows it. at the top there is: " + (", ".join((top or [])[:20]) or "nothing"))
        folder.close()
        return None

    notes.append("game folder: " + str(folder))
    return folder


def _game(relay, folder, notes, problems):
    """What the game has put in its folder: its settings file, and whether it has written
    anything yet. Nothing yet is a note and not a problem: it is what a server looks like
    before its first start with the bridge on."""
    raw = folder.read("discord.json")

    if raw is None:
        problems.append("no discord.json in " + str(folder) + ". start the game server once with zBay installed")
    else:
        try:
            data = json.loads(relay.strip_json_comments(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            problems.append("discord.json doesn't parse: " + str(err))
        else:
            if isinstance(data, dict) and data.get("enabled") is True:
                notes.append('discord.json: "enabled" is true')
            else:
                problems.append('"enabled" isn\'t true in discord.json, so the game writes nothing for the bot. '
                                "set it to true, then press Reload Settings in the zBay window or restart the server")

    files = relay.out_files(folder)
    if files:
        notes.append("bridge: the game has written " + str(len(files)) + " file(s), newest from boot " + str(files[-1][0]))
    else:
        notes.append("bridge: nothing from the game yet. it writes on its first start with the bridge on")


def _local(args, notes, problems):
    """What local mode needs: the one Discord server, and the game's folder, on this disk
    or on an SFTP host."""
    notes.append("mode: local, for one game server on a rented host, read over sftp" if args.sftp_host
                 else "mode: local, for one game server on this machine")

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

    relay = relay_module()

    if args.sftp_host:
        folder = _remote(args, notes, problems)
        if folder is None:
            return
    else:
        path = Path(args.zbay_dir)
        if not path.is_dir():
            problems.append("ZBAY_DIR isn't a folder: " + str(path) + ". the mod makes it the first time "
                            "the game server starts with zBay installed")
            return

        notes.append("game folder: " + str(path))
        folder = relay.Folder(path)

    try:
        _game(relay, folder, notes, problems)
    except OSError as err:
        problems.append("couldn't read the game's folder: " + str(err))
    finally:
        if args.sftp_host:
            folder.close()


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
