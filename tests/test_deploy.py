"""What the bot says about what it is connected to, and the files that put it on a host.

A bot started from .env.example with only the token filled in ran for an evening in hosted
mode, connected to Discord and to nothing else, answering every command as if a relay were
missing. These are the things that would have said so.
"""

import asyncio
import re
import tomllib
from pathlib import Path

import pytest

from zbay_relay import Bridge, describe
from zbay_service.__main__ import found, read_settings
from zbay_service.bot import ZBayBot
from zbay_service.hub import Hub
from zbay_service.store import Store

from folders import Disk, out
from test_bot import GUILD, Interaction
from test_service import FakeSink

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / ".env.example"


def test_the_container_is_local_mode_whatever_the_settings_say():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    command = re.search(r"^CMD \[(.+)\]$", dockerfile, re.M).group(1)
    assert command.split(", ")[-1] == '"--local"'

    # the flag wins over a settings file that says otherwise, which the example does.
    args, _ = read_settings(["--env-file", str(EXAMPLE), "--local", "--check"], {})
    assert args.local is True

    # and a container handed the example with only a token stops and says why. it does not
    # start hosted and sit there.
    with pytest.raises(SystemExit, match="local mode needs ZBAY_DIR and ZBAY_GUILD"):
        read_settings(["--env-file", str(EXAMPLE), "--local"], {"ZBAY_DISCORD_TOKEN": "tok"})


def test_fly_config_has_a_volume_and_nothing_a_secret_would_override():
    fly = tomllib.loads((REPO / "fly.toml").read_text(encoding="utf-8"))

    assert "ZBAY_LOCAL" not in fly.get("env", {}), "a secret of the same name wins over [env]"
    assert "http_service" not in fly and "services" not in fly, "the bot listens on nothing"
    assert fly["mounts"][0]["destination"] == "/data"
    assert fly["restart"][0]["policy"] == "always"


def test_a_pass_that_carried_something_is_a_line_in_the_log():
    assert describe({"events": 0, "snapshot": False, "commands": 0}) is None

    line = describe({"events": 2, "snapshot": True, "commands": 1})
    assert re.fullmatch(r"\d\d:\d\d:\d\d  2 event\(s\) from the game, snapshot, 1 command\(s\) to the game", line)

    assert "snapshot" not in describe({"events": 0, "snapshot": False, "commands": 3})


def test_the_first_look_at_the_games_folder_says_what_is_there(tmp_path):
    world = Disk(tmp_path)
    bridge = world.bridge()

    assert "nothing from the game in " + str(tmp_path) in found(bridge)
    assert "enabled" in found(bridge)

    out(world, 100, {"seq": 1, "type": "hello"})
    out(world, 250, {"seq": 2, "type": "hello"})
    assert found(bridge) == "connected to the game's folder: 2 file(s) from the game, newest from boot 250"


async def test_the_games_hello_is_in_the_log(capsys):
    hub = Hub(Store(), FakeSink())
    hub.setup(GUILD)

    await hub.ingest(GUILD, 100, [{"seq": 1, "type": "hello", "world": "servertest", "currency": {"symbol": "$"}},
                                  {"seq": 2, "type": "listing", "item": "Axe", "price": 4, "seller": "al"}])

    printed = capsys.readouterr().out
    assert f"[{GUILD}] the game server says hello: world 'servertest', boot 100" in printed
    assert printed.count("hello") == 1, "a listing is not a hello"


async def test_a_copy_with_no_relay_does_not_send_anybody_looking_for_one():
    bot = ZBayBot(Store(), guild_only=str(GUILD))
    group = bot.tree.get_command("zbay")
    bot.hub.setup(GUILD)

    async def nobody_home(*args, **kwargs):
        return None

    bot.hub.ask_balance = bot.hub.ask_mailbox = nobody_home

    try:
        said = []
        for name in ("listings", "auctions", "balance", "mailbox"):
            interaction = Interaction()
            await group.get_command(name).callback(interaction)
            said.append(interaction.text)

        assert all("relay" not in text for text in said), said
        assert "discord.json" in said[0] and "can't read its folder" in said[2]
    finally:
        await bot.close()
