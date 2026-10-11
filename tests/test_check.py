"""--check: says whether the bot would start, and everything to fix if not."""

import paramiko
import pytest

from zbay_service import remote
from zbay_service.__main__ import main, open_bridge, read_settings
from zbay_service.check import check, report
from zbay_service.local import relay_module

from folders import FINGERPRINT, PASSWORD, ROOT, Sftp, out

TOKEN = "tok-SECRET-do-not-print"


def game_folder(tmp_path, enabled=True, out=True):
    folder = tmp_path / "zBay"
    (folder / "bridge").mkdir(parents=True)
    (folder / "discord.json").write_text(
        '// as the mod writes it\n{ "enabled": ' + ("true" if enabled else "false") + ', "endpoint": "", }\n')
    if out:
        (folder / "bridge" / "out_100.log").write_text('{"seq":1,"type":"hello"}\n')
    return folder


def settings(tmp_path, *flags, **environ):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    return read_settings(["--env-file", str(empty), "--check", *flags], dict(environ))


def test_a_local_setup_that_is_ready(tmp_path):
    folder = game_folder(tmp_path)
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN, ZBAY_LOCAL="true", ZBAY_DIR=str(folder), ZBAY_GUILD="123456")

    ok, notes, problems = check(args, token)

    assert ok and problems == []
    assert "token: set" in notes
    assert any("local" in note for note in notes)
    assert any('"enabled" is true' in note for note in notes)
    assert any("written 1 file(s), newest from boot 100" in note for note in notes)


def test_a_game_that_has_not_written_yet_is_not_a_problem(tmp_path):
    folder = game_folder(tmp_path, out=False)
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN, ZBAY_LOCAL="true", ZBAY_DIR=str(folder), ZBAY_GUILD="123456")

    ok, notes, _ = check(args, token)
    assert ok and any("nothing from the game yet" in note for note in notes)


def test_everything_wrong_is_said_at_once(tmp_path):
    folder = game_folder(tmp_path, enabled=False)
    args, token = settings(tmp_path, ZBAY_LOCAL="true", ZBAY_DIR=str(folder), ZBAY_GUILD="my server")

    ok, _, problems = check(args, token)

    assert not ok and len(problems) == 3
    assert any("ZBAY_DISCORD_TOKEN isn't set" in problem for problem in problems)
    assert any("all digits" in problem and "'my server'" in problem for problem in problems)
    assert any('"enabled" isn\'t true' in problem for problem in problems)


def test_a_folder_that_is_not_there(tmp_path):
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN, ZBAY_LOCAL="true",
                           ZBAY_DIR=str(tmp_path / "nowhere"), ZBAY_GUILD="1")

    ok, _, problems = check(args, token)
    assert not ok and "isn't a folder" in problems[0]


def test_local_with_nothing_filled_in(tmp_path):
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN, ZBAY_LOCAL="true")

    ok, _, problems = check(args, token)
    assert not ok
    assert any("ZBAY_GUILD isn't set" in problem for problem in problems)
    assert any("ZBAY_DIR isn't set" in problem for problem in problems)


def test_a_folder_with_no_settings_file_or_a_broken_one(tmp_path):
    folder = tmp_path / "zBay"
    folder.mkdir()
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN, ZBAY_LOCAL="true", ZBAY_DIR=str(folder), ZBAY_GUILD="1")
    assert "no discord.json" in check(args, token)[2][0]

    (folder / "discord.json").write_text('{ "enabled": ')
    assert "doesn't parse" in check(args, token)[2][0]


def test_hosted_needs_only_the_token(tmp_path):
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN)
    ok, notes, _ = check(args, token)
    assert ok and any("relays connect on 127.0.0.1:8080" in note for note in notes)

    args, token = settings(tmp_path)
    assert not check(args, token)[0]


def test_the_token_is_never_printed(tmp_path, capsys):
    folder = game_folder(tmp_path)
    args, token = settings(tmp_path, ZBAY_DISCORD_TOKEN=TOKEN, ZBAY_LOCAL="true", ZBAY_DIR=str(folder), ZBAY_GUILD="1")

    assert report(args, token) == 0
    printed = capsys.readouterr().out

    assert "ready to start" in printed
    assert TOKEN not in printed and "SECRET" not in printed


def test_it_exits_with_what_it_found(tmp_path, monkeypatch, capsys):
    for name in ("ZBAY_DISCORD_TOKEN", "ZBAY_LOCAL", "ZBAY_DIR", "ZBAY_GUILD", "ZBAY_PORT", "ZBAY_HOST", "ZBAY_DB"):
        monkeypatch.delenv(name, raising=False)

    empty = tmp_path / "empty.env"
    empty.write_text("")

    with pytest.raises(SystemExit) as stopped:
        main(["--env-file", str(empty), "--check"])
    assert stopped.value.code == 1
    assert "not ready: 1 thing(s) to fix" in capsys.readouterr().out

    filled = tmp_path / "filled.env"
    filled.write_text("ZBAY_DISCORD_TOKEN=" + TOKEN + "\n")

    with pytest.raises(SystemExit) as stopped:
        main(["--env-file", str(filled), "--check"])
    assert stopped.value.code == 0
    assert TOKEN not in capsys.readouterr().out


# the game's folder on a rented host, over sftp.
SFTP = {"ZBAY_DISCORD_TOKEN": TOKEN, "ZBAY_LOCAL": "true", "ZBAY_GUILD": "123456", "ZBAY_DIR": ROOT,
        "ZBAY_SFTP_HOST": "games.example", "ZBAY_SFTP_PORT": "2022", "ZBAY_SFTP_USER": "player.1a2b",
        "ZBAY_SFTP_PASSWORD": PASSWORD, "ZBAY_SFTP_FINGERPRINT": FINGERPRINT}


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A rented host with the game's folder on it, and the two ways the check reaches one."""
    host = Sftp(tmp_path)
    host.write("discord.json", '// as the mod writes it\n{ "enabled": true, "endpoint": "", }\n')
    out(host, 100, {"seq": 1, "type": "hello"})

    host.presents = FINGERPRINT
    host.signed_in = []

    def presented(address, port, timeout=None):
        return host.presents

    def folder(address, port, user, password, root, expected):
        host.signed_in.append(user)
        return remote_folder(address, port, user, password, root, expected, opener=host.opener)

    remote_folder = remote.SftpFolder
    monkeypatch.setattr(remote, "presented", presented)
    monkeypatch.setattr(remote, "SftpFolder", folder)
    return host


def test_a_rented_host_that_is_ready(tmp_path, host, capsys):
    args, token = settings(tmp_path, **SFTP)

    assert report(args, token) == 0
    printed = capsys.readouterr().out

    assert "read over sftp" in printed
    assert "games.example:2022 is the host in ZBAY_SFTP_FINGERPRINT" in printed
    assert "game folder: sftp://player.1a2b@games.example:2022" + ROOT in printed
    assert '"enabled" is true' in printed and "newest from boot 100" in printed
    assert PASSWORD not in printed and "SECRET" not in printed


def test_the_fingerprint_is_shown_before_anything_is_signed_in_to(tmp_path, host):
    unpinned = dict(SFTP, ZBAY_SFTP_FINGERPRINT="")
    args, token = settings(tmp_path, **unpinned)

    ok, _, problems = check(args, token)

    assert not ok and len(problems) == 1
    assert "ZBAY_SFTP_FINGERPRINT isn't set" in problems[0] and FINGERPRINT in problems[0]
    assert host.signed_in == [], "the password went nowhere"


def test_a_host_showing_another_key_is_not_signed_in_to(tmp_path, host):
    host.presents = "SHA256:somebodyElse"
    args, token = settings(tmp_path, **SFTP)

    ok, _, problems = check(args, token)

    assert not ok and "SHA256:somebodyElse" in problems[0] and "in between" in problems[0]
    assert host.signed_in == []


def test_a_host_that_cannot_be_reached(tmp_path, host, monkeypatch):
    def nobody(address, port, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(remote, "presented", nobody)
    args, token = settings(tmp_path, **SFTP)

    ok, _, problems = check(args, token)
    assert not ok and "can't reach the game host's sftp at games.example:2022" in problems[0]


def test_a_sign_in_the_host_refuses(tmp_path, host):
    def refused(*args):
        raise paramiko.AuthenticationException("Authentication failed.")

    host.opener = refused
    args, token = settings(tmp_path, **SFTP)

    ok, _, problems = check(args, token)
    assert not ok and "couldn't sign in to games.example:2022 as player.1a2b" in problems[0]
    assert PASSWORD not in problems[0]


def test_a_wrong_path_says_what_is_on_the_host(tmp_path, host):
    args, token = settings(tmp_path, **dict(SFTP, ZBAY_DIR="/Zomboid/Lua/zBay"))

    ok, _, problems = check(args, token)
    assert not ok and "there is no /Zomboid/Lua/zBay on the game host" in problems[0]
    assert "at the top there is: srv/" in problems[0]


def test_sftp_settings_left_out_are_each_named(tmp_path, host):
    args, token = settings(tmp_path, **dict(SFTP, ZBAY_SFTP_USER="", ZBAY_SFTP_PASSWORD=""))

    ok, _, problems = check(args, token)
    assert not ok
    assert any("ZBAY_SFTP_USER isn't set" in problem for problem in problems)
    assert any("ZBAY_SFTP_PASSWORD isn't set" in problem for problem in problems)
    assert host.signed_in == []


def test_a_real_start_wants_all_of_them_and_the_password_has_no_flag(tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("")

    with pytest.raises(SystemExit, match="ZBAY_SFTP_FINGERPRINT"):
        read_settings(["--env-file", str(empty)], dict(SFTP, ZBAY_SFTP_FINGERPRINT=""))

    with pytest.raises(SystemExit) as stopped:
        read_settings(["--env-file", str(empty), "--sftp-password", "x"], dict(SFTP))
    assert stopped.value.code == 2, "argparse knows no such flag"

    args, _ = read_settings(["--env-file", str(empty)], dict(SFTP))
    assert args.sftp_password == PASSWORD and args.sftp_port == 2022


def test_the_bridge_is_over_whichever_folder_the_settings_name(tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    relay = relay_module()

    args, _ = read_settings(["--env-file", str(empty)], dict(SFTP, ZBAY_STATE=str(tmp_path / "place.json")))
    bridge = open_bridge(args, relay)
    assert isinstance(bridge.folder, remote.SftpFolder) and bridge.folder.root == ROOT
    assert bridge.state_path == tmp_path / "place.json", "its place is kept here, not on the host"

    disk = game_folder(tmp_path)
    args, _ = read_settings(["--env-file", str(empty)],
                            {"ZBAY_DISCORD_TOKEN": TOKEN, "ZBAY_LOCAL": "true", "ZBAY_GUILD": "1", "ZBAY_DIR": str(disk)})
    bridge = open_bridge(args, relay)
    assert isinstance(bridge.folder, relay.Folder)
    assert bridge.state_path == disk / "bridge" / "relay_state.json"
