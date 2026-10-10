"""What the service has to remember, in one SQLite file.

A tenant is one Discord server paired with one game server. The pairing is a key: the
bot hands it to an admin once, the relay sends it with every request, and only its
hash is kept here, so a copy of this file is not a set of working keys.
"""

import hashlib
import json
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    guild_id     TEXT PRIMARY KEY,
    key_hash     TEXT UNIQUE NOT NULL,
    feed_channel TEXT,
    currency     TEXT,
    last_boot    INTEGER NOT NULL DEFAULT 0,
    last_seq     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    guild_id   TEXT NOT NULL,
    discord_id TEXT NOT NULL,
    username   TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);
CREATE TABLE IF NOT EXISTS commands (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    body     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    guild_id   TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- tenants ----

    def create_tenant(self, guild_id):
        """Make the tenant, or give an existing one a new key. Returns the key, once."""
        key = "zb_" + secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO tenants (guild_id, key_hash, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET key_hash = excluded.key_hash",
            (str(guild_id), hash_key(key), int(time.time())))
        self.db.commit()
        return key

    def tenant(self, guild_id):
        return self.db.execute("SELECT * FROM tenants WHERE guild_id = ?", (str(guild_id),)).fetchone()

    def tenant_by_key(self, key):
        if not key:
            return None
        return self.db.execute("SELECT * FROM tenants WHERE key_hash = ?", (hash_key(key),)).fetchone()

    def set_feed(self, guild_id, channel_id):
        self.db.execute("UPDATE tenants SET feed_channel = ? WHERE guild_id = ?",
                        (None if channel_id is None else str(channel_id), str(guild_id)))
        self.db.commit()

    def set_currency(self, guild_id, currency):
        self.db.execute("UPDATE tenants SET currency = ? WHERE guild_id = ?",
                        (json.dumps(currency) if isinstance(currency, dict) else None, str(guild_id)))
        self.db.commit()

    def currency(self, guild_id):
        row = self.tenant(guild_id)
        if not row or not row["currency"]:
            return None
        try:
            return json.loads(row["currency"])
        except json.JSONDecodeError:
            return None

    def set_watermark(self, guild_id, boot, seq):
        self.db.execute("UPDATE tenants SET last_boot = ?, last_seq = ? WHERE guild_id = ?",
                        (int(boot), int(seq), str(guild_id)))
        self.db.commit()

    def remove_tenant(self, guild_id):
        for table in ("tenants", "links", "commands", "snapshots"):
            self.db.execute("DELETE FROM " + table + " WHERE guild_id = ?", (str(guild_id),))
        self.db.commit()

    # ---- links ----

    def link(self, guild_id, discord_id, username):
        self.db.execute("DELETE FROM links WHERE guild_id = ? AND username = ?", (str(guild_id), username))
        self.db.execute("INSERT OR REPLACE INTO links (guild_id, discord_id, username) VALUES (?, ?, ?)",
                        (str(guild_id), str(discord_id), username))
        self.db.commit()

    def unlink(self, guild_id, discord_id):
        self.db.execute("DELETE FROM links WHERE guild_id = ? AND discord_id = ?", (str(guild_id), str(discord_id)))
        self.db.commit()

    def linked_name(self, guild_id, discord_id):
        row = self.db.execute("SELECT username FROM links WHERE guild_id = ? AND discord_id = ?",
                              (str(guild_id), str(discord_id))).fetchone()
        return row["username"] if row else None

    # ---- commands waiting for a relay ----

    def queue_command(self, guild_id, body):
        cursor = self.db.execute("INSERT INTO commands (guild_id, body) VALUES (?, ?)",
                                 (str(guild_id), json.dumps(body)))
        self.db.commit()
        return cursor.lastrowid

    def commands_after(self, guild_id, after):
        """Everything newer than `after`. Asking for it is the receipt for everything older."""
        self.db.execute("DELETE FROM commands WHERE guild_id = ? AND id <= ?", (str(guild_id), int(after)))
        self.db.commit()
        rows = self.db.execute("SELECT id, body FROM commands WHERE guild_id = ? AND id > ? ORDER BY id",
                               (str(guild_id), int(after))).fetchall()
        return [dict(json.loads(row["body"]), id=row["id"]) for row in rows]

    # ---- snapshots ----

    def set_snapshot(self, guild_id, body):
        self.db.execute(
            "INSERT INTO snapshots (guild_id, body, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
            (str(guild_id), body, int(time.time())))
        self.db.commit()

    def snapshot(self, guild_id):
        row = self.db.execute("SELECT body FROM snapshots WHERE guild_id = ?", (str(guild_id),)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["body"])
        except json.JSONDecodeError:
            return None
