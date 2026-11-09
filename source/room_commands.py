"""Discord-facing room creation, management, and confirmed deletion."""
import logging
from typing import Optional

import discord
from discord.ext import commands

from room_permissions import (can_manage_room, has_manage_channels, visibility_overwrites,
                              ROOM_PERMISSIONS, ROOM_MODERATION_PERMISSIONS, apply_member_defaults)

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
            mode = "private"
        if row is None:
            raise commands.BadArgument("This channel is not a room managed by this bot.")
        if not can_manage_room(channel, member, row["owner_id"]):
            raise commands.CheckFailure("Only the room owner, room managers, or server administrators can do that.")
        return mode, row

    @commands.hybrid_group(name="room", invoke_without_command=True, fallback="help")
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def room(self, ctx):
        """Create, manage, or delete bot-managed voice rooms."""
        await ctx.send_help(ctx.command)

    @room.command(name="private", aliases=["semi"])
    @commands.guild_only()
    async def semi(self, ctx, *, name: str):
        """Create an owner-only room: g!room private Room name. One per person."""
        service = self.bot.get_cog("SemiRooms")
        channel = await service.create_room(ctx.author, getattr(ctx.channel, "category", None), name)
        await ctx.reply(f"Your room is ready: {channel.mention}. "
                        "You'll find a welcome message and setup help in its chat.",
                        allowed_mentions=discord.AllowedMentions.none())

    @room.command(name="fix")
    @commands.guild_only()
    async def fix(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        """Restore owner management; administrators can select another owner's room."""
        if channel is None:
            # Even administrators default only to their own rooms.
            private = [dict(row, room_label="Private")
                       for row in await self.bot.db.list_semi_rooms(ctx.guild.id)
                       if row["owner_id"] == ctx.author.id]
            temporary = [dict(row, room_label="Temporary") for row in
                         await self.bot.db.get_member_voice_channels(ctx.guild.id, ctx.author.id)]
            owned = private + temporary
            if not owned:
                await ctx.reply("You don't own a room in this server yet.")
                return
            if len(owned) != 1:
                choices = "\n".join(f"**{row['room_label']}**: <#{row['channel_id']}>" for row in owned)
                await ctx.reply(
                    "Which room would you like to repair?\n" + choices +
                    "\nUse `/room fix` and select the room in **channel**, "
                    "or use `g!room fix #room`.",
                    allowed_mentions=discord.AllowedMentions.none())
                return
            channel_id = owned[0]["channel_id"]
        else:
            channel_id = channel.id
        service = self.bot.get_cog("SemiRooms")
        async with service.lock(("channel", channel_id)):
            row = await self.bot.db.get_semi_room(ctx.guild.id, channel_id)
            if row is None:
                row = await self.bot.db.get_voice_channel(ctx.guild.id, channel_id)
            is_admin = ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator
            if row is None or (row["owner_id"] != ctx.author.id and not is_admin):
                await ctx.reply("Only the recorded owner or a server administrator can repair this room's permissions.")
                return
            try:
                current = await ctx.guild.fetch_channel(channel_id)
            except discord.NotFound:
                await ctx.reply("That room has been deleted, so there are no permissions to repair.")
                return
            owner = ctx.author
            if row["owner_id"] != ctx.author.id:
                try:
                    owner = await ctx.guild.fetch_member(row["owner_id"])
                except discord.NotFound:
                    await ctx.reply("The recorded owner has left this server. No permissions were changed.")
                    return
            if has_manage_channels(current, owner):
                await ctx.reply("You already have Manage Channel permission for that room. Nothing changed."
                                if owner.id == ctx.author.id else
                                "The owner already has Manage Channel permission. Nothing changed.")
                return
            overwrite = current.overwrites_for(owner)
            overwrite.manage_channels = True
            await current.set_permissions(owner, overwrite=overwrite,
                                          reason=f"Owner management repaired by {ctx.author.id}")
        log.info("Owner management restored guild_id=%s channel_id=%s owner_id=%s",
                 ctx.guild.id, channel_id, owner.id)
        await ctx.reply(f"The owner's Manage Channel permission for <#{channel_id}> is restored.",
                        allowed_mentions=discord.AllowedMentions.none())

    @room.command(name="ring")
    @commands.guild_only()
    async def ring(self, ctx, channel: discord.VoiceChannel):
        """From the waiting lobby, ask to join an occupied private room."""
        try:
            await self.bot.get_cog("RoomRinging").ring(ctx, channel)
        except commands.CommandError as error:
            await ctx.reply(str(error), ephemeral=True, delete_after=60,
                            allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            await ctx.reply("The room, lobby or member no longer exists. Please choose an available private room.",
                            ephemeral=True, delete_after=60)
        except discord.HTTPException:
            log.exception("Doorbell request failed guild_id=%s channel_id=%s", ctx.guild.id, channel.id)
            await ctx.reply("Discord couldn't deliver the ring. Check the bot's channel permissions and try again.",
                            ephemeral=True, delete_after=60)

    async def parse_members(self, ctx, text):
        tokens = text.replace(",", " ").split()
        if not tokens or len(tokens) > 25:
            raise commands.BadArgument("Mention between 1 and 25 members or roles, separated by spaces or commas.")
        members = {}
        for token in tokens:
            if token.startswith("<@&"):
                member = await commands.RoleConverter().convert(ctx, token)
            else:
                try:
                    member = await commands.MemberConverter().convert(ctx, token)
                except commands.MemberNotFound:
                    member = await commands.RoleConverter().convert(ctx, token)
            members[member.id] = member
        return list(members.values())

    async def update_members(self, ctx, channel, members, *, management=False, enabled, opener=False, remove_entry=False):
        service = self.bot.get_cog("SemiRooms")
        changed, skipped_managers, skipped_others = [], [], []
        async with service.lock(("channel", channel.id)):
            current = await ctx.guild.fetch_channel(channel.id)
            mode, row = await self.managed_room(current, ctx.author)
            if not enabled and not opener and any(member.id == row["owner_id"] for member in members):
                raise commands.BadArgument("The room owner's access and management cannot be revoked with this command.")
            previous_openers = await self.bot.db.list_room_openers(ctx.guild.id, channel.id)
            openers = set(previous_openers)
            overwrites = current.overwrites
            for member in members:
                is_manager = can_manage_room(current, member, row["owner_id"], overwrites=overwrites)
                if opener and not enabled:
                    if is_manager:
                        skipped_managers.append(member)
                        continue
                    if member.id not in openers:
                        skipped_others.append(member)
                        continue
                if management and not enabled and not is_manager:
                    skipped_others.append(member)
                    continue
                if remove_entry:
                    if member not in overwrites and member.id not in openers:
                        skipped_others.append(member)
                        continue
                    overwrites.pop(member, None)
                    openers.discard(member.id)
                    changed.append(member)
                    continue
                overwrite = overwrites.setdefault(member, discord.PermissionOverwrite())
                if enabled and not management:
                    apply_member_defaults(overwrite)
                if management:
                    if enabled:
                        overwrite.update(**ROOM_PERMISSIONS)
                    else:
                        overwrite.update(**dict.fromkeys(ROOM_MODERATION_PERMISSIONS, False))
                    overwrite.connect = True
                    overwrite.view_channel = True
                    openers.add(member.id)  # Demoting a manager leaves explicit opener access.
                    if not enabled:
                        overwrite.manage_permissions = False
                elif opener:
                    overwrite.connect = True
                    overwrite.view_channel = True
                    if enabled:
                        openers.add(member.id)
                    else:
                        openers.discard(member.id)
                else:
                    overwrite.connect = enabled
                    if enabled:
                        overwrite.view_channel = True
                    else:
                        overwrite.update(**dict.fromkeys(ROOM_MODERATION_PERMISSIONS, False))
                        openers.discard(member.id)
                changed.append(member)
            if changed:
                if mode == "private":
                    overwrites = visibility_overwrites(current, row["owner_id"], bool(row["hidden"]),
                                                       source_overwrites=overwrites, opener_ids=openers)
                # Keep persistence and the API edit consistent if Discord rejects it.
                # Reconciliation uses the same lock and cannot observe this intermediate state.
                await self.bot.db.replace_room_openers(ctx.guild.id, channel.id, openers)
                try:
                    await current.edit(overwrites=overwrites, reason=f"Room member permissions updated by {ctx.author.id}")
                except Exception:
                    await self.bot.db.replace_room_openers(ctx.guild.id, channel.id, previous_openers)
                    raise
        action = "Management" if management else "Opener access" if opener else "Join access" if enabled else "All room access"
        text = f"{action} {'granted' if enabled else 'revoked'} for {len(changed)} member(s)/role(s)."
        if remove_entry:
            text = f"Removed permission entries and stored opener access for {len(changed)} member(s)/role(s). Other grants still apply."
        elif not management and not opener and not enabled:
            text = f"Explicitly denied joining and management for {len(changed)} member(s)/role(s). Role denies do not override other role or personal allows."
        if management and not enabled and changed:
            text += " They remain openers and can still open and join the room."
        elif opener and not enabled and changed:
            text += " They remain guests and can join while the room is visible."
        if skipped_managers:
            names = ", ".join(f"<@&{member.id}>" if isinstance(member, discord.Role) else f"<@{member.id}>" for member in skipped_managers)
            text += f"\nSkipped {names}: they are room managers, which includes opener access. Remove their manager permission first, then remove opener access."
        if skipped_others:
            text += "\nAlready without that permission: " + ", ".join(f"<@&{member.id}>" if isinstance(member, discord.Role) else f"<@{member.id}>" for member in skipped_others) + "."
        await ctx.reply(text, allowed_mentions=discord.AllowedMentions.none())

    @room.group(name="manager", invoke_without_command=True)
    @commands.guild_only()
    async def manager(self, ctx):
        """Add or remove room managers."""
        await ctx.send_help(ctx.command)

    @manager.command(name="add")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def manager_add(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Grant listed members or roles room management and join access."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), management=True, enabled=True)

    @manager.command(name="remove")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def manager_remove(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Remove management, leaving listed members or roles as openers."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), management=True, enabled=False)

    @room.group(name="opener", invoke_without_command=True)
    @commands.guild_only()
    async def opener(self, ctx):
        """Manage who can open hidden rooms without managing them."""
        await ctx.send_help(ctx.command)

    @opener.command(name="add")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def opener_add(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Grant opening and joining without granting management."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), opener=True, enabled=True)

    @opener.command(name="remove")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def opener_remove(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Downgrade openers to guests; managers must be demoted first."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), opener=True, enabled=False)

    @room.group(name="user", invoke_without_command=True)
    @commands.guild_only()
    async def user(self, ctx):
        """Add or remove room join access."""
        await ctx.send_help(ctx.command)

    @user.command(name="add")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def user_add(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Let listed members or roles join without granting room management."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), management=False, enabled=True)

    @user.command(name="remove")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def user_remove(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Delete permission entries and stored opener access; other grants still apply."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), enabled=False, remove_entry=True)

    @user.command(name="deny")
    @commands.guild_only()
    @discord.app_commands.describe(members="Members or roles: mentions separated by spaces or commas")
    async def user_deny(self, ctx, channel: discord.VoiceChannel, *, members: str):
        """Explicitly deny joining and management and remove stored opener access."""
        await self.update_members(ctx, channel, await self.parse_members(ctx, members), enabled=False)

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
