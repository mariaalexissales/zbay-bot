"""The middle: everything the service does, with Discord and HTTP kept at arm's length.

A relay hands records in; a Discord command asks for something. What comes out goes
through a Sink, which is the bot in production and a list in the tests.
"""

import asyncio
import secrets
import string
import time

from . import render

# how long a link code is good for. it has to be typed into the game by hand.
LINK_MINUTES = 10

CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")


class Sink:
    """Where messages go. The bot implements this."""

    async def post(self, guild_id, channel_id, text):
        raise NotImplementedError

    async def dm(self, discord_id, text):
        raise NotImplementedError


class Hub:
    def __init__(self, store, sink):
        self.store = store
        self.sink = sink
        # replyTo -> the future a /zbay balance is waiting on.
        self.questions = {}
        # guild -> an event set when a command is queued, for a relay holding a request open.
        self.waiters = {}

    # ---- from the relay ----

    async def ingest(self, guild_id, boot, records):
        """Take a batch of what the game wrote. Returns the last seq now held.

        The game numbers its records and never reuses a number, so a batch sent twice is
        told apart from a new one by the numbers alone. The exception is a world that was
        wiped or rolled back, which counts from wherever its save says; that shows up as
        a newer boot, and a newer boot is always listened to from its first record.
        """
        tenant = self.store.tenant(guild_id)
        if not tenant:
            return 0

        boot = int(boot or 0)
        last_boot, last_seq = tenant["last_boot"], tenant["last_seq"]

        if boot < last_boot:
            return last_seq
        if boot > last_boot:
            last_boot, last_seq = boot, 0

        currency = self.currency(guild_id)

        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                seq = int(record.get("seq"))
            except (TypeError, ValueError):
                continue
            if seq <= last_seq:
                continue

            last_seq = seq

            # the game says what its money is called in the first record of every boot, and
            # again when the owner changes it. everything after is printed in that.
            if record.get("type") in ("hello", "config") and isinstance(record.get("currency"), dict):
                currency = record["currency"]
                self.store.set_currency(guild_id, currency)
                continue

            try:
                await self._handle(tenant, record, currency)
            except Exception as err:  # one bad record is not a reason to refuse the rest.
                print(f"[{guild_id}] record {seq} failed: {err!r}")

        self.store.set_watermark(guild_id, last_boot, last_seq)
        return last_seq

    async def _handle(self, tenant, record, currency):
        guild_id = tenant["guild_id"]
        kind = record.get("type")

        if kind == "notice":
            text = render.notice_text(record, currency)
            if text and record.get("discordId"):
                await self.sink.dm(str(record["discordId"]), text)
            return

        if kind == "linked":
            self.store.link(guild_id, record.get("discordId"), str(record.get("user")))
            await self.sink.dm(str(record.get("discordId")),
                               f"Linked to **{render.clean(record.get('user'))}** in game. "
                               "You'll get a message here when something you're selling or bidding on moves.")
            return

        if kind == "unlinked":
            self.store.unlink(guild_id, record.get("discordId"))
            return

        if kind == "reply":
            question = self.questions.pop(str(record.get("replyTo")), None)
            if question and not question.done():
                question.set_result(record)
            return

        text = render.event_text(record, currency)
        if text and tenant["feed_channel"]:
            await self.sink.post(guild_id, tenant["feed_channel"], text)

    def set_snapshot(self, guild_id, body):
        self.store.set_snapshot(guild_id, body)

    def _queue(self, guild_id, body):
        self.store.queue_command(guild_id, body)
        waiter = self.waiters.get(str(guild_id))
        if waiter:
            waiter.set()

    async def commands(self, guild_id, after, wait=0):
        """What the relay should write into the game's inbox. Holds the request open for
        up to `wait` seconds when there is nothing yet."""
        found = self.store.commands_after(guild_id, after)
        if found or wait <= 0:
            return found

        waiter = self.waiters.setdefault(str(guild_id), asyncio.Event())
        waiter.clear()
        try:
            await asyncio.wait_for(waiter.wait(), timeout=wait)
        except asyncio.TimeoutError:
            return []
        return self.store.commands_after(guild_id, after)

    # ---- from discord ----

    def setup(self, guild_id):
        """Pair a Discord server with a game server. The key is shown once and not kept."""
        return self.store.create_tenant(guild_id)

    def is_set_up(self, guild_id):
        return self.store.tenant(guild_id) is not None

    def set_feed(self, guild_id, channel_id):
        self.store.set_feed(guild_id, channel_id)

    def forget(self, guild_id):
        self.store.remove_tenant(guild_id)

    def offer_link(self, guild_id, discord_id):
        """A code for the player to type into the game's bank tab."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) + "-" + \
               "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))

        self._queue(guild_id, {
            "type": "link_offer",
            "code": code,
            "discordId": str(discord_id),
            "expires": int((time.time() + LINK_MINUTES * 60) * 1000),
        })
        return code

    def unlink(self, guild_id, discord_id):
        was = self.store.linked_name(guild_id, discord_id)
        self.store.unlink(guild_id, discord_id)
        self._queue(guild_id, {"type": "unlink", "discordId": str(discord_id)})
        return was

    def linked_name(self, guild_id, discord_id):
        return self.store.linked_name(guild_id, discord_id)

    async def ask_balance(self, guild_id, discord_id, timeout=15):
        """Ask the game. None when it didn't answer in time, which is a server that is
        down or a relay that isn't running."""
        reply_to = secrets.token_hex(8)
        question = asyncio.get_running_loop().create_future()
        self.questions[reply_to] = question

        self._queue(guild_id, {"type": "balance_query", "discordId": str(discord_id), "replyTo": reply_to})

        try:
            return await asyncio.wait_for(question, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.questions.pop(reply_to, None)

    def search(self, guild_id, text=None, kind=None, limit=10):
        """(lines, how many matched, the snapshot or None when the game has sent none)."""
        snapshot = self.store.snapshot(guild_id)
        lines, total = render.search(snapshot, text, kind, limit)
        return lines, total, snapshot

    def currency(self, guild_id):
        # what the game last said outright, or failing that what its last snapshot carried.
        return self.store.currency(guild_id) or (self.store.snapshot(guild_id) or {}).get("currency")
