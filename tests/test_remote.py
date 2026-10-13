"""The game's folder over SFTP: what is particular to it being somewhere else.

That the bridge works over one at all is test_relay.py, which runs every bridge test
against it. This is the rest: a connection that drops, a host that isn't the one it is
meant to be, and the paths a rented host shows.

Nothing here opens a connection. paramiko's client is played by folders.FakeSftp, and its
handshake by a stand-in, so what is proven is what this code does with them, not that a
real host agrees. The first real run is where that is found out, and --check is built to
say why.
"""

import base64
import hashlib

import paramiko
import pytest

from zbay_relay import step
from zbay_service import remote
from zbay_service.remote import SftpFolder, WrongHostKey, fingerprint, same_fingerprint

from folders import FINGERPRINT, PASSWORD, ROOT, Sftp, out
from test_relay import FakeClient


@pytest.fixture
def host(tmp_path):
    return Sftp(tmp_path)


def test_a_file_that_is_not_there_is_not_an_error(host):
    folder = host.folder()

    assert folder.read("bridge/nothing.log") is None
    assert folder.stamp("bridge/nothing.log") is None
    assert folder.names("no-such-folder") == []
    assert folder.listing("/nowhere") is None


def test_only_files_are_named_and_a_listing_marks_folders(host):
    host.write("discord.json", "{}")
    host.write("bridge/in.log", "")
    folder = host.folder()

    assert folder.names() == ["discord.json"]
    assert folder.names("bridge") == ["in.log"]
    assert folder.listing(ROOT) == ["bridge/", "discord.json"]


def test_reading_from_where_it_left_off(host):
    host.write("bridge/out_1.log", "0123456789")
    folder = host.folder()

    assert folder.read("bridge/out_1.log") == b"0123456789"
    assert folder.read("bridge/out_1.log", 4) == b"456789"
    assert folder.read("bridge/out_1.log", 4, 3) == b"456"
    assert folder.read("bridge/out_1.log", 10) == b""


def test_append_makes_the_bridge_folder_when_the_game_has_not_yet(host):
    host.sftp.dirs.discard(ROOT + "/bridge")
    folder = host.folder()

    folder.append("bridge/in.log", b"one\n")
    folder.append("bridge/in.log", b"two\n")

    assert host.read("bridge/in.log") == "one\ntwo\n"


def test_the_path_is_taken_the_way_it_was_typed(host):
    # with a slash in front it is from the top of the host's disk.
    for typed in ("/srv/Zomboid/Lua/zBay", "/srv/Zomboid/Lua/zBay/", "\\srv\\Zomboid\\Lua\\zBay"):
        assert host.folder(typed).root == "/srv/Zomboid/Lua/zBay"

    # without one it is from where sftp starts, which is how a file manager shows it.
    for typed in ("Zomboid/Lua/zBay", "Zomboid/Lua/zBay/", " Zomboid\\Lua\\zBay "):
        assert host.folder(typed).root == "Zomboid/Lua/zBay"
    assert host.folder("").root == "."

    assert str(host.folder()) == "sftp://player.1a2b@games.example:2022" + ROOT
    assert str(host.folder("Zomboid/Lua/zBay")) == "sftp://player.1a2b@games.example:2022/~/Zomboid/Lua/zBay"
    assert PASSWORD not in str(host.folder())


def test_a_path_copied_off_the_file_manager_reaches_the_same_folder(host):
    host.write("discord.json", "{}")
    out(host, 100, {"seq": 1, "type": "hello"})

    typed = host.folder("Zomboid/Lua/zBay")

    assert typed.names() == ["discord.json"]
    assert typed.names("bridge") == ["out_100.log"]
    assert typed.read("discord.json") == b"{}"

    typed.append("bridge/in.log", b"one\n")
    assert host.read("bridge/in.log") == "one\n"


def test_a_dropped_connection_is_said_and_the_next_call_connects_again(host):
    host.write("discord.json", "{}")
    folder = host.folder()

    assert folder.names() == ["discord.json"] and host.opened == 1

    host.sftp.down = True
    with pytest.raises(ConnectionError, match="games.example"):
        folder.names()

    host.sftp.down = False
    assert folder.names() == ["discord.json"]
    assert host.opened == 2, "the dead session was dropped and a new one opened"


def test_an_outage_is_never_taken_for_a_file_having_gone(host):
    # two boots' files. a drained one is removed when a newer exists, and "can't read it"
    # must not look like "nothing left in it".
    old = out(host, 100, {"seq": 1})
    new = out(host, 200, {"seq": 2})
    bridge, client = host.bridge(), FakeClient()

    host.sftp.down = True
    with pytest.raises(ConnectionError):
        step(bridge, client)

    assert host.exists(old) and host.exists(new)
    assert bridge.state["boot"] == 0 and bridge.state["offset"] == 0
    assert client.events == []

    host.sftp.down = False
    while step(bridge, client)["events"]:
        pass
    assert [batch for _, batch in client.events] == [[{"seq": 1}], [{"seq": 2}]]


def test_a_refusal_from_the_host_is_not_a_dropped_connection(host):
    folder = host.folder()
    folder.names()

    def refuse(path, mode="r"):
        raise PermissionError(13, "Permission denied")

    host.sftp.open = refuse
    with pytest.raises(PermissionError):
        folder.truncate("bridge/in.log")
    assert host.opened == 1, "the session is still good"


def test_fingerprints():
    key = type("Key", (), {"asbytes": lambda self: b"a host key"})()
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(b"a host key").digest()).decode().rstrip("=")

    assert fingerprint(key) == expected

    assert same_fingerprint(expected, expected)
    assert same_fingerprint(expected, expected[7:]), "copied without the SHA256: in front"
    assert same_fingerprint(expected, " " + expected + "=\n"), "copied with padding and a newline"
    assert not same_fingerprint(expected, expected[:-1] + "x")
    assert not same_fingerprint("", ""), "nothing pinned matches nothing"
    assert not same_fingerprint(expected, None)


class Handshaken:
    """A transport that has got as far as showing its key."""

    def __init__(self, key):
        self.key, self.signed_in, self.closed = key, [], False

    def get_remote_server_key(self):
        return type("Key", (), {"asbytes": lambda _: self.key})()

    def auth_password(self, user, password):
        self.signed_in.append((user, password))

    def set_keepalive(self, seconds):
        pass

    def close(self):
        self.closed = True


def test_the_password_is_not_sent_to_a_host_showing_the_wrong_key(monkeypatch):
    transport = Handshaken(b"somebody else's key")
    monkeypatch.setattr(remote, "_handshake", lambda host, port, timeout: transport)

    pinned = fingerprint(Handshaken(b"the real key").get_remote_server_key())

    with pytest.raises(WrongHostKey) as refused:
        remote.connect("games.example", 2022, "player", PASSWORD, pinned)

    assert transport.signed_in == [], "nothing was sent"
    assert transport.closed
    assert refused.value.shown == fingerprint(transport.get_remote_server_key())
    assert PASSWORD not in str(refused.value)

    # and with nothing pinned at all, nothing is trusted.
    with pytest.raises(WrongHostKey):
        remote.connect("games.example", 2022, "player", PASSWORD, None)


def test_the_right_key_is_signed_in_to(monkeypatch):
    transport = Handshaken(b"the real key")
    channel = type("Channel", (), {"settimeout": lambda self, seconds: None})()
    client = type("Client", (), {"get_channel": lambda self: channel})()

    monkeypatch.setattr(remote, "_handshake", lambda host, port, timeout: transport)
    monkeypatch.setattr(paramiko.SFTPClient, "from_transport", staticmethod(lambda t: client))

    got = remote.connect("games.example", 2022, "player", PASSWORD, fingerprint(transport.get_remote_server_key()))

    assert got == (transport, client)
    assert transport.signed_in == [("player", PASSWORD)]


def test_a_wrong_key_reaches_whoever_asked_as_itself(tmp_path):
    def opener(host, port, user, password, expected, timeout):
        raise WrongHostKey(host, "SHA256:other")

    folder = SftpFolder("games.example", 2022, "player", PASSWORD, ROOT, FINGERPRINT, opener=opener)

    with pytest.raises(WrongHostKey, match="SHA256:other"):
        folder.names()


def test_a_refused_sign_in_is_a_connection_that_failed(tmp_path):
    def opener(host, port, user, password, expected, timeout):
        raise paramiko.AuthenticationException("Authentication failed.")

    folder = SftpFolder("games.example", 2022, "player", PASSWORD, ROOT, FINGERPRINT, opener=opener)

    with pytest.raises(ConnectionError, match="Authentication failed") as failed:
        folder.names()
    assert PASSWORD not in str(failed.value)
