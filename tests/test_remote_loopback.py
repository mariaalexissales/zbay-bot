"""The game's folder over SFTP, against a real SFTP server on this machine.

test_remote.py plays paramiko's client with a stand-in. Here nothing is played: paramiko's
own server is started on a local port over a folder on disk, and the bot's code signs in to
it the way it will to a game host. So the handshake, the host key, the sign-in and every
file operation cross a real wire.

What it does not show is that a particular game host's SFTP server behaves the same. Theirs
is other software, and --check on the first real run is where a difference would surface.
"""

import json
import os
import socket
import threading

import paramiko
import pytest

from zbay_relay import Bridge, step
from zbay_service import remote
from zbay_service.__main__ import read_settings
from zbay_service.check import report
from zbay_service.remote import SftpFolder, WrongHostKey, fingerprint

from test_relay import FakeClient

USER, PASSWORD = "player.1a2b", "loopback-SECRET"


class Auth(paramiko.ServerInterface):
    def __init__(self, attempts):
        self.attempts = attempts

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        self.attempts.append(username)
        good = username == USER and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if good else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class Files(paramiko.SFTPServerInterface):
    """A folder on disk, served. As little as an SFTP server can be."""

    def __init__(self, server, root):
        self.root = str(root)

    def _real(self, path):
        return os.path.join(self.root, *[part for part in path.split("/") if part])

    def _failed(self, err):
        return paramiko.SFTPServer.convert_errno(err.errno)

    def list_folder(self, path):
        try:
            out = []
            for name in os.listdir(self._real(path)):
                attrs = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                attrs.filename = name
                out.append(attrs)
            return out
        except OSError as err:
            return self._failed(err)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as err:
            return self._failed(err)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            fd = os.open(self._real(path), flags | getattr(os, "O_BINARY", 0), 0o666)
        except OSError as err:
            return self._failed(err)

        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"

        handle = paramiko.SFTPHandle(flags)
        handle.readfile = handle.writefile = os.fdopen(fd, mode)
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as err:
            return self._failed(err)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as err:
            return self._failed(err)
        return paramiko.SFTP_OK


@pytest.fixture(scope="module")
def host_key():
    return paramiko.RSAKey.generate(2048)


@pytest.fixture
def host(tmp_path, host_key):
    """An SFTP server over tmp_path, with the game's folder at /Zomboid/Lua/zBay."""
    game = tmp_path / "Zomboid" / "Lua" / "zBay"
    (game / "bridge").mkdir(parents=True)

    try:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
    except OSError as err:
        pytest.skip("can't listen on a local port here: " + str(err))

    attempts, transports = [], []

    def serve():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(connection)
            transport.add_server_key(host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files, tmp_path)
            transports.append(transport)
            try:
                transport.start_server(server=Auth(attempts))
            except (paramiko.SSHException, EOFError, OSError):
                pass

    threading.Thread(target=serve, daemon=True).start()

    class Host:
        pass

    host = Host()
    host.port = listener.getsockname()[1]
    host.game, host.attempts = game, attempts
    host.fingerprint = fingerprint(host_key)
    host.opened = []

    def folder(password=PASSWORD, pinned=None):
        made = SftpFolder("127.0.0.1", host.port, USER, password, "/Zomboid/Lua/zBay",
                          pinned or host.fingerprint, timeout=10)
        host.opened.append(made)
        return made

    host.folder = folder
    yield host

    for made in host.opened:
        made.close()
    listener.close()
    for transport in transports:
        transport.close()


def test_the_host_shows_its_key_without_being_signed_in_to(host):
    assert remote.presented("127.0.0.1", host.port, timeout=10) == host.fingerprint
    assert host.attempts == []


def test_a_host_showing_another_key_never_sees_the_password(host):
    folder = host.folder(pinned="SHA256:notThisOne")

    with pytest.raises(WrongHostKey) as refused:
        folder.names()

    assert refused.value.shown == host.fingerprint
    assert host.attempts == [], "no sign-in was attempted"


def test_a_wrong_password_is_a_connection_that_failed(host):
    with pytest.raises(ConnectionError, match="(?i)authentication"):
        host.folder(password="not it").names()
    assert host.attempts == [USER]


def test_every_file_operation_over_the_wire(host):
    folder = host.folder()
    (host.game / "discord.json").write_text("{}")
    (host.game / "bridge" / "out_1.log").write_bytes(b"0123456789")

    assert folder.names() == ["discord.json"]
    assert folder.names("bridge") == ["out_1.log"]
    assert folder.names("nowhere") == []
    assert folder.listing("/Zomboid/Lua/zBay") == ["bridge/", "discord.json"]
    assert folder.listing("/nowhere") is None

    assert folder.read("bridge/out_1.log") == b"0123456789"
    assert folder.read("bridge/out_1.log", 4, 3) == b"456"
    assert folder.read("bridge/missing.log") is None

    stamp = folder.stamp("bridge/out_1.log")
    assert stamp[1] == 10 and folder.stamp("bridge/missing.log") is None

    folder.append("bridge/in.log", b"one\n")
    folder.append("bridge/in.log", b"two\n")
    assert (host.game / "bridge" / "in.log").read_bytes() == b"one\ntwo\n"

    folder.truncate("bridge/in.log")
    assert (host.game / "bridge" / "in.log").read_bytes() == b""

    folder.remove("bridge/out_1.log")
    assert not (host.game / "bridge" / "out_1.log").exists()

    folder.append("made/later.log", b"x")
    assert (host.game / "made" / "later.log").read_bytes() == b"x"


def test_the_bridge_runs_over_it(host, tmp_path):
    bridge_dir = host.game / "bridge"
    (bridge_dir / "out_100.log").write_bytes(b'{"seq":1,"type":"hello"}\n{"seq":2,"type":"sale"}\n')
    (bridge_dir / "snapshot.json").write_text('{"listings":[],"rev":1}')

    bridge = Bridge(folder=host.folder(), state_path=tmp_path / "bot" / "place.json")
    client = FakeClient()
    client.commands = [{"id": 5, "type": "link_offer", "code": "AB-CD", "discordId": "42"}]

    done = step(bridge, client)

    assert done == {"events": 2, "snapshot": True, "commands": 1}
    assert client.events == [(100, [{"seq": 1, "type": "hello"}, {"seq": 2, "type": "sale"}])]
    assert client.snapshots == ['{"listings":[],"rev":1}']

    written = json.loads((bridge_dir / "in.log").read_text())
    assert written["type"] == "link_offer" and written["seq"] == 1

    assert (tmp_path / "bot" / "place.json").exists(), "the bot's place is kept on its own machine"
    assert not (bridge_dir / "relay_state.json").exists(), "and not on the game host"

    # the mod acts on it and says so; a newer boot's file appears. the inbox is cleared and
    # the old out file, drained, is removed.
    (bridge_dir / "in_ack.json").write_text(json.dumps({"seq": 1, "epoch": bridge.state["epoch"]}))
    (bridge_dir / "out_200.log").write_bytes(b'{"seq":3,"type":"hello"}\n')

    step(bridge, client)
    assert (bridge_dir / "in.log").read_bytes() == b""
    assert not (bridge_dir / "out_100.log").exists()
    assert client.events[-1] == (200, [{"seq": 3, "type": "hello"}])


def test_check_against_it(host, tmp_path, capsys):
    (host.game / "discord.json").write_text('// as the mod writes it\n{ "enabled": true, }\n')
    (host.game / "bridge" / "out_100.log").write_bytes(b'{"seq":1,"type":"hello"}\n')

    empty = tmp_path / "empty.env"
    empty.write_text("")
    environ = {"ZBAY_DISCORD_TOKEN": "tok-SECRET", "ZBAY_LOCAL": "true", "ZBAY_GUILD": "123456",
               "ZBAY_DIR": "/Zomboid/Lua/zBay", "ZBAY_SFTP_HOST": "127.0.0.1", "ZBAY_SFTP_PORT": str(host.port),
               "ZBAY_SFTP_USER": USER, "ZBAY_SFTP_PASSWORD": PASSWORD}

    # first with nothing pinned: it says what the host presents, and signs in to nothing.
    args, token = read_settings(["--env-file", str(empty), "--check"], dict(environ))
    assert report(args, token) == 1
    printed = capsys.readouterr().out
    assert host.fingerprint in printed and host.attempts == []

    # then with what it said put in.
    args, token = read_settings(["--env-file", str(empty), "--check"],
                                dict(environ, ZBAY_SFTP_FINGERPRINT=host.fingerprint))
    assert report(args, token) == 0
    printed = capsys.readouterr().out

    assert "ready to start" in printed and "newest from boot 100" in printed
    assert PASSWORD not in printed and "SECRET" not in printed
