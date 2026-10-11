"""The game's folder when it is on somebody else's machine, read over SFTP.

A rented game host gives its customers the server's files over SFTP and nothing else:
there is no running a program beside the game. So the folder is reached from here
instead. SftpFolder answers the same six questions the relay's Folder does, which is all
the bridge ever asks, so nothing else in the bot knows the difference.

The host's key is pinned. Its fingerprint has to match the one in the settings before the
password is sent, and one that doesn't match is refused rather than learned.
"""

import base64
import hashlib
import posixpath
import socket
import stat

import paramiko

TIMEOUT = 20

# seconds between keepalives. a game host's firewall drops a quiet connection sooner than
# a quiet market goes between events.
KEEPALIVE = 30


class WrongHostKey(ConnectionError):
    """The host showed a key other than the pinned one. `shown` is the one it showed."""

    def __init__(self, host, shown):
        super().__init__(f"{host} presented the host key {shown}, which is not the one in ZBAY_SFTP_FINGERPRINT")
        self.shown = shown


def fingerprint(key):
    """A host key's SHA256 fingerprint, written the way ssh and most panels write it."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def same_fingerprint(a, b):
    """Whether two fingerprints are the same one, however each was copied out."""
    def bare(text):
        text = str(text or "").strip()
        if text.upper().startswith("SHA256:"):
            text = text[7:]
        return text.rstrip("=")

    return bare(a) != "" and bare(a) == bare(b)


def _handshake(host, port, timeout):
    transport = paramiko.Transport(socket.create_connection((host, port), timeout=timeout))
    try:
        transport.start_client(timeout=timeout)
    except BaseException:
        transport.close()
        raise
    return transport


def presented(host, port, timeout=TIMEOUT):
    """The fingerprint a host shows, without signing in to it. For --check."""
    try:
        transport = _handshake(host, port, timeout)
    except (paramiko.SSHException, EOFError) as err:
        raise ConnectionError(f"sftp to {host}: {err}") from err

    try:
        return fingerprint(transport.get_remote_server_key())
    finally:
        transport.close()


def connect(host, port, user, password, expected, timeout=TIMEOUT):
    """Open a session. Returns (transport, sftp client).

    The key is checked after the handshake and before the password goes anywhere.
    """
    transport = _handshake(host, port, timeout)
    try:
        shown = fingerprint(transport.get_remote_server_key())
        if not same_fingerprint(shown, expected):
            raise WrongHostKey(host, shown)

        transport.auth_password(user, password)
        transport.set_keepalive(KEEPALIVE)

        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.get_channel().settimeout(timeout)
        return transport, sftp
    except BaseException:
        transport.close()
        raise


class SftpFolder:
    """A folder on an SFTP host, with the relay Folder's methods.

    One session is kept open and opened again when it drops. A file that isn't there is
    None or an empty list, as on disk. A host that can't be reached is a ConnectionError,
    so the bridge never mistakes an outage for a file having gone.
    """

    def __init__(self, host, port, user, password, root, expected, timeout=TIMEOUT, opener=connect):
        self.host, self.port, self.user = host, int(port), user
        self.root = "/" + str(root).replace("\\", "/").strip("/")
        self._password, self._expected, self._timeout = password, expected, timeout
        self._opener = opener
        self._transport = self._sftp = None

    def __str__(self):
        return f"sftp://{self.user}@{self.host}:{self.port}{self.root}"

    def _path(self, name):
        return posixpath.join(self.root, name) if name else self.root

    def close(self):
        transport, self._transport, self._sftp = self._transport, None, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _do(self, action):
        """Run one action on the session, opening it first if need be.

        A missing file and a refused one come out as they do on disk. Anything else means
        the connection is no good: the session is dropped, so the next call opens a new
        one, and the caller is told it could not reach the host.
        """
        try:
            if self._sftp is None:
                self._transport, self._sftp = self._opener(
                    self.host, self.port, self.user, self._password, self._expected, self._timeout)
            return action(self._sftp)
        except (FileNotFoundError, PermissionError, WrongHostKey):
            raise
        except (paramiko.SSHException, EOFError, OSError) as err:
            self.close()
            raise ConnectionError(f"sftp to {self.host}: {err or type(err).__name__}") from err

    def names(self, sub=""):
        def action(sftp):
            try:
                entries = sftp.listdir_attr(self._path(sub))
            except FileNotFoundError:
                return []
            return sorted(e.filename for e in entries if stat.S_ISREG(e.st_mode or 0))

        return self._do(action)

    def read(self, name, offset=0, limit=None):
        def action(sftp):
            try:
                with sftp.open(self._path(name), "rb") as handle:
                    if offset:
                        handle.seek(offset)
                    return handle.read(limit) if limit else handle.read()
            except FileNotFoundError:
                return None

        return self._do(action)

    def stamp(self, name):
        def action(sftp):
            try:
                attrs = sftp.stat(self._path(name))
            except FileNotFoundError:
                return None
            return [attrs.st_mtime, attrs.st_size]

        return self._do(action)

    def append(self, name, data):
        """Add to the end of a file, making it, and the folder it is in, if need be."""
        def action(sftp):
            target = self._path(name)
            try:
                handle = sftp.open(target, "ab")
            except FileNotFoundError:
                sftp.mkdir(posixpath.dirname(target))
                handle = sftp.open(target, "ab")
            with handle:
                handle.write(data)

        self._do(action)

    def truncate(self, name):
        self._do(lambda sftp: sftp.open(self._path(name), "wb").close())

    def remove(self, name):
        self._do(lambda sftp: sftp.remove(self._path(name)))

    def listing(self, absolute):
        """What is at a path on the host, folders marked with a slash, or None when there
        is no such path. For --check, to help find where a host keeps the game's folder."""
        def action(sftp):
            try:
                entries = sftp.listdir_attr(absolute)
            except FileNotFoundError:
                return None
            return sorted(e.filename + ("/" if stat.S_ISDIR(e.st_mode or 0) else "") for e in entries)

        return self._do(action)
