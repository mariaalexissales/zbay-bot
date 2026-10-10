"""Settings: the .env file, the real environment over it, and flags over both."""

from pathlib import Path

import pytest

from zbay_service import env
from zbay_service.__main__ import read_settings

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_file_is_read_as_it_is_written():
    assert env.parse(r"""
# a comment
ZBAY_DB=zbay.sqlite

export ZBAY_HOST = 0.0.0.0
ZBAY_DIR=C:\Users\you\Zomboid\Lua\zBay
ZBAY_SETUP_URL=https://example.test/setup#start   # where owners are sent
QUOTED="kept # whole"
SINGLE='also kept'
EQUALS=a=b=c
EMPTY=
not a setting
=nothing
""") == {
        "ZBAY_DB": "zbay.sqlite",
        "ZBAY_HOST": "0.0.0.0",
        # a backslash is a backslash: nothing in a path is taken for an escape.
        "ZBAY_DIR": r"C:\Users\you\Zomboid\Lua\zBay",
        # a hash with no space before it belongs to the value.
        "ZBAY_SETUP_URL": "https://example.test/setup#start",
        "QUOTED": "kept # whole",
        "SINGLE": "also kept",
        "EQUALS": "a=b=c",
        "EMPTY": "",
    }


def test_a_byte_order_mark_from_notepad_is_not_part_of_the_first_name(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfZBAY_DB=x.sqlite\n")

    environ = {}
    assert env.load(path, environ) == ["ZBAY_DB"] and environ == {"ZBAY_DB": "x.sqlite"}


def test_the_real_environment_wins_over_the_file(tmp_path):
    path = write(tmp_path, "ZBAY_DISCORD_TOKEN=from-file\nZBAY_PORT=9000\nZBAY_DB=\n")
    environ = {"ZBAY_DISCORD_TOKEN": "from-host", "ZBAY_DB": "host.sqlite"}

    assert env.load(path, environ) == ["ZBAY_PORT"]
    assert environ == {"ZBAY_DISCORD_TOKEN": "from-host", "ZBAY_PORT": "9000", "ZBAY_DB": "host.sqlite"}


def test_where_the_file_is_looked_for(tmp_path):
    assert env.find(str(tmp_path / "nope.env")) is None

    elsewhere = tmp_path / "other.env"
    elsewhere.write_text("A=1")
    assert env.find(str(elsewhere)) == elsewhere

    here = write(tmp_path, "A=1")
    assert env.find(None, tmp_path) == here


def test_settings_come_from_the_file(tmp_path):
    path = write(tmp_path, """
ZBAY_DISCORD_TOKEN=tok
ZBAY_DB=/data/zbay.sqlite
ZBAY_HOST=0.0.0.0
ZBAY_PORT=9000
ZBAY_SETUP_URL=https://example.test
""")
    args, token = read_settings(["--env-file", str(path)], {})

    assert token == "tok"
    assert (args.db, args.host, args.port, args.setup_url) == ("/data/zbay.sqlite", "0.0.0.0", 9000, "https://example.test")
    assert args.local is False


def test_with_nothing_set_the_defaults_are_the_documented_ones(tmp_path):
    args, _ = read_settings(["--env-file", str(write(tmp_path, "ZBAY_DISCORD_TOKEN=tok\n"))], {})

    assert (args.db, args.host, args.port, args.interval) == ("zbay.sqlite", "127.0.0.1", 8080, 2.0)
    assert args.local is False and args.setup_url is None


def test_the_environment_beats_the_file_and_a_flag_beats_both(tmp_path):
    path = write(tmp_path, "ZBAY_DISCORD_TOKEN=tok\nZBAY_PORT=9000\nZBAY_HOST=1.1.1.1\nZBAY_DB=file.sqlite\n")

    args, _ = read_settings(["--env-file", str(path), "--db", "flag.sqlite"], {"ZBAY_HOST": "2.2.2.2", "ZBAY_DB": "host.sqlite"})

    assert args.port == 9000            # only the file says
    assert args.host == "2.2.2.2"       # the environment over the file
    assert args.db == "flag.sqlite"     # a flag over both


def test_local_mode_from_the_file(tmp_path):
    path = write(tmp_path, "ZBAY_DISCORD_TOKEN=tok\nZBAY_LOCAL=true\nZBAY_DIR=C:\\Zomboid\\Lua\\zBay\nZBAY_GUILD=123\nZBAY_INTERVAL=0.5\n")

    args, _ = read_settings(["--env-file", str(path)], {})
    assert args.local is True
    assert (args.zbay_dir, args.guild, args.interval) == ("C:\\Zomboid\\Lua\\zBay", "123", 0.5)

    # and a file that says local can still be run hosted.
    args, _ = read_settings(["--env-file", str(path), "--no-local"], {})
    assert args.local is False


def test_what_is_said_when_something_is_missing(tmp_path):
    empty = write(tmp_path, "# nothing yet\n")

    with pytest.raises(SystemExit, match="ZBAY_DISCORD_TOKEN isn't set"):
        read_settings(["--env-file", str(empty)], {})

    with pytest.raises(SystemExit, match="ZBAY_DIR and ZBAY_GUILD"):
        read_settings(["--env-file", str(empty), "--local"], {"ZBAY_DISCORD_TOKEN": "tok"})

    with pytest.raises(SystemExit, match="ZBAY_PORT should be a number"):
        read_settings(["--env-file", str(empty)], {"ZBAY_DISCORD_TOKEN": "tok", "ZBAY_PORT": "eighty"})

    with pytest.raises(SystemExit, match="no settings file at"):
        read_settings(["--env-file", str(tmp_path / "missing.env")], {"ZBAY_DISCORD_TOKEN": "tok"})


def test_the_token_has_no_flag(tmp_path, capsys):
    with pytest.raises(SystemExit):
        read_settings(["--env-file", str(write(tmp_path, "")), "--token", "tok"], {})
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_example_file_is_complete_and_holds_no_secrets():
    example = env.parse(EXAMPLE.read_text(encoding="utf-8"))

    # every setting the bot reads is in the example, so the example is the documentation.
    source = (EXAMPLE.parent / "service/zbay_service/__main__.py").read_text(encoding="utf-8")
    import re
    read = set(re.findall(r'"(ZBAY_[A-Z_]+)"', source))
    assert read <= set(example), read - set(example)

    assert example["ZBAY_DISCORD_TOKEN"] == ""
    assert "ZBAY_RCON_PASSWORD" not in example, "it is there commented out, with no value"

    # and running on it as it stands is hosted mode, short only of a token.
    with pytest.raises(SystemExit, match="ZBAY_DISCORD_TOKEN isn't set"):
        read_settings(["--env-file", str(EXAMPLE)], {})
    args, _ = read_settings(["--env-file", str(EXAMPLE)], {"ZBAY_DISCORD_TOKEN": "tok"})
    assert args.local is False and args.port == 8080 and args.zbay_dir is None
