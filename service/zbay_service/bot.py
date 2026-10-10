"""The Discord half: slash commands in, messages out.

Everything it knows it asks the hub. Nothing here touches the store or a relay
directly, which is what lets the same bot run hosted for many servers or beside one.
"""

import discord
from discord import app_commands

from . import render
from .hub import LINK_MINUTES, Hub, Sink

# only what slash commands and direct messages need. no message content, no member list.
INTENTS = discord.Intents.none()
INTENTS.guilds = True

NOT_SET_UP = "zBay isn't set up on this server yet. An admin can run `/zbay setup`."
NO_SNAPSHOT = ("Nothing from the game server yet. If it's running, check that the relay is too "
               "and that `enabled` is true in discord.json.")


class ZBayBot(discord.Client, Sink):
    def __init__(self, store, setup_url=None, guild_only=None):
        super().__init__(intents=INTENTS, allowed_mentions=discord.AllowedMentions.none())

        self.hub = Hub(store, self)
        self.setup_url = setup_url
        # set in local mode: the one Discord server this copy of the bot is for.
        self.guild_only = int(guild_only) if guild_only else None
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(self._commands())

    async def setup_hook(self):
        if self.guild_only:
            # registered to the one server, so the commands appear at once instead of in an hour.
            guild = discord.Object(id=self.guild_only)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"zbay bot ready as {self.user} in {len(self.guilds)} server(s)")

    async def on_guild_remove(self, guild):
        # kicked or the server was deleted: its key stops working and nothing of it is kept.
        if not self.guild_only:
            self.hub.forget(guild.id)

    # ---- Sink ----

    async def post(self, guild_id, channel_id, text):
        channel = self.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(text)
        except discord.HTTPException as err:
            print(f"[{guild_id}] could not post to {channel_id}: {err}")

    async def dm(self, discord_id, text):
        try:
            user = self.get_user(int(discord_id)) or await self.fetch_user(int(discord_id))
            await user.send(text)
        except (discord.HTTPException, ValueError):
            # direct messages closed, or the account is gone. nothing to do about either.
            pass

    # ---- commands ----

    def _commands(self):
        group = app_commands.Group(name="zbay", description="The zBay marketplace", guild_only=True)
        hub = self.hub

        async def ready(interaction):
            if hub.is_set_up(interaction.guild_id):
                return True
            await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
            return False

        @group.command(name="setup", description="Pair this Discord server with your game server")
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(new_key="Replace the server key. The old one stops working at once.")
        async def setup(interaction: discord.Interaction, new_key: bool = False):
            if not interaction.permissions.manage_guild:
                await interaction.response.send_message("You need Manage Server for that.", ephemeral=True)
                return

            if hub.is_set_up(interaction.guild_id) and not new_key:
                await interaction.response.send_message(
                    "This server is already set up. The key is only ever shown once: run "
                    "`/zbay setup new_key:True` if you need a new one. The old one stops working.",
                    ephemeral=True)
                return

            key = hub.setup(interaction.guild_id)
            lines = [
                "**Your server key** (shown once, keep it private):",
                f"```{key}```",
                'Put it in `serverKey` in `Zomboid/Lua/zBay/discord.json` on the game server, '
                'set `enabled` to true, and start the relay.',
            ]
            if self.setup_url:
                lines.append(f"Step by step: {self.setup_url}")
            lines.append("Then pick where sales are announced with `/zbay channel`.")

            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @group.command(name="channel", description="Choose the channel sales and auctions are announced in")
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(channel="Leave empty to stop announcing")
        async def channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
            if not interaction.permissions.manage_guild:
                await interaction.response.send_message("You need Manage Server for that.", ephemeral=True)
                return
            if not await ready(interaction):
                return

            hub.set_feed(interaction.guild_id, channel.id if channel else None)
            await interaction.response.send_message(
                f"Announcing in {channel.mention}." if channel else "Announcements are off.", ephemeral=True)

        @group.command(name="link", description="Link your Discord account to your character")
        async def link(interaction: discord.Interaction):
            if not await ready(interaction):
                return

            code = hub.offer_link(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(
                f"Your code is **{code}**. In game, open zBay, go to the **Bank** tab, type it into "
                f"**Discord code** and press **Link**. It's good for {LINK_MINUTES} minutes.",
                ephemeral=True)

        @group.command(name="unlink", description="Stop zBay messaging you about your character")
        async def unlink(interaction: discord.Interaction):
            if not await ready(interaction):
                return

            was = hub.unlink(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(
                f"Unlinked from **{render.clean(was)}**." if was else "You weren't linked.", ephemeral=True)

        @group.command(name="balance", description="What's in your zBay bank")
        async def balance(interaction: discord.Interaction):
            if not await ready(interaction):
                return

            # the game has to be asked, and it answers through a file. that can take a few seconds.
            await interaction.response.defer(ephemeral=True)
            reply = await hub.ask_balance(interaction.guild_id, interaction.user.id)

            if reply is None:
                text = "The game server didn't answer. It may be down, or the relay isn't running."
            elif not reply.get("ok"):
                text = "Your Discord account isn't linked to a character yet. Run `/zbay link`."
            else:
                amount = render.money(reply.get("balance"), hub.currency(interaction.guild_id))
                text = f"**{render.clean(reply.get('user'))}** has {amount} in the bank."

            await interaction.followup.send(text, ephemeral=True)

        async def show(interaction, text, kind, title):
            if not await ready(interaction):
                return

            lines, total, snapshot = hub.search(interaction.guild_id, text, kind)
            if snapshot is None:
                await interaction.response.send_message(NO_SNAPSHOT, ephemeral=True)
                return
            if not lines:
                await interaction.response.send_message("Nothing matches.", ephemeral=True)
                return

            head = f"**{title}**" + (f" · showing {len(lines)} of {total}" if total > len(lines) else "")
            await interaction.response.send_message(head + "\n" + "\n".join(lines), ephemeral=True)

        @group.command(name="search", description="Search what's for sale")
        @app_commands.describe(item="Part of an item's name")
        async def search(interaction: discord.Interaction, item: str):
            await show(interaction, item[:40], None, f"Matching “{render.clean(item, 40)}”")

        @group.command(name="listings", description="The newest fixed-price listings")
        async def listings(interaction: discord.Interaction):
            await show(interaction, None, "fixed", "Newest listings")

        @group.command(name="auctions", description="Auctions running now")
        async def auctions(interaction: discord.Interaction):
            await show(interaction, None, "auction", "Auctions")

        return group
