"""The game's folder, two ways, for tests that should not care which.

Disk is a folder under tmp_path, read through the relay's own Folder. Sftp is the same
files held in memory behind a stand-in for paramiko's client, read through SftpFolder. A
test written against either runs against both, which is what shows the bridge asks a
folder for nothing an SFTP host can't give.
"""

import errno
import json
import stat
from types import SimpleNamespace

from zbay_relay import Bridge
from zbay_service.remote import SftpFolder

ROOT = "/srv/Zomboid/Lua/zBay"
FINGERPRINT = "SHA256:abcDEF123"
PASSWORD = "sftp-SECRET-do-not-print"


def missing(path):
    return FileNotFoundError(errno.ENOENT, "No such file", path)


class FakeFile:
    def __init__(self, sftp, path, mode):
        self.sftp, self.path, self.mode, self.pos = sftp, path, mode, 0

        if "r" in mode:
            if path not in sftp.files:
                raise missing(path)
            return

        if path.rsplit("/", 1)[0] not in sftp.dirs:
            raise missing(path)
        if "w" in mode or path not in sftp.files:
            sftp.files[path] = b"" if "w" in mode else sftp.files.get(path, b"")
            sftp.touch(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        pass

    def seek(self, offset):
        self.pos = offset

    def read(self, size=None):
        self.sftp.alive()
        data = self.sftp.files[self.path][self.pos:]
        data = data if size is None else data[:size]
        self.pos += len(data)
        return data

    def write(self, data):
        self.sftp.alive()
        self.sftp.files[self.path] += bytes(data)
        self.sftp.touch(self.path)


class FakeSftp:
    """paramiko's SFTPClient, as far as SftpFolder leans on it, over a dict.

    down = True is the connection having gone: every call raises what paramiko raises
    when its channel closes.
    """

    def __init__(self):
        self.files, self.mtimes = {}, {}
        self.dirs = {"/", "/srv", "/srv/Zomboid", "/srv/Zomboid/Lua", ROOT, ROOT + "/bridge"}
        self.clock = 0
        self.down = False

    def alive(self):
        if self.down:
            raise EOFError()

    def touch(self, path):
        self.clock += 1
        self.mtimes[path] = self.clock

    def _attrs(self, path, name=None):
        if path in self.files:
            return SimpleNamespace(filename=name, st_mode=stat.S_IFREG | 0o644,
                                   st_size=len(self.files[path]), st_mtime=self.mtimes[path])
        if path in self.dirs:
            return SimpleNamespace(filename=name, st_mode=stat.S_IFDIR | 0o755, st_size=0, st_mtime=0)
        raise missing(path)

    def listdir_attr(self, path):
        self.alive()
        path = path.rstrip("/") or "/"
        if path not in self.dirs:
            raise missing(path)

        prefix = path.rstrip("/") + "/"
        inside = [p for p in list(self.files) + list(self.dirs) if p.startswith(prefix) and "/" not in p[len(prefix):]]
        return [self._attrs(p, p[len(prefix):]) for p in inside if p != path]

    def stat(self, path):
        self.alive()
        return self._attrs(path)

    def open(self, path, mode="r"):
        self.alive()
        return FakeFile(self, path, mode)

    def remove(self, path):
        self.alive()
        if path not in self.files:
            raise missing(path)
        del self.files[path]

    def mkdir(self, path):
        self.alive()
        self.dirs.add(path)


class FakeTransport:
    def close(self):
        pass


class Disk:
    """The game's folder on this machine."""

    kind = "disk"

    def __init__(self, tmp_path):
        self.root = tmp_path
        (tmp_path / "bridge").mkdir()

    def bridge(self):
        return Bridge(self.root)

    def write(self, name, data):
        (self.root / name).write_bytes(data.encode() if isinstance(data, str) else data)

    def append(self, name, data):
        with open(self.root / name, "ab") as handle:
            handle.write(data.encode() if isinstance(data, str) else data)

    def read(self, name):
        return (self.root / name).read_text(encoding="utf-8")

    def exists(self, name):
        return (self.root / name).exists()

    def size(self, name):
        return (self.root / name).stat().st_size


class Sftp:
    """The game's folder on a rented host, reached the way the bot reaches one."""

    kind = "sftp"

    def __init__(self, tmp_path):
        self.sftp = FakeSftp()
        self.state = tmp_path / "state" / "relay_state.json"
        self.opened = 0

    def opener(self, host, port, user, password, expected, timeout):
        self.opened += 1
        self.sftp.alive()
        return FakeTransport(), self.sftp

    def folder(self, root=ROOT):
        return SftpFolder("games.example", 2022, "player.1a2b", PASSWORD, root, FINGERPRINT, opener=self.opener)

    def bridge(self):
        return Bridge(folder=self.folder(), state_path=self.state)

    def write(self, name, data):
        path = ROOT + "/" + name
        self.sftp.files[path] = data.encode() if isinstance(data, str) else data
        self.sftp.touch(path)

    def append(self, name, data):
        path = ROOT + "/" + name
        self.sftp.files[path] = self.sftp.files.get(path, b"") + (data.encode() if isinstance(data, str) else data)
        self.sftp.touch(path)

    def read(self, name):
        return self.sftp.files[ROOT + "/" + name].decode("utf-8")

    def exists(self, name):
        return ROOT + "/" + name in self.sftp.files

    def size(self, name):
        return len(self.sftp.files[ROOT + "/" + name])


def out(world, boot, *records, tail=""):
    """What the mod does: add records to this boot's out file. Returns the file's name."""
    name = f"bridge/out_{boot}.log"
    world.append(name, b"".join(json.dumps(record).encode() + b"\n" for record in records) + tail.encode())
    return name
