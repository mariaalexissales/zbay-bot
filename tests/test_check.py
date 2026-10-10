"""--check: says whether the bot would start, and everything to fix if not."""

import pytest

from zbay_service.__main__ import main, read_settings
from zbay_service.check import check, report

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
