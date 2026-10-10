"""The bot against the mod itself.

Everything else here tests one side against a made-up other side. This runs the mod's real
Lua, behind the stub of the game its own tests use, and lets the relay carry what it writes
to the hub and back. It is the one place that fails if the two repos stop agreeing on a
file name, a field, or who numbers what.

Skipped when the mod, its harness or lupa isn't on this machine. ZBAY_MOD_DIR says where
the mod is when it isn't beside the other mods.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

lua51 = pytest.importorskip("lupa.lua51")

import zbay_relay
from zbay_service.hub import Hub
from zbay_service.local import Direct
from zbay_service.store import Store

from test_service import FakeSink

MOD = Path(os.environ.get("ZBAY_MOD_DIR") or Path.home() / "Zomboid/Workshop/zBay")
LUA = MOD / "Contents/mods/zBay/42/media/lua"
HARNESS = MOD.parent / "estral-tools/zbay/harness"

pytestmark = pytest.mark.skipif(not (LUA.is_dir() and HARNESS.is_dir()), reason="the zBay mod and its harness aren't here")

GUILD = "111"
PREFIX = "zBay/"


class Game:
    """The mod, running as a dedicated server with the bridge switched on."""

    def __init__(self, folder):
        self.folder = folder
        self.written = {}
        self.rt = lua51.LuaRuntime(unpack_returned_tuples=True)

        paths = [HARNESS, LUA / "shared", LUA / "server", LUA / "client"]
        self.rt.globals().package.path = ";".join(p.as_posix() + "/?.lua" for p in paths)

        # the stub's wall clock starts on a fixed day. the bot reads the real one, so the two
        # are set to agree: "ends in an hour" has to mean the same thing on both sides.
        self.lua(f"""
            M = require "mock"
            M.clock = {int(time.time() * 1000)}
            B = require "boot"
            B.server(nil, B.DISCORD_ON)
            seller = B.player("seller")
            buyer = B.player("buyer", 1000)
        """)

    def lua(self, code):
        return self.rt.execute(code)

    def write_out(self):
        """What the mod wrote, put on disk where the relay looks.

        Only what has changed since last time. The mod appends and never deletes, so a
        file the relay has removed is still in the stub's memory, and copying it out
        again would bring it back.
        """
        for path, text in self.rt.globals().M.files.items():
            if not path.startswith(PREFIX + "bridge/") or path.endswith("in.log"):
                continue
            if self.written.get(path) == text:
                continue

            self.written[path] = text
            target = self.folder / path[len(PREFIX):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))

    def read_in(self):
        """What the relay wrote, handed to the mod."""
        inbox = self.folder / "bridge" / "in.log"
        self.rt.globals().M.files[PREFIX + "bridge/in.log"] = inbox.read_text(encoding="utf-8") if inbox.exists() else None

    def tick(self, ms=3000):
        self.lua(f"M.tick({ms})")


@pytest.fixture
async def world(tmp_path):
    hub = Hub(Store(), FakeSink())
    hub.setup(GUILD)
    hub.set_feed(GUILD, "900")

    game = Game(tmp_path)
    bridge = zbay_relay.Bridge(tmp_path)
    direct = Direct(hub, GUILD)

    async def carry():
        """One round trip: game -> disk -> relay -> hub -> relay -> disk -> game."""
        game.tick()
        game.write_out()
        done = await asyncio.to_thread(zbay_relay.step, bridge, direct)
        game.read_in()
        game.tick()
        game.write_out()
        return done

    return hub, game, bridge, carry


async def test_a_sale_in_game_is_announced_in_discord(world):
    hub, game, bridge, carry = world

    game.lua("""
        lot = B.list(seller, "Base.Axe", { price = 40 })
        M.command(buyer, "buy", { id = lot.id, qty = 1 })
    """)
    await carry()

    texts = [text for _, _, text in hub.sink.posts]
    assert any("**Axe** listed by seller for $40 each" in text for text in texts), texts
    assert any("**Axe** sold to buyer by seller for $40" in text for text in texts), texts

    # the mod's first record of a boot is a hello, and it is not news.
    assert not any("hello" in text for text in texts)


async def test_search_answers_from_the_mods_snapshot(world):
    hub, game, bridge, carry = world

    game.lua("""
        B.list(seller, "Base.Axe", { price = 40 })
        B.list(seller, "Base.Nails", { kind = "a", price = 10, hours = 1 })
    """)
    game.tick(20000)
    await carry()

    lines, total, snapshot = hub.search(GUILD, "axe")
    assert total == 1 and "**Axe**, $40 each · seller" in lines[0]

    lines, total, _ = hub.search(GUILD, None, "auction")
    assert total == 1 and "**Nails**, bid $10" in lines[0] and "ends in" in lines[0]


async def test_linking_and_what_follows(world):
    hub, game, bridge, carry = world

    code = hub.offer_link(GUILD, 42)
    await carry()

    # the player types the code the bot gave them into the bank tab.
    game.lua(f'M.command(buyer, "link", {{ code = "{code.lower()}" }})')
    assert game.lua('return M.result(buyer).code') == "Linked"
    await carry()

    assert hub.linked_name(GUILD, 42) == "buyer"
    assert "Linked to **buyer**" in hub.sink.dms[0][1]

    # now something happens to them, and it arrives as a direct message.
    game.lua("""
        mine = B.list(buyer, "Base.Nails", { price = 7 })
        zBay_Bank.credit("seller", 100, "Credit")
        M.command(seller, "buy", { id = mine.id, qty = 1 })
    """)
    await carry()

    assert ("42", "Sold: **Nails**, for $7.") in hub.sink.dms

    # and a question from discord is answered by the game.
    asking = asyncio.create_task(hub.ask_balance(GUILD, 42, timeout=5))
    await asyncio.sleep(0.01)
    await carry()
    await carry()

    reply = await asking
    assert reply["ok"] is True and reply["user"] == "buyer"
    assert reply["balance"] == game.lua('return zBay_Bank.balance("buyer")')


async def test_the_inbox_is_cleared_once_the_mod_has_acknowledged_it(world):
    hub, game, bridge, carry = world
    inbox = bridge.inbox

    hub.offer_link(GUILD, 42)
    await carry()
    assert inbox.stat().st_size > 0

    ack = json.loads((bridge.dir / "in_ack.json").read_text())
    assert ack == {"seq": 1, "epoch": bridge.state["epoch"], "t": ack["t"]}

    await carry()
    assert inbox.stat().st_size == 0

    # an emptied inbox is not a new command to the mod.
    assert game.lua('return zBay_State.ensure().bridge.inSeq') == 1


async def test_a_restart_of_the_game_server_carries_on(world):
    hub, game, bridge, carry = world

    game.lua('B.list(seller, "Base.Axe", { price = 40 })')
    await carry()
    posted = len(hub.sink.posts)
    first = bridge.out_files()[0][1]

    # the server boots again a while later: a new out file, numbered on from the last.
    game.lua("""
        M.advance(3600000)
        M.fire("OnInitGlobalModData")
        B.list(seller, "Base.Nails", { price = 3 })
    """)
    await carry()
    await carry()

    assert len(hub.sink.posts) == posted + 1
    assert "**Nails** listed" in hub.sink.posts[-1][2]
    assert not first.exists(), "the drained file from the boot before should have been removed"
