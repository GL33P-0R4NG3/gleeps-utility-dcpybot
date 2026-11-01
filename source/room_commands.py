"""Discord-facing room creation, management, and confirmed deletion."""
import logging
from typing import Optional

import discord
from discord.ext import commands

from room_permissions import can_manage_room, has_manage_channels, visibility_overwrites

log = logging.getLogger(__name__)


class DeleteRoomView(discord.ui.View):
    def __init__(self, cog, actor_id, guild_id, channel_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.actor_id = actor_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message = None
        self.used = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another member.", ephemeral=True)
            return False
        return True

    async def finish(self, text):
        for child in self.children:
            child.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(content=text, view=self)
            except discord.HTTPException:
                pass  # The confirmation itself may have lived in the deleted room.

    @discord.ui.button(label="Continue â€” delete room", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self.used:
            await interaction.response.send_message("This confirmation has already been handled.", ephemeral=True)
            return
        self.used = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            guild = self.cog.bot.get_guild(self.guild_id)
            if guild is None:
                raise commands.BadArgument("The server is currently unavailable; the room was kept.")
            # Check current membership and permissions again, not those at prompt time.
            member = await guild.fetch_member(interaction.user.id)
            channel = guild.get_channel(self.channel_id)
            if channel is None:
                channel = await guild.fetch_channel(self.channel_id)
            mode, row = await self.cog.managed_room(channel, member)
            await channel.delete(reason=f"Room deletion confirmed by {member.id}")
            if mode == "temporary":
                await self.cog.bot.db.delete_voice_channel(guild.id, row["lobby_id"], channel.id)
                await self.cog.bot.db.delete_settings_user(guild.id, row["lobby_id"], row["owner_id"])
            else:
                await self.cog.bot.db.delete_semi_room(guild.id, channel.id)
            text = "Room deleted. Its access role, if any, was kept."
            log.info("Room deletion confirmed guild_id=%s channel_id=%s actor_id=%s", guild.id, channel.id, member.id)
        except commands.CommandError as error:
            text = str(error)
        except discord.NotFound:
            text = "The room or member no longer exists. Cleanup will reconcile any remaining records."
        except discord.Forbidden:
            text = "Discord denied the operation. Check the bot's permissions; no successful deletion was confirmed."
        except Exception:
            log.exception("Confirmed room deletion failed channel_id=%s", self.channel_id)
            text = "Deletion could not be completed cleanly. The error has been logged."
        await self.finish(text)
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.used:
            await interaction.response.send_message("This confirmation has already been handled.", ephemeral=True)
            return
        self.used = True
        await interaction.response.send_message("Cancelled. The room was kept.", ephemeral=True)
        await self.finish("Deletion cancelled. The room was kept.")

    async def on_timeout(self):
        if not self.used:
            self.used = True
            await self.finish("Confirmation expired. The room was kept.")


class RoomCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def managed_room(self, channel, member):
        row = await self.bot.db.get_voice_channel(channel.guild.id, channel.id)
        mode = "temporary"
        if row is None:
            row = await self.bot.db.get_semi_room(channel.guild.id, channel.id)
            mode = "semi-temporary"
        if row is None:
            raise commands.BadArgument("This channel is not a room managed by this bot.")
        if not can_manage_room(channel, member, row["owner_id"]):
            raise commands.CheckFailure("Only the room owner or a member with room Manage Channels can do that.")
        return mode, row

    @commands.hybrid_group(name="room", invoke_without_command=True, fallback="help")
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def room(self, ctx):
        """Create, manage, or delete bot-managed voice rooms."""
        await ctx.send_help(ctx.command)

    @room.command(name="semi")
    @commands.guild_only()
    async def semi(self, ctx, role: Optional[discord.Role] = None, *, name: str):
        """Create a private room: g!room semi [@AccessRole] Room name. One per person."""
        service = self.bot.get_cog("SemiRooms")
        channel, role = await service.create_room(ctx.author, getattr(ctx.channel, "category", None), name, role)
        await ctx.reply(f"Your room is ready: {channel.mention}. "
                        "You'll find a welcome message and setup help in its chat.",
                        allowed_mentions=discord.AllowedMentions.none())

    @room.command(name="fix")
    @commands.guild_only()
    async def fix(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        """Restore your room's Manage Channel permission: g!room fix [channel]."""
        if channel is None:
            # Works from any server text channel, even when the owner cannot see
            # their room. Prefer their persistent room over their temporary one.
            owned = [row for row in await self.bot.db.list_semi_rooms(ctx.guild.id)
                     if row["owner_id"] == ctx.author.id]
            if not owned:
                owned = await self.bot.db.get_member_voice_channels(ctx.guild.id, ctx.author.id)
            if not owned:
                await ctx.reply("You don't own a room in this server yet.")
                return
            if len(owned) != 1:
                await ctx.reply("Please specify which room: `g!room fix <channel>`.")
                return
            channel_id = owned[0]["channel_id"]
        else:
            channel_id = channel.id
        service = self.bot.get_cog("SemiRooms")
        async with service.lock(("channel", channel_id)):
            row = await self.bot.db.get_semi_room(ctx.guild.id, channel_id)
            if row is None:
                row = await self.bot.db.get_voice_channel(ctx.guild.id, channel_id)
            if row is None or row["owner_id"] != ctx.author.id:
                await ctx.reply("Only the recorded owner can repair this room's permissions.")
                return
            try:
                current = await ctx.guild.fetch_channel(channel_id)
            except discord.NotFound:
                await ctx.reply("That room has been deleted, so there are no permissions to repair.")
                return
            if has_manage_channels(current, ctx.author):
                await ctx.reply("You already have Manage Channel permission for that room. Nothing changed.")
                return
            overwrite = current.overwrites_for(ctx.author)
            overwrite.manage_channels = True
            await current.set_permissions(ctx.author, overwrite=overwrite,
                                          reason=f"Room owner {ctx.author.id} restored their management permission")
        log.info("Owner management restored guild_id=%s channel_id=%s owner_id=%s",
                 ctx.guild.id, channel_id, ctx.author.id)
        await ctx.reply(f"Your Manage Channel permission for <#{channel_id}> is restored.",
                        allowed_mentions=discord.AllowedMentions.none())

    @room.command(name="manager")
    @commands.guild_only()
    async def manager(self, ctx, channel: discord.VoiceChannel, member: discord.Member, enabled: bool = True):
        """Grant room management and join permission; append false to revoke management."""
        mode, row = await self.managed_room(channel, ctx.author)

        async def apply(current, current_row):
            overwrites = current.overwrites
            overwrite = overwrites.setdefault(member, discord.PermissionOverwrite())
            overwrite.manage_channels = enabled
            if enabled:
                overwrite.connect = True
                overwrite.view_channel = True
            if mode == "semi-temporary" and current_row["hidden"]:
                overwrites = visibility_overwrites(current, current_row["owner_id"], True,
                                                   source_overwrites=overwrites)
            # Apply management and hidden-state visibility together. set_permissions
            # followed by a cached channel read could overwrite the new grant.
            await current.edit(overwrites=overwrites, reason=f"Room management updated by {ctx.author.id}")

        if mode == "semi-temporary":
            service = self.bot.get_cog("SemiRooms")
            async with service.lock(("channel", channel.id)):
                current = await ctx.guild.fetch_channel(channel.id)
                _, row = await self.managed_room(current, ctx.author)
                await apply(current, row)
        else:
            await apply(channel, row)
        await ctx.reply(f"Room management {'granted' if enabled else 'revoked'} for {member.mention}. "
                        + ("They can see and reopen the room." if enabled else "Existing join permission is unchanged."),
                        allowed_mentions=discord.AllowedMentions.none())

    @room.command(name="delete")
    @commands.guild_only()
    async def delete(self, ctx, channel: discord.VoiceChannel):
        """Ask for confirmation before permanently deleting a room and its saved preset."""
        mode, _ = await self.managed_room(channel, ctx.author)
        view = DeleteRoomView(self, ctx.author.id, ctx.guild.id, channel.id)
        view.message = await ctx.reply(
            f"Delete **{channel.name}** ({mode})? This permanently removes the channel, "
            "its chat history and room settings. A temporary room's saved preset is also cleared. "
            "Access roles are kept. Click Continue within 60 seconds; Cancel or no response keeps the room. "
            "Discord's native Delete Channel action cannot be intercepted by the bot.",
            view=view, allowed_mentions=discord.AllowedMentions.none(),
        )
