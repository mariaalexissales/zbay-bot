"""The hub and the relay api, with Discord played by a list."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from zbay_service import render
from zbay_service.api import make_app
from zbay_service.hub import Hub, Sink
from zbay_service.store import Store, hash_key

GUILD = "111"
DOLLARS = {"name": "Dollars", "symbol": "$"}


class FakeSink(Sink):
    def __init__(self):
        self.posts, self.dms = [], []

    async def post(self, guild_id, channel_id, text):
        self.posts.append((guild_id, channel_id, text))

    async def dm(self, discord_id, text):
        self.dms.append((discord_id, text))


@pytest.fixture
def hub():
    hub = Hub(Store(), FakeSink())
    hub.key = hub.setup(GUILD)
    hub.set_feed(GUILD, "900")
    hub.set_snapshot(GUILD, json.dumps({"currency": DOLLARS, "listings": [
        {"id": "L3", "kind": "auction", "item": "Axe", "qty": 1, "price": 40, "buyout": 200, "bids": 2,
         "seller": "al", "ends": 10_000_000},
        {"id": "L2", "kind": "fixed", "item": "Box of Nails", "qty": 10, "price": 2, "seller": "bo"},
        {"id": "L1", "kind": "fixed", "item": "Fire Axe", "qty": 1, "price": 75, "seller": "al"},
    ]}))
    return hub


# ---- the hub ----

async def test_market_events_go_to_the_feed_channel(hub):
    await hub.ingest(GUILD, 100, [
        {"seq": 1, "type": "hello", "boot": 100},
        {"seq": 2, "type": "listing", "item": "Axe", "qty": 2, "price": 40, "seller": "al"},
        {"seq": 3, "type": "sale", "item": "Axe", "qty": 1, "price": 40, "seller": "al", "buyer": "bo"},
        {"seq": 4, "type": "auctionEnd", "item": "Anvil", "qty": 1, "seller": "al", "bids": 0},
    ])

    texts = [text for _, _, text in hub.sink.posts]
    assert len(texts) == 3
    assert all(channel == "900" for _, channel, _ in hub.sink.posts)
    assert "**Axe** x2 listed by al for $40 each" in texts[0]
    assert "sold to bo by al for $40" in texts[1]
    assert "ended with no bids" in texts[2]


async def test_the_game_says_what_its_money_is_called(hub):
    hub.set_snapshot(GUILD, json.dumps({"listings": []}))

    await hub.ingest(GUILD, 100, [
        {"seq": 1, "type": "hello", "boot": 100, "currency": {"name": "Caps", "symbol": ""}},
        {"seq": 2, "type": "listing", "item": "Axe", "price": 40, "seller": "al"},
        {"seq": 3, "type": "config", "currency": {"name": "Dollars", "symbol": "$"}},
        {"seq": 4, "type": "listing", "item": "Saw", "price": 9, "seller": "al"},
    ])

    texts = [text for _, _, text in hub.sink.posts]
    assert len(texts) == 2
    assert "for 40 Caps each" in texts[0]
    assert "for $9 each" in texts[1]
    assert hub.currency(GUILD) == {"name": "Dollars", "symbol": "$"}


async def test_no_channel_no_posts(hub):
    hub.set_feed(GUILD, None)
    await hub.ingest(GUILD, 100, [{"seq": 1, "type": "listing", "item": "Axe", "price": 1, "seller": "al"}])
    assert hub.sink.posts == []


async def test_a_batch_sent_twice_is_handled_once(hub):
    batch = [{"seq": 1, "type": "listing", "item": "Axe", "price": 1, "seller": "al"},
             {"seq": 2, "type": "listing", "item": "Saw", "price": 1, "seller": "al"}]

    assert await hub.ingest(GUILD, 100, batch) == 2
    assert await hub.ingest(GUILD, 100, batch) == 2
    assert await hub.ingest(GUILD, 100, batch + [{"seq": 3, "type": "listing", "item": "Nail", "price": 1, "seller": "al"}]) == 3
    assert len(hub.sink.posts) == 3


async def test_a_newer_boot_is_heard_from_its_first_record(hub):
    await hub.ingest(GUILD, 100, [{"seq": 500, "type": "listing", "item": "Axe", "price": 1, "seller": "al"}])

    # the world was rolled back: the game counts from where its save says.
    await hub.ingest(GUILD, 200, [{"seq": 12, "type": "listing", "item": "Saw", "price": 1, "seller": "al"}])
    assert len(hub.sink.posts) == 2

    # and a straggler from the boot before is not replayed.
    await hub.ingest(GUILD, 100, [{"seq": 501, "type": "listing", "item": "Old", "price": 1, "seller": "al"}])
    assert len(hub.sink.posts) == 2


async def test_one_bad_record_does_not_cost_the_batch(hub):
    assert await hub.ingest(GUILD, 100, [
        "nonsense", {"type": "listing"}, {"seq": "x"},
        {"seq": 5, "type": "listing", "item": "Axe", "price": 1, "seller": "al"},
    ]) == 5
    assert len(hub.sink.posts) == 1


async def test_an_unknown_server_is_ignored(hub):
    assert await hub.ingest("999", 100, [{"seq": 1, "type": "listing", "item": "Axe"}]) == 0
    assert hub.sink.posts == []


async def test_notices_are_direct_messages(hub):
    await hub.ingest(GUILD, 100, [
        {"seq": 1, "type": "notice", "user": "al", "discordId": "42", "code": "Outbid", "item": "Axe", "amount": 150},
        {"seq": 2, "type": "notice", "user": "al", "discordId": "42", "code": "NotAThing", "item": "Axe"},
    ])
    assert hub.sink.dms == [("42", "You were outbid on **Axe**. Your $150 is back in the bank.")]
    assert hub.sink.posts == []


async def test_linking_runs_through_the_game(hub):
    code = hub.offer_link(GUILD, 42)
    assert len(code) == 9 and code[4] == "-"

    commands = await hub.commands(GUILD, 0)
    assert commands[0]["type"] == "link_offer" and commands[0]["code"] == code and commands[0]["discordId"] == "42"
    assert commands[0]["expires"] > 1_700_000_000_000
    assert hub.linked_name(GUILD, 42) is None

    # the player typed it in, and the game says so.
    await hub.ingest(GUILD, 100, [{"seq": 1, "type": "linked", "user": "al", "discordId": "42"}])
    assert hub.linked_name(GUILD, 42) == "al"
    assert "Linked to **al**" in hub.sink.dms[0][1]

    assert hub.unlink(GUILD, 42) == "al"
    assert hub.linked_name(GUILD, 42) is None
    assert (await hub.commands(GUILD, commands[0]["id"]))[0]["type"] == "unlink"


async def test_asking_for_commands_is_the_receipt_for_older_ones(hub):
    hub.offer_link(GUILD, 1)
    hub.offer_link(GUILD, 2)

    first = await hub.commands(GUILD, 0)
    assert len(first) == 2
    # a relay that crashed before writing them asks again and gets them again.
    assert len(await hub.commands(GUILD, 0)) == 2
    assert await hub.commands(GUILD, first[-1]["id"]) == []


async def test_a_held_request_wakes_when_a_command_arrives(hub):
    waiting = asyncio.create_task(hub.commands(GUILD, 0, wait=5))
    await asyncio.sleep(0.01)
    assert not waiting.done()

    hub.offer_link(GUILD, 7)
    commands = await asyncio.wait_for(waiting, timeout=1)
    assert commands[0]["discordId"] == "7"

    assert await hub.commands(GUILD, commands[0]["id"], wait=0.01) == []


async def test_a_balance_question_is_answered_by_the_game(hub):
    asking = asyncio.create_task(hub.ask_balance(GUILD, 42, timeout=2))
    await asyncio.sleep(0.01)

    query = (await hub.commands(GUILD, 0))[0]
    assert query["type"] == "balance_query" and query["discordId"] == "42"

    await hub.ingest(GUILD, 100, [{"seq": 1, "type": "reply", "replyTo": query["replyTo"], "ok": True,
                                   "user": "al", "balance": 1250}])
    reply = await asking
    assert reply["balance"] == 1250 and reply["user"] == "al"


async def test_a_balance_question_nobody_answers_gives_up(hub):
    assert await hub.ask_balance(GUILD, 42, timeout=0.01) is None
    assert hub.questions == {}


async def test_a_mailbox_question_is_answered_by_the_game(hub):
    asking = asyncio.create_task(hub.ask_mailbox(GUILD, 42, timeout=2))
    await asyncio.sleep(0.01)

    query = (await hub.commands(GUILD, 0))[0]
    assert query["type"] == "mailbox_query" and query["discordId"] == "42"

    await hub.ingest(GUILD, 100, [{"seq": 1, "type": "reply", "replyTo": query["replyTo"], "ok": True,
                                   "user": "al", "total": 2, "lines": 1, "items": [{"item": "Anvil", "qty": 2}]}])
    reply = await asking
    assert reply["total"] == 2 and reply["items"][0]["item"] == "Anvil"

    # a game on an older mod reads the question and says nothing.
    assert await hub.ask_mailbox(GUILD, 42, timeout=0.01) is None
    assert hub.questions == {}


async def test_search_reads_the_snapshot(hub):
    lines, total, snapshot = hub.search(GUILD, "axe")
    assert total == 2 and snapshot is not None
    assert "**Axe**" in lines[0] and "bid $40 (2)" in lines[0] and "buyout $200" in lines[0]
    assert "**Fire Axe**, $75 each · al" in lines[1]

    lines, total, _ = hub.search(GUILD, None, "fixed")
    assert total == 2 and "x10" in lines[0]

    assert hub.search(GUILD, "zzz")[1] == 0
    assert hub.search("999")[2] is None


async def test_a_new_key_retires_the_old_one(hub):
    old = hub.key
    new = hub.setup(GUILD)

    assert new != old
    assert hub.store.tenant_by_key(old) is None
    assert hub.store.tenant_by_key(new)["guild_id"] == GUILD
    # the pairing itself is kept: only the key changed.
    assert hub.store.tenant(GUILD)["feed_channel"] == "900"


def test_keys_are_stored_hashed():
    store = Store()
    key = store.create_tenant(GUILD)
    row = store.tenant(GUILD)
    assert key not in tuple(row) and row["key_hash"] == hash_key(key)


async def test_leaving_a_server_forgets_it(hub):
    await hub.ingest(GUILD, 100, [{"seq": 1, "type": "linked", "user": "al", "discordId": "42"}])
    hub.offer_link(GUILD, 1)
    hub.forget(GUILD)

    assert not hub.is_set_up(GUILD)
    assert hub.store.tenant_by_key(hub.key) is None
    assert hub.linked_name(GUILD, 42) is None and hub.search(GUILD)[2] is None


# ---- rendering ----

def test_money_matches_the_game():
    assert render.money(1250, DOLLARS) == "$1,250"
    assert render.money(1250, {"name": "Caps", "symbol": ""}) == "1,250 Caps"
    assert render.money(1250, None) == "1,250"
    assert render.money(None, DOLLARS) == "?"


def test_names_from_the_game_cannot_ping_or_format():
    text = render.event_text({"type": "listing", "item": "@everyone **free** `stuff`", "price": 1,
                              "seller": "<@123> _al_"}, DOLLARS)
    assert "@" not in text and "`" not in text and "_al_" not in text
    assert "**everyone free stuff**" in text


def test_time_left():
    assert render.time_left(2 * 86_400_000 + 4 * 3_600_000, 0) == "2d 4h"
    assert render.time_left(3 * 3_600_000 + 12 * 60_000, 0) == "3h 12m"
    assert render.time_left(45 * 60_000, 0) == "45m"
    assert render.time_left(20_000, 0) == "under a minute"
    assert render.time_left(0, 5) is None and render.time_left(None) is None


def test_every_notice_the_game_sends_has_words():
    for code in ("Sold", "Outbid", "Won", "AuctionSold", "NoBids", "Expired", "Mail", "Removed", "BidReturned"):
        assert render.notice_text({"code": code, "item": "Axe", "amount": 5}, DOLLARS)


# ---- the api ----

@pytest.fixture
async def api(hub):
    client = TestClient(TestServer(make_app(hub)))
    await client.start_server()
    client.auth = {"Authorization": "Bearer " + hub.key}
    yield client
    await client.close()


async def test_requests_need_the_key(api):
    for method, path in (("POST", "/v1/events"), ("PUT", "/v1/snapshot"), ("GET", "/v1/commands")):
        assert (await api.request(method, path, data="{}")).status == 401
        assert (await api.request(method, path, data="{}", headers={"Authorization": "Bearer nope"})).status == 401

    assert (await api.get("/healthz")).status == 200


async def test_events_over_http(api, hub):
    response = await api.post("/v1/events", headers=api.auth, json={"boot": 100, "records": [
        {"seq": 1, "type": "listing", "item": "Axe", "price": 40, "seller": "al"}]})

    assert response.status == 200 and (await response.json()) == {"ok": True, "seq": 1}
    assert len(hub.sink.posts) == 1

    assert (await api.post("/v1/events", headers=api.auth, data="not json")).status == 400
    assert (await api.post("/v1/events", headers=api.auth, json={"boot": 1})).status == 400


async def test_the_key_decides_whose_events_they_are(api, hub):
    other = hub.setup("222")
    hub.set_feed("222", "777")

    await api.post("/v1/events", headers={"Authorization": "Bearer " + other}, json={"boot": 1, "records": [
        {"seq": 1, "type": "listing", "item": "Saw", "price": 1, "seller": "cy", "guild": GUILD}]})

    assert [(guild, channel) for guild, channel, _ in hub.sink.posts] == [("222", "777")]


async def test_snapshot_over_http(api, hub):
    body = {"currency": {"name": "Caps", "symbol": ""}, "listings": [
        {"id": "L9", "kind": "fixed", "item": "Hammer", "qty": 1, "price": 3, "seller": "cy"}]}

    assert (await api.put("/v1/snapshot", headers=api.auth, json=body)).status == 200
    lines, total, _ = hub.search(GUILD, "hammer")
    assert total == 1 and "3 Caps each" in lines[0]

    assert (await api.put("/v1/snapshot", headers=api.auth, json={"nope": 1})).status == 400


async def test_commands_over_http(api, hub):
    hub.offer_link(GUILD, 42)

    response = await api.get("/v1/commands?after=0", headers=api.auth)
    commands = (await response.json())["commands"]
    assert commands[0]["type"] == "link_offer"

    response = await api.get(f"/v1/commands?after={commands[0]['id']}", headers=api.auth)
    assert (await response.json()) == {"commands": []}

    assert (await api.get("/v1/commands?after=x", headers=api.auth)).status == 400


async def test_the_relay_and_the_service_fit_together(api, hub, tmp_path):
    """The real relay against the real api: a line the game wrote comes out in Discord,
    and a command from Discord lands in the game's inbox."""
    import zbay_relay

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "out_100.log").write_text(
        '{"seq":1,"type":"hello","boot":100}\n'
        '{"seq":2,"type":"sale","item":"Axe","qty":1,"price":40,"seller":"al","buyer":"bo"}\n')
    (bridge_dir / "snapshot.json").write_text('{"currency":{"symbol":"$"},"listings":[]}')

    hub.offer_link(GUILD, 42)

    endpoint = str(api.make_url("")).rstrip("/")
    bridge, client = zbay_relay.Bridge(tmp_path), zbay_relay.Client(endpoint, hub.key)

    done = await asyncio.to_thread(zbay_relay.step, bridge, client)

    assert done == {"events": 2, "snapshot": True, "commands": 1}
    assert "sold to bo" in hub.sink.posts[0][2]
    assert hub.search(GUILD)[2]["listings"] == []

    line = json.loads((bridge_dir / "in.log").read_text())
    assert line["type"] == "link_offer" and line["discordId"] == "42" and line["seq"] == 1

    with pytest.raises(Exception):
        await asyncio.to_thread(zbay_relay.step, bridge, zbay_relay.Client(endpoint, "wrong"))
