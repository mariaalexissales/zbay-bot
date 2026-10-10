"""The slash commands, called the way Discord calls them but with nobody on the other end.

This does not connect. It builds the bot, takes each command's own function off the
command tree and hands it a stand-in for the interaction, so what is tested is what a
command says and does, not whether Discord delivers it.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from zbay_service.bot import NO_SNAPSHOT, NOT_SET_UP, ZBayBot
from zbay_service.store import Store

GUILD = 111


class Interaction:
    def __init__(self, user=42, admin=False, guild=GUILD):
        self.guild_id = guild
        self.user = SimpleNamespace(id=user)
        self.permissions = SimpleNamespace(manage_guild=admin)
        self.said = []
        self.deferred = False

        async def send_message(text, ephemeral=False):
            self.said.append((text, ephemeral))

        async def defer(ephemeral=False):
            self.deferred = ephemeral

        async def send(text, ephemeral=False):
            self.said.append((text, ephemeral))

        self.response = SimpleNamespace(send_message=send_message, defer=defer)
        self.followup = SimpleNamespace(send=send)

    @property
    def text(self):
        return self.said[-1][0]


async def make(guild_only=None):
    bot = ZBayBot(Store(), setup_url="https://setup.example", guild_only=guild_only)
    group = bot.tree.get_command("zbay")

    async def run(name, interaction, **options):
        await group.get_command(name).callback(interaction, **options)
        return interaction

    return bot, run


@pytest.fixture
async def hosted():
    bot, run = await make()
    yield bot, run
    await bot.close()


@pytest.fixture
async def paired(hosted):
    bot, run = hosted
    await run("setup", Interaction(admin=True))
    return bot, run


async def test_setup_is_for_admins_and_shows_the_key_once(hosted):
    bot, run = hosted

    refused = await run("setup", Interaction(admin=False))
    assert "Manage Server" in refused.text and not bot.hub.is_set_up(GUILD)

    first = await run("setup", Interaction(admin=True))
    key = first.text.split("```")[1]
    assert key.startswith("zb_") and bot.hub.store.tenant_by_key(key)["guild_id"] == str(GUILD)
    assert "https://setup.example" in first.text

    again = await run("setup", Interaction(admin=True))
    assert "already set up" in again.text and "zb_" not in again.text
    assert bot.hub.store.tenant_by_key(key) is not None

    replaced = await run("setup", Interaction(admin=True), new_key=True)
    new = replaced.text.split("```")[1]
    assert new != key and bot.hub.store.tenant_by_key(key) is None


async def test_a_copy_running_beside_one_game_server_hands_out_no_key():
    bot, run = await make(guild_only=str(GUILD))
    try:
        # what local mode does as it starts: the pairing exists, with a key nothing uses.
        unused = bot.hub.setup(GUILD)

        told = await run("setup", Interaction(admin=True))
        assert "no key to set up" in told.text and "zb_" not in told.text
        assert "/zbay channel" in told.text

        told = await run("setup", Interaction(admin=True), new_key=True)
        assert "zb_" not in told.text
        assert bot.hub.store.tenant_by_key(unused) is not None, "asking must not have replaced it"
    finally:
        await bot.close()


async def test_nothing_else_works_before_setup(hosted):
    bot, run = hosted

    for name, options in (("link", {}), ("unlink", {}), ("balance", {}), ("listings", {}), ("auctions", {}),
                          ("search", {"item": "axe"})):
        assert (await run(name, Interaction(), **options)).text == NOT_SET_UP

    assert (await run("channel", Interaction(admin=True))).text == NOT_SET_UP


async def test_everything_is_said_privately(paired):
    bot, run = paired
    bot.hub.set_snapshot(GUILD, json.dumps({"listings": []}))

    spoken = [await run("setup", Interaction(admin=True)), await run("link", Interaction()),
              await run("unlink", Interaction()), await run("listings", Interaction()),
              await run("search", Interaction(), item="x"), await run("channel", Interaction(admin=True))]

    assert all(ephemeral for interaction in spoken for _, ephemeral in interaction.said)


async def test_channel(paired):
    bot, run = paired
    channel = SimpleNamespace(id=900, mention="<#900>")

    assert "Manage Server" in (await run("channel", Interaction(admin=False), channel=channel)).text
    assert bot.hub.store.tenant(GUILD)["feed_channel"] is None

    assert "<#900>" in (await run("channel", Interaction(admin=True), channel=channel)).text
    assert bot.hub.store.tenant(GUILD)["feed_channel"] == "900"

    assert "off" in (await run("channel", Interaction(admin=True))).text
    assert bot.hub.store.tenant(GUILD)["feed_channel"] is None


async def test_link_and_unlink(paired):
    bot, run = paired

    offered = await run("link", Interaction(user=42))
    command = (await bot.hub.commands(GUILD, 0))[0]
    assert command["type"] == "link_offer" and command["discordId"] == "42"
    assert command["code"] in offered.text and "Bank" in offered.text

    assert "weren't linked" in (await run("unlink", Interaction(user=42))).text

    await bot.hub.ingest(GUILD, 100, [{"seq": 1, "type": "linked", "user": "al_*the*_seller", "discordId": "42"}])
    gone = await run("unlink", Interaction(user=42))
    # a name out of the game is shown with its markdown taken out.
    assert gone.text == "Unlinked from **altheseller**."


async def test_balance_is_asked_of_the_game(paired, monkeypatch):
    bot, run = paired
    await bot.hub.ingest(GUILD, 100, [{"seq": 1, "type": "hello", "currency": {"name": "Caps", "symbol": ""}}])

    asking = asyncio.create_task(run("balance", Interaction(user=42)))
    await asyncio.sleep(0.01)
    query = (await bot.hub.commands(GUILD, 0))[0]
    await bot.hub.ingest(GUILD, 100, [{"seq": 2, "type": "reply", "replyTo": query["replyTo"], "ok": True,
                                       "user": "al", "balance": 1250}])
    answered = await asking
    assert answered.deferred is True
    assert answered.text == "**al** has 1,250 Caps in the bank."

    asking = asyncio.create_task(run("balance", Interaction(user=7)))
    await asyncio.sleep(0.01)
    query = (await bot.hub.commands(GUILD, query["id"]))[0]
    await bot.hub.ingest(GUILD, 100, [{"seq": 3, "type": "reply", "replyTo": query["replyTo"], "ok": False}])
    assert "/zbay link" in (await asking).text

    async def nobody_home(*args, **kwargs):
        return None

    monkeypatch.setattr(bot.hub, "ask_balance", nobody_home)
    assert "didn't answer" in (await run("balance", Interaction())).text


async def test_search_and_the_two_lists(paired):
    bot, run = paired

    assert (await run("listings", Interaction())).text == NO_SNAPSHOT

    bot.hub.set_snapshot(GUILD, json.dumps({"currency": {"symbol": "$"}, "listings": [
        {"id": "L3", "kind": "auction", "item": "Axe", "qty": 1, "price": 40, "seller": "al", "ends": 0},
        {"id": "L2", "kind": "fixed", "item": "Nails", "qty": 10, "price": 2, "seller": "bo"},
    ] + [{"id": f"F{n}", "kind": "fixed", "item": "Plank", "qty": 1, "price": n, "seller": "cy"} for n in range(15)]}))

    listings = (await run("listings", Interaction())).text
    assert "Newest listings" in listings and "showing 10 of 16" in listings and "Axe" not in listings

    auctions = (await run("auctions", Interaction())).text
    assert "**Axe**" in auctions and "Nails" not in auctions

    found = (await run("search", Interaction(), item="nail")).text
    assert "**Nails** x10, $2 each" in found

    assert (await run("search", Interaction(), item="zzz")).text == "Nothing matches."

    # what somebody types into the search box is shown back to them, and cannot ping anyone.
    echoed = (await run("search", Interaction(), item="@everyone plank")).text
    assert "@" not in echoed.splitlines()[0]
