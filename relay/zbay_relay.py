#!/usr/bin/env python3
"""Carry zBay's Discord bridge between a Project Zomboid server and the zBay bot.

The game can't open a network connection from Lua, so the zBay mod talks to the
outside world through files in Zomboid/Lua/zBay/bridge/. This program runs beside
the game server, reads what the mod writes, sends it to the bot, and writes what
the bot sends back.

    python zbay_relay.py --zbay-dir "C:\\Users\\you\\Zomboid\\Lua\\zBay"

It reads "endpoint" and "serverKey" from discord.json in that folder, the same
file the mod reads its switches from. Standard library only: nothing to install.

To have commands from Discord acted on at once instead of at the mod's next poll,
give it the server's RCON details. The password comes from the environment and is
never written anywhere:

    set ZBAY_RCON_PASSWORD=...
    python zbay_relay.py --zbay-dir ... --rcon-port 27015
"""

import argparse
import json
import os
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = 1

# how many records go to the bot in one request. a server back from a long outage has a
# backlog, and it is sent in pieces rather than as one body the bot might refuse.
BATCH = 200


def strip_json_comments(text):
    """discord.json as the mod writes it has // comments and may have trailing commas.

    A character scan, not a regex: a naive strip eats the slashes in "https://". A comma
    is trailing when the next thing that isn't whitespace or a comment closes the container.
    """
    if text.startswith("\ufeff"):
        text = text[1:]

    out = []
    i, n = 0, len(text)
    in_string = escaped = False

    while i < n:
        c = text[i]

        if in_string:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            stop = text.find("\n", i)
            if stop == -1:
                break
            i = stop
        elif text.startswith("/*", i):
            stop = text.find("*/", i + 2)
            if stop == -1:
                break
            i = stop + 2
        elif c == ",":
            j = i + 1
            while j < n:
                if text[j].isspace():
                    j += 1
                elif text.startswith("//", j):
                    stop = text.find("\n", j)
                    j = n if stop == -1 else stop + 1
                elif text.startswith("/*", j):
                    stop = text.find("*/", j + 2)
                    j = n if stop == -1 else stop + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1

    return "".join(out)


def read_settings(zbay_dir):
    path = Path(zbay_dir) / "discord.json"
    try:
        settings = json.loads(strip_json_comments(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise SystemExit("no discord.json in " + str(zbay_dir) + ". start the game server once so the mod writes it")
    except json.JSONDecodeError as err:
        raise SystemExit("discord.json doesn't parse: " + str(err))

    endpoint = str(settings.get("endpoint") or "").rstrip("/")
    key = str(settings.get("serverKey") or "")
    if not endpoint or not key:
        raise SystemExit('discord.json needs "endpoint" and "serverKey". the setup page says what goes in them')
    if not settings.get("enabled"):
        print('note: "enabled" is false in discord.json, so the mod is writing nothing yet')

    return endpoint, key


class Bridge:
    """The bridge folder, and where this relay has got to in it.

    The mod only ever appends to its out file and starts a new one each boot, because
    Lua can neither delete nor rename. So the relay reads from a byte offset, and
    removes a file once a newer one exists and the old one has nothing left in it.
    """

    def __init__(self, zbay_dir):
        self.dir = Path(zbay_dir) / "bridge"
        self.state_path = self.dir / "relay_state.json"
        self.inbox = self.dir / "in.log"
        self.ack = self.dir / "in_ack.json"
        self.snapshot = self.dir / "snapshot.json"
        self.state = self._load()

    def _load(self):
        """The relay's own state, or a fresh one.

        A relay that has lost its state counts from one again, and says so with a new
        epoch: that is what tells the mod to listen from the top instead of ignoring low
        numbers.
        """
        state = {}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        state.setdefault("epoch", secrets.token_hex(6))
        state.setdefault("in_seq", 0)
        state.setdefault("boot", 0)
        state.setdefault("offset", 0)
        state.setdefault("snapshot_stamp", None)
        state.setdefault("command_after", 0)
        return state

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def out_files(self):
        files = []
        for path in self.dir.glob("out_*.log"):
            stem = path.stem[4:]
            if stem.isdigit():
                files.append((int(stem), path))
        return sorted(files)

    def read_new(self, limit=BATCH):
        """Up to `limit` records not yet sent, as (boot, records, offset after them).

        A line with no newline at the end is the mod mid-write and is left for next time.
        A whole line that isn't a record is skipped, not waited on for ever. A file with
        nothing left in it is removed once the server has booted again since. Returns
        (None, [], None) when there is nothing.
        """
        files = [f for f in self.out_files() if f[0] >= self.state["boot"]]

        while files:
            boot, path = files[0]
            offset = self.state["offset"] if boot == self.state["boot"] else 0

            try:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except FileNotFoundError:
                data = b""

            records = []
            consumed = 0

            while len(records) < limit:
                end = data.find(b"\n", consumed)
                if end == -1:
                    break
                line = data[consumed:end].strip()
                consumed = end + 1
                if not line:
                    continue
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    records.append(record)

            if records or consumed:
                return boot, records, offset + consumed

            if len(files) > 1:
                try:
                    path.unlink()
                except OSError:
                    pass
                self.state["boot"], self.state["offset"] = files[1][0], 0
                files.pop(0)
                continue

            return None, [], None

        return None, [], None

    def commit(self, boot, offset):
        self.state["boot"], self.state["offset"] = boot, offset
        self.save()

    def snapshot_if_changed(self):
        """The snapshot's text when it has changed since the last one sent, else None.

        One caught mid-write doesn't parse. It will be whole on the next look.
        """
        try:
            stat = self.snapshot.stat()
        except FileNotFoundError:
            return None

        stamp = [stat.st_mtime_ns, stat.st_size]
        if stamp == self.state["snapshot_stamp"]:
            return None

        try:
            text = self.snapshot.read_text(encoding="utf-8")
            json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

        self.pending_stamp = stamp
        return text

    def snapshot_sent(self):
        self.state["snapshot_stamp"] = self.pending_stamp
        self.save()

    def write_commands(self, commands):
        """Append the bot's commands to in.log, numbered the way the mod expects."""
        if not commands:
            return

        lines = []
        for command in commands:
            self.state["in_seq"] += 1
            body = {k: v for k, v in command.items() if k != "id"}
            body["seq"] = self.state["in_seq"]
            body["epoch"] = self.state["epoch"]
            lines.append(json.dumps(body, separators=(",", ":")))
            self.state["command_after"] = max(self.state["command_after"], int(command.get("id", 0)))

        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.inbox, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

        self.save()

    def compact_inbox(self):
        """Empty in.log once the mod says it has acted on everything in it.

        False when the game has the file open: it will still be there to clear next time.
        """
        try:
            if self.inbox.stat().st_size == 0:
                return False
            ack = json.loads(self.ack.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False

        if ack.get("epoch") != self.state["epoch"] or ack.get("seq") != self.state["in_seq"]:
            return False

        try:
            open(self.inbox, "w").close()
        except OSError:
            return False
        return True


class Client:
    """The bot's side of the wire."""

    def __init__(self, endpoint, key, timeout=20):
        self.endpoint, self.key, self.timeout = endpoint, key, timeout

    def _call(self, method, path, body=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        request = urllib.request.Request(self.endpoint + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "User-Agent": "zbay-relay/" + str(VERSION),
        })
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def post_events(self, boot, records):
        return self._call("POST", "/v1/events", {"boot": boot, "records": records})

    def put_snapshot(self, text):
        return self._call("PUT", "/v1/snapshot", text.encode("utf-8"))

    def get_commands(self, after):
        return self._call("GET", "/v1/commands?after=" + str(int(after))).get("commands", [])


class Rcon:
    """Just enough of Source RCON to run one command."""

    AUTH, EXEC = 3, 2

    def __init__(self, host, port, password, timeout=5):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout

    @staticmethod
    def packet(ident, kind, body):
        payload = struct.pack("<ii", ident, kind) + body.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    @staticmethod
    def _read(sock):
        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                raise ConnectionError("rcon closed the connection")
            header += chunk
        size = struct.unpack("<i", header)[0]
        body = b""
        while len(body) < size:
            chunk = sock.recv(size - len(body))
            if not chunk:
                raise ConnectionError("rcon closed the connection")
            body += chunk
        ident, kind = struct.unpack("<ii", body[:8])
        return ident, kind

    def run(self, command):
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(self.packet(1, self.AUTH, self.password))
            ident, _ = self._read(sock)
            if ident == -1:
                raise PermissionError("rcon refused the password")
            sock.sendall(self.packet(2, self.EXEC, command))


def step(bridge, client, poke=None):
    """One pass: send what the mod wrote, fetch what the bot wants. Returns what it did.

    The place in the out file moves only after the bot has the records, so a failed send
    leaves it where it was. A failed poke is a delay and not a loss: the mod polls anyway.
    """
    done = {"events": 0, "snapshot": False, "commands": 0}

    boot, records, offset = bridge.read_new()
    if boot is not None:
        if records:
            client.post_events(boot, records)
        bridge.commit(boot, offset)
        done["events"] = len(records)

    text = bridge.snapshot_if_changed()
    if text is not None:
        client.put_snapshot(text)
        bridge.snapshot_sent()
        done["snapshot"] = True

    commands = client.get_commands(bridge.state["command_after"])
    if commands:
        bridge.write_commands(commands)
        done["commands"] = len(commands)
        if poke:
            try:
                poke()
            except Exception as err:
                print("rcon poke failed: " + str(err))

    bridge.compact_inbox()
    return done


def main(argv=None):
    """Run until interrupted.

    An outage is said once, not on every pass. Nothing is lost in one: the place in the
    out file only moves after a send lands.
    """
    parser = argparse.ArgumentParser(description="zBay relay: carries the Discord bridge for one game server.")
    parser.add_argument("--zbay-dir", required=True, help="the server's Zomboid/Lua/zBay folder")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between passes (default 2)")
    parser.add_argument("--rcon-host", default="127.0.0.1")
    parser.add_argument("--rcon-port", type=int, default=0, help="the server's RCONPort. leave out to not poke")
    args = parser.parse_args(argv)

    endpoint, key = read_settings(args.zbay_dir)
    bridge = Bridge(args.zbay_dir)
    client = Client(endpoint, key)

    poke = None
    if args.rcon_port:
        password = os.environ.get("ZBAY_RCON_PASSWORD")
        if not password:
            raise SystemExit("--rcon-port needs ZBAY_RCON_PASSWORD set to the server's RCONPassword")
        rcon = Rcon(args.rcon_host, args.rcon_port, password)
        poke = lambda: rcon.run("reloadlua zBay_BridgePoke.lua")

    print("zbay relay " + str(VERSION) + ": " + str(bridge.dir) + " <-> " + endpoint)

    failing = False
    while True:
        try:
            done = step(bridge, client, poke)
            if failing:
                print("connected again")
                failing = False
            if done["events"] or done["commands"] or done["snapshot"]:
                print(time.strftime("%H:%M:%S") + "  sent " + str(done["events"]) + " event(s)"
                      + (", snapshot" if done["snapshot"] else "")
                      + ", received " + str(done["commands"]) + " command(s)")
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise SystemExit("the bot refused the serverKey. run /zbay setup again and paste the new one")
            if not failing:
                print("the bot answered " + str(err.code) + ". will keep trying")
                failing = True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            if not failing:
                print("can't reach the bot (" + str(getattr(err, "reason", err)) + "). will keep trying")
                failing = True
        except KeyboardInterrupt:
            return 0

        try:
            time.sleep(args.interval if not failing else max(args.interval, 10))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
