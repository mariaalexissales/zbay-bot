"""The relay's client, answered by a hub in this process instead of over HTTP.

Local mode runs the relay's own file-handling code against the bridge folder, and this
stands where the network would be. So the two modes share every line that reads or
writes the game's files, and differ only in how far the records travel.

The relay's code is synchronous and is run in a worker thread; the hub and its store
belong to the event loop's thread. Each call crosses over and waits for the answer.
"""

import asyncio


class Direct:
    def __init__(self, hub, guild_id, loop=None, timeout=30):
        self.hub, self.guild_id, self.timeout = hub, str(guild_id), timeout
        self.loop = loop or asyncio.get_running_loop()

    def _run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=self.timeout)

    def post_events(self, boot, records):
        return self._run(self.hub.ingest(self.guild_id, boot, records))

    def put_snapshot(self, text):
        async def put():
            self.hub.set_snapshot(self.guild_id, text)
        return self._run(put())

    def get_commands(self, after):
        return self._run(self.hub.commands(self.guild_id, after))
