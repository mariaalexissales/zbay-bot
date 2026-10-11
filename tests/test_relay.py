"""The relay against a bridge folder, with the bot's end played by a list.

Every test of the bridge itself runs twice: against the game's folder on disk, and against
the same files on an SFTP host. See folders.py.
"""

import json
import struct

import pytest

import zbay_relay
from zbay_relay import Bridge, Rcon, step, strip_json_comments

from folders import Disk, Sftp, out


class FakeClient:
    def __init__(self):
        self.events, self.snapshots, self.asked = [], [], []
        self.commands = []
        self.fail = False

    def post_events(self, boot, records):
        if self.fail:
            raise ConnectionError("down")
        self.events.append((boot, records))

    def put_snapshot(self, text):
        self.snapshots.append(text)

    def get_commands(self, after):
        self.asked.append(after)
        return [c for c in self.commands if c["id"] > after]


@pytest.fixture(params=[Disk, Sftp], ids=["disk", "sftp"])
def world(request, tmp_path):
    return request.param(tmp_path)


@pytest.fixture
def zbay(tmp_path):
    (tmp_path / "bridge").mkdir()
    return tmp_path


def test_sends_new_lines_once(world):
    out(world, 100, {"seq": 1, "type": "hello"}, {"seq": 2, "type": "listing"})
    bridge, client = world.bridge(), FakeClient()

    assert step(bridge, client)["events"] == 2
    assert client.events == [(100, [{"seq": 1, "type": "hello"}, {"seq": 2, "type": "listing"}])]

    assert step(bridge, client)["events"] == 0
    assert len(client.events) == 1

    out(world, 100, {"seq": 3, "type": "sale"})
    step(bridge, client)
    assert client.events[-1] == (100, [{"seq": 3, "type": "sale"}])


def test_a_line_being_written_is_left_until_it_is_whole(world):
    name = out(world, 100, {"seq": 1}, tail='{"seq":2,"ty')
    bridge, client = world.bridge(), FakeClient()

    step(bridge, client)
    assert client.events == [(100, [{"seq": 1}])]

    world.append(name, b'pe":"sale"}\n')
    step(bridge, client)
    assert client.events[-1] == (100, [{"seq": 2, "type": "sale"}])


def test_a_whole_line_that_is_not_a_record_is_skipped(world):
    name = out(world, 100, {"seq": 1})
    world.append(name, b"not json\n[1,2]\n")
    out(world, 100, {"seq": 2})
    bridge, client = world.bridge(), FakeClient()

    step(bridge, client)
    assert client.events == [(100, [{"seq": 1}, {"seq": 2}])]


def test_a_failed_send_is_sent_again(world):
    out(world, 100, {"seq": 1})
    bridge, client = world.bridge(), FakeClient()

    client.fail = True
    with pytest.raises(ConnectionError):
        step(bridge, client)
    assert bridge.state["offset"] == 0

    client.fail = False
    step(bridge, client)
    assert client.events == [(100, [{"seq": 1}])]


def test_where_it_got_to_survives_a_restart(world):
    out(world, 100, {"seq": 1})
    step(world.bridge(), FakeClient())

    out(world, 100, {"seq": 2})
    client = FakeClient()
    step(world.bridge(), client)
    assert client.events == [(100, [{"seq": 2}])]


def test_an_old_file_is_removed_once_drained_and_a_newer_one_exists(world):
    old = out(world, 100, {"seq": 1})
    bridge, client = world.bridge(), FakeClient()
    step(bridge, client)

    # still the only one: the server may yet write to it.
    step(bridge, client)
    assert world.exists(old)

    new = out(world, 200, {"seq": 2, "type": "hello"})
    step(bridge, client)

    assert not world.exists(old) and world.exists(new)
    assert client.events[-1] == (200, [{"seq": 2, "type": "hello"}])


def test_a_backlog_goes_in_pieces(world):
    out(world, 100, *[{"seq": n} for n in range(1, 451)])
    bridge, client = world.bridge(), FakeClient()

    sizes = []
    while step(bridge, client)["events"]:
        sizes.append(len(client.events[-1][1]))

    assert sizes == [200, 200, 50]
    assert [r["seq"] for _, batch in client.events for r in batch] == list(range(1, 451))


def test_a_long_file_is_read_a_piece_at_a_time(world, monkeypatch):
    # a server that has been up for weeks, met for the first time. nothing is skipped and
    # no record is cut in two where one read ends and the next begins.
    monkeypatch.setattr(zbay_relay, "READ_BYTES", 100)
    out(world, 100, *[{"seq": n, "type": "listing", "item": "Nails"} for n in range(1, 41)])
    bridge, client = world.bridge(), FakeClient()

    passes = 0
    while step(bridge, client)["events"]:
        passes += 1

    assert passes > 5
    assert [r["seq"] for _, batch in client.events for r in batch] == list(range(1, 41))


def test_snapshot_is_sent_when_it_changes(world):
    bridge, client = world.bridge(), FakeClient()

    step(bridge, client)
    assert client.snapshots == []

    world.write("bridge/snapshot.json", '{"listings":[],"rev":1}')
    step(bridge, client)
    step(bridge, client)
    assert client.snapshots == ['{"listings":[],"rev":1}']

    world.write("bridge/snapshot.json", '{"listings":[],"rev":22}')
    step(bridge, client)
    assert len(client.snapshots) == 2

    # caught mid-write: not sent, and tried again once it is whole.
    world.write("bridge/snapshot.json", '{"listings":[')
    step(bridge, client)
    assert len(client.snapshots) == 2


def test_commands_are_numbered_for_the_mod(world):
    bridge, client = world.bridge(), FakeClient()
    client.commands = [{"id": 7, "type": "link_offer", "code": "AB", "discordId": "1"},
                       {"id": 9, "type": "unlink", "discordId": "2"}]

    assert step(bridge, client)["commands"] == 2

    lines = [json.loads(line) for line in world.read("bridge/in.log").splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    assert lines[0]["type"] == "link_offer" and lines[0]["code"] == "AB"
    assert "id" not in lines[0]
    assert lines[0]["epoch"] == lines[1]["epoch"] == bridge.state["epoch"]

    # asks from where it got to, which is also the receipt for what it has.
    step(bridge, client)
    assert client.asked == [0, 9]
    assert len(world.read("bridge/in.log").splitlines()) == 2


def test_inbox_is_cleared_only_once_the_mod_has_acted_on_all_of_it(world):
    bridge, client = world.bridge(), FakeClient()
    client.commands = [{"id": 1, "type": "unlink", "discordId": "2"}, {"id": 2, "type": "unlink", "discordId": "3"}]
    step(bridge, client)

    inbox, ack = "bridge/in.log", "bridge/in_ack.json"

    world.write(ack, json.dumps({"seq": 1, "epoch": bridge.state["epoch"]}))
    step(bridge, client)
    assert world.size(inbox) > 0

    world.write(ack, json.dumps({"seq": 2, "epoch": "somebody-else"}))
    step(bridge, client)
    assert world.size(inbox) > 0

    world.write(ack, json.dumps({"seq": 2, "epoch": bridge.state["epoch"]}))
    step(bridge, client)
    assert world.size(inbox) == 0

    client.commands.append({"id": 3, "type": "unlink", "discordId": "4"})
    step(bridge, client)
    assert json.loads(world.read(inbox))["seq"] == 3


def test_a_relay_that_lost_its_state_announces_a_new_epoch(zbay):
    first = Bridge(zbay)
    first.save()
    assert Bridge(zbay).state["epoch"] == first.state["epoch"]

    (zbay / "bridge" / "relay_state.json").unlink()
    assert Bridge(zbay).state["epoch"] != first.state["epoch"]


def test_a_poke_follows_new_commands_and_its_failure_is_not_fatal(world):
    bridge, client = world.bridge(), FakeClient()
    poked = []

    step(bridge, client, poke=lambda: poked.append(1))
    assert poked == []

    client.commands = [{"id": 1, "type": "unlink", "discordId": "2"}]
    step(bridge, client, poke=lambda: poked.append(1))
    assert poked == [1]

    def broken():
        raise ConnectionRefusedError("no rcon")

    client.commands.append({"id": 2, "type": "unlink", "discordId": "3"})
    assert step(bridge, client, poke=broken)["commands"] == 1


def test_settings_are_read_from_the_file_the_mod_writes(zbay):
    (zbay / "discord.json").write_text("""
// zBay's Discord bridge.
{
  "enabled": true,
  "endpoint": "https://bot.example/",   // a slash-slash inside a string is not a comment
  "serverKey": "zb_abc",
  "events": { "bid": false, },
}
""")
    assert zbay_relay.read_settings(zbay) == ("https://bot.example", "zb_abc")


def test_settings_that_are_not_filled_in_stop_it(zbay):
    with pytest.raises(SystemExit, match="no discord.json"):
        zbay_relay.read_settings(zbay)

    (zbay / "discord.json").write_text('{ "enabled": true, "endpoint": "", "serverKey": "" }')
    with pytest.raises(SystemExit, match="endpoint"):
        zbay_relay.read_settings(zbay)

    (zbay / "discord.json").write_text('{ "enabled": true, ')
    with pytest.raises(SystemExit, match="doesn't parse"):
        zbay_relay.read_settings(zbay)


def test_comment_stripping_keeps_strings_whole():
    text = '﻿{ "a": "x // y", /* gone */ "b": [1, 2, ], "c": "trailing, comma" , }'
    assert json.loads(strip_json_comments(text)) == {"a": "x // y", "b": [1, 2], "c": "trailing, comma"}


def test_rcon_packets_are_source_rcon():
    packet = Rcon.packet(2, Rcon.EXEC, "reloadlua zBay_BridgePoke.lua")
    size, ident, kind = struct.unpack("<iii", packet[:12])

    assert size == len(packet) - 4
    assert (ident, kind) == (2, 2)
    assert packet[12:] == b"reloadlua zBay_BridgePoke.lua\x00\x00"
