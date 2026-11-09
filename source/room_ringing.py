"""Doorbell requests from a public waiting room to occupied private rooms."""
import asyncio
import logging
import time
from types import SimpleNamespace

import discord
from discord.ext import commands, tasks

from room_permissions import can_manage_room

log = logging.getLogger(__name__)
REQUEST_TIMEOUT = 60
LOBBY_PERMISSIONS = dict(view_channel=True, connect=True, send_messages=True,
                         read_message_history=True, speak=True, stream=True,
                         use_voice_activation=True, use_application_commands=True)
LOBBY_HELP = (
    "**🛎️ Ring a private room**\nJoin this lobby, then use `/room ring` and choose an occupied "
    "private room. Its owner or managers can let you in with one click. Stay here while you wait. "
    "You can chat, talk or stream in the meantime. Unanswered requests are ignored after 1 minute. Wait 5 minutes after an ignored request before ringing that room again."
)


async def private_reply(interaction, text: str, *, view=None):
    message = await interaction.followup.send(text, ephemeral=True, view=view, wait=True,
                                             allowed_mentions=discord.AllowedMentions.none())
    await message.delete(delay=REQUEST_TIMEOUT)
    return message


class DenyConfirmation(discord.ui.View):
    def __init__(self, request, actor_id: int):
        super().__init__(timeout=60)
        self.request = request
        self.actor_id = actor_id
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another manager.",
                                                    ephemeral=True, delete_after=60)
            return False
        return True

    @discord.ui.button(label="Confirm deny", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await self.request.decide(interaction, allow=False, block=True)
        if self.request.done:
            self.stop()
            if self.message is not None:
                await self.message.edit(content="Request handled.", view=None)
                await self.message.delete(delay=10)

    @discord.ui.button(label="Go back", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Use Allow, Ignore or Deny on the original request.", view=None)
        if self.message is not None:
            await self.message.delete(delay=10)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


class IgnoreReasonModal(discord.ui.Modal, title="Ignore room request"):
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=500,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, request):
        super().__init__(timeout=REQUEST_TIMEOUT)
        self.request = request

    async def on_submit(self, interaction):
        await self.request.decide(interaction, allow=False, reason=str(self.reason).strip())


class RingRequest(discord.ui.View):
    def __init__(self, service, ctx, room_id: int, lobby_id: int):
        super().__init__(timeout=REQUEST_TIMEOUT)
        self.service = service
        self.ctx = ctx
        self.room_id = room_id
        self.lobby_id = lobby_id
        self.requester_id = ctx.author.id
        self.deadline = time.monotonic() + REQUEST_TIMEOUT
        self.message = None
        self.done = False
        self.lock = asyncio.Lock()
        self.expiry_task: asyncio.Task | None = None

    async def expire_at_deadline(self):
        # View timeouts can reset on clicks; the doorbell deadline must not.
        await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        await self.on_timeout()

    async def manager(self, interaction):
        if interaction.guild_id != self.ctx.guild.id:
            raise commands.CheckFailure("This request belongs to another server.")
        channel = await self.ctx.guild.fetch_channel(self.room_id)
        row = await self.service.bot.db.get_semi_room(self.ctx.guild.id, self.room_id)
        member = await self.ctx.guild.fetch_member(interaction.user.id)
        if row is None or not can_manage_room(channel, member, row["owner_id"]):
            raise commands.CheckFailure("Only this room's owner or managers can respond.")
        return channel, member

    async def notify(self, text: str):
        try:
            if self.ctx.interaction is not None:
                await private_reply(self.ctx.interaction, text)
            else:
                await self.ctx.author.send(text, delete_after=REQUEST_TIMEOUT,
                                           allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.info("Ring result could not be delivered requester_id=%s", self.requester_id)

    async def finish(self, text: str, *, ignored: bool = False):
        if ignored:
            event = SimpleNamespace(guild=self.ctx.guild, author=self.ctx.author, room_id=self.room_id)
            bucket = self.service.cooldowns.get_bucket(event)
            bucket.reset()
            bucket.update_rate_limit()
        self.done = True
        self.stop()
        if self.expiry_task is not None and self.expiry_task is not asyncio.current_task():
            self.expiry_task.cancel()
        self.service.pending.pop((self.ctx.guild.id, self.requester_id, self.room_id), None)
        for button in self.children:
            button.disabled = True
        if self.message is not None:
            try:
                await self.service.bot.db.schedule_ring_cleanup(
                    self.ctx.guild.id, self.room_id, self.message.id, int(time.time()) + 10)
            except Exception:
                log.exception("Could not reschedule ring cleanup message_id=%s", self.message.id)
            try:
                await self.message.edit(content=text, view=self,
                                        allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.info("Ring card could not be updated room_id=%s", self.room_id)
            await self.message.delete(delay=10)

    async def decide(self, interaction, *, allow: bool, reason: str = "", block: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            if self.done or time.monotonic() >= self.deadline:
                if not self.done:
                    await self.finish("Request expired. Automatically ignored after 1 minute.", ignored=True)
                await private_reply(interaction, "This request has expired or was already handled.")
                return
            try:
                channel, manager = await self.manager(interaction)
            except (commands.CommandError, discord.NotFound) as error:
                await private_reply(interaction, str(error) if isinstance(error, commands.CommandError)
                                                else "The room or member no longer exists.")
                return
            except discord.HTTPException:
                await private_reply(interaction, "I couldn't verify your room permissions. Please try again.")
                return
            if block:
                try:
                    async with self.service.room_lock(self.room_id):
                        channel, manager = await self.manager(interaction)
                        row = await self.service.bot.db.get_semi_room(self.ctx.guild.id, self.room_id)
                        if row is None or row["owner_id"] == self.requester_id:
                            raise commands.CheckFailure("The room owner cannot be blocked.")
                        requester = await self.ctx.guild.fetch_member(self.requester_id)
                        overwrite = channel.overwrites_for(requester)
                        overwrite.connect = False
                        await channel.set_permissions(requester, overwrite=overwrite,
                                                      reason=f"Doorbell denied by {manager.id}")
                except (commands.CommandError, discord.HTTPException) as error:
                    text = str(error) if isinstance(error, commands.CommandError) else "I couldn't set the Connect deny. Check my Manage Permissions access and try again."
                    await private_reply(interaction, text)
                    return
                await self.finish(f"Request denied by {manager.mention}. Joining and ringing this room are now blocked.")
                await self.notify("Your request was denied. You cannot join or ring this room unless its managers remove the restriction.")
                await private_reply(interaction, "Connect denied for this person in this room. Future rings here are blocked.")
                return
            if not allow:
                text = "Your room request was ignored. Wait 5 minutes before ringing this room again." + (f" Reason: {reason}" if reason else "")
                await self.finish(f"Request ignored by {manager.mention}.", ignored=True)
                await self.notify(text)
                await private_reply(interaction, "Request ignored.")
                return
            try:
                async with self.service.room_lock(self.room_id):
                    requester = await self.ctx.guild.fetch_member(self.requester_id)
                    channel = await self.service.validate(requester, self.room_id, expected_lobby=self.lobby_id)
                    # Recheck authorization under the same lock used by permission commands.
                    channel, manager = await self.manager(interaction)
                    await self.service.admit(requester, channel, manager)
            except (commands.CommandError, discord.NotFound) as error:
                text = str(error) if isinstance(error, commands.CommandError) else "The room or requester no longer exists."
                await self.finish("Request cancelled: " + text)
                await self.notify(text)
                await private_reply(interaction, text)
                return
            except discord.HTTPException:
                log.exception("Room admission failed room_id=%s requester_id=%s", self.room_id, self.requester_id)
                await private_reply(interaction, "Discord refused the move. Check my Move Members and channel access permissions, then retry.")
                return
            await self.finish(f"Request accepted by {manager.mention}.")
            await self.notify(f"Your request was accepted. You were moved to {channel.mention}.")
            await private_reply(interaction, "Accepted and moved into the room.")

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow(self, interaction, button):
        await self.decide(interaction, allow=True)

    @discord.ui.button(label="Ignore", style=discord.ButtonStyle.secondary)
    async def ignore(self, interaction, button):
        await self.decide(interaction, allow=False)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.manager(interaction)
        except (commands.CommandError, discord.HTTPException):
            await private_reply(interaction, "Only this room's owner or managers can deny this request.")
            return
        if self.done or time.monotonic() >= self.deadline:
            await private_reply(interaction, "This request has expired or was already handled.")
            return
        view = DenyConfirmation(self, interaction.user.id)
        view.message = await private_reply(
            interaction, "Deny this person access to this room? This sets an explicit Connect deny and blocks future rings here. "
            "Go back to keep the request active.", view=view)

    @discord.ui.button(label="Ignore with reason", style=discord.ButtonStyle.secondary)
    async def ignore_reason(self, interaction, button):
        # Opening a modal must be the first response, so authorization is repeated
        # on submission rather than delaying this interaction with network calls.
        if self.done or time.monotonic() >= self.deadline:
            await interaction.response.send_message("This request has expired or was already handled.", ephemeral=True, delete_after=60)
            return
        await interaction.response.send_modal(IgnoreReasonModal(self))

    async def on_timeout(self):
        async with self.lock:
            if not self.done:
                await self.finish("Request expired. Automatically ignored after 1 minute.", ignored=True)
                await self.notify("Your room request was automatically ignored. Wait 5 minutes before ringing this room again.")


class RoomRinging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending: dict[tuple[int, int, int], RingRequest] = {}
        self.cooldowns = commands.CooldownMapping.from_cooldown(1, 300, lambda event: (event.guild.id, event.author.id, event.room_id))
        self.prompts = commands.CooldownMapping.from_cooldown(1, 60, commands.BucketType.member)

    def room_lock(self, room_id: int):
        return self.bot.get_cog("SemiRooms").lock(("channel", room_id))

    async def cog_load(self):
        self.cleanup.start()

    async def cog_unload(self):
        self.cleanup.cancel()
        for request in list(self.pending.values()):
            async with request.lock:
                await request.finish("Request cancelled because the bot is restarting. Please ring again.")

    @tasks.loop(seconds=30)
    async def cleanup(self):
        try:
            await self.cleanup_messages()
        except Exception:
            log.exception("Ring message cleanup failed; will retry")

    @cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    async def cleanup_messages(self):
        for guild_id, channel_id, message_id in await self.bot.db.due_ring_messages(int(time.time())):
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.unavailable:
                continue
            try:
                channel = await guild.fetch_channel(channel_id)
                await channel.get_partial_message(message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Could not delete old ring message_id=%s; will retry", message_id)
                continue
            await self.bot.db.delete_ring_cleanup(message_id)

    async def configure(self, guild, creation_channel, lobbychannel=None):
        """Reuse a recorded waiting room; create one only when none exists."""
        async with self.bot.get_cog("SemiRooms").lock(("ring-config", guild.id)):
            rows = await self.bot.db.list_lobbies(guild.id)
            creation_ids = {row["lobby_id"] for row in rows} | {creation_channel.id}
            if lobbychannel is None:
                current = next((row for row in rows if row["lobby_id"] == creation_channel.id), None)
                candidates = [current] if current else []
                candidates += [row for row in rows if row is not current]
                for row in candidates:
                    lobby_id = row["settings_json"].get("RingLobbyID")
                    if lobby_id is None:
                        continue
                    try:
                        existing = await guild.fetch_channel(lobby_id)
                    except discord.NotFound:
                        continue
                    if row is current or existing.category_id == creation_channel.category_id:
                        lobbychannel = existing
                        break
            created = False
            if lobbychannel is not None:
                if lobbychannel.guild.id != guild.id or not isinstance(lobbychannel, discord.VoiceChannel):
                    raise commands.BadArgument("Choose a voice lobby in this server.")
                if lobbychannel.id in creation_ids:
                    raise commands.BadArgument("The waiting lobby must be separate from room-creation channels.")
                if (await self.bot.db.get_semi_room(guild.id, lobbychannel.id)
                        or await self.bot.db.get_voice_channel(guild.id, lobbychannel.id)):
                    raise commands.BadArgument("A managed room cannot also be the waiting lobby.")
                lobbychannel = await guild.fetch_channel(lobbychannel.id)
                overwrites = lobbychannel.overwrites
                for overwrite in overwrites.values():
                    for name in LOBBY_PERMISSIONS:
                        if getattr(overwrite, name) is False:
                            setattr(overwrite, name, None)
                overwrites.setdefault(guild.default_role, discord.PermissionOverwrite()).update(**LOBBY_PERMISSIONS)
                await lobbychannel.edit(overwrites=overwrites, reason="Open the doorbell waiting lobby to everyone")
            else:
                lobbychannel = await guild.create_voice_channel(
                    "🛎️ Lobby", category=creation_channel.category,
                    overwrites={guild.default_role: discord.PermissionOverwrite(**LOBBY_PERMISSIONS)},
                    reason="Private-room doorbell waiting lobby")
                created = True
            try:
                await self.bot.db.update_lobby_setting(guild.id, creation_channel.id, "RingLobbyID", lobbychannel.id)
            except Exception:
                if created:
                    await lobbychannel.delete(reason="Roll back unrecorded waiting lobby")
                raise
            try:
                message = await lobbychannel.send(LOBBY_HELP, delete_after=REQUEST_TIMEOUT, allowed_mentions=discord.AllowedMentions.none())
                await self.bot.db.schedule_ring_cleanup(guild.id, lobbychannel.id, message.id, int(time.time()) + REQUEST_TIMEOUT)
            except discord.HTTPException:
                log.warning("Could not send waiting-lobby help channel_id=%s", lobbychannel.id)
            return lobbychannel

    async def validate_configuration(self, guild, creation_channel, lobbychannel=None):
        """Reject conflicting channel roles before setlobby writes configuration."""
        if creation_channel.guild.id != guild.id:
            raise commands.BadArgument("Choose a creation channel in this server.")
        if lobbychannel is None:
            return
        if lobbychannel.guild.id != guild.id or not isinstance(lobbychannel, discord.VoiceChannel):
            raise commands.BadArgument("Choose a voice lobby in this server.")
        if lobbychannel.id == creation_channel.id or await self.bot.db.get_lobby(guild.id, lobbychannel.id):
            raise commands.BadArgument("The waiting lobby must be separate from room-creation channels.")
        if (await self.bot.db.get_semi_room(guild.id, lobbychannel.id)
                or await self.bot.db.get_voice_channel(guild.id, lobbychannel.id)):
            raise commands.BadArgument("A managed room cannot also be the waiting lobby.")

    async def validate(self, member, room_id: int, *, expected_lobby: int | None = None):
        guild = member.guild
        rows = await self.bot.db.list_lobbies(guild.id)
        private_lobbies = [row for row in rows if row["settings_json"]["RoomMode"] == "semi"]
        lobby_ids = {row["settings_json"].get("RingLobbyID") for row in private_lobbies}
        lobby_ids.discard(None)
        voice = member.voice
        if voice is None or voice.channel is None or voice.channel.id not in lobby_ids:
            raise commands.BadArgument("Join the doorbell lobby before using `/room ring`.")
        if expected_lobby is not None and voice.channel.id != expected_lobby:
            raise commands.BadArgument("You left the waiting lobby. Ring again from the lobby when ready.")
        if room_id in lobby_ids or any(row["lobby_id"] == room_id for row in rows):
            raise commands.BadArgument("You cannot ring a lobby or a room-creation channel.")
        row = await self.bot.db.get_semi_room(guild.id, room_id)
        if row is None:
            raise commands.BadArgument("Choose a private room managed by this bot.")
        channel = await guild.fetch_channel(room_id)
        if not isinstance(channel, discord.VoiceChannel) or channel.guild.id != guild.id:
            raise commands.BadArgument("Choose a private voice room in this server.")
        paired = [item for item in private_lobbies if item["settings_json"].get("RingLobbyID") == voice.channel.id]
        in_section = False
        for item in paired:
            try:
                creation = await guild.fetch_channel(item["lobby_id"])
            except discord.NotFound:
                continue
            if creation.category_id == channel.category_id:
                in_section = True
                break
        if not in_section:
            raise commands.BadArgument("That room is outside this lobby's private-room section.")
        personal = channel.overwrites_for(member)
        if personal.connect is False or personal.view_channel is False:
            raise commands.CheckFailure("This room explicitly denies your access. Ringing cannot bypass that restriction.")
        # Also respect specific role bans; the default @everyone deny is the doorbell's purpose.
        for role in member.roles:
            if role.id == guild.default_role.id:
                continue
            overwrite = channel.overwrites_for(role)
            if overwrite.connect is False or overwrite.view_channel is False:
                raise commands.CheckFailure("One of your roles is explicitly restricted in this room.")
        if row["hidden"] or not any(not occupant.bot for occupant in channel.members):
            raise commands.BadArgument("That private room is inactive or empty. Try when someone is there.")
        if member.is_timed_out():
            raise commands.CheckFailure("You cannot ring rooms while timed out.")
        if channel.permissions_for(member).connect:
            raise commands.BadArgument("You already have access to that room; you can join it directly.")
        if channel.user_limit and len(channel.members) >= channel.user_limit:
            raise commands.BadArgument("That room is full. Try again when a place is available.")
        bot_permissions = channel.permissions_for(guild.me)
        lobby_permissions = voice.channel.permissions_for(guild.me)
        if not (bot_permissions.view_channel and bot_permissions.connect and bot_permissions.send_messages
                and bot_permissions.embed_links and bot_permissions.move_members and lobby_permissions.move_members):
            raise commands.BadArgument("I need channel access, Send Messages, Embed Links and Move Members to handle this request.")
        return channel

    async def admit(self, requester, channel, manager):
        # A moderator-approved move admits this visit without granting lasting access.
        await requester.move_to(channel, reason=f"Doorbell accepted by {manager.id}")

    async def ring(self, ctx, channel):
        if channel.guild.id != ctx.guild.id:
            raise commands.BadArgument("Choose a room in this server.")
        key = (ctx.guild.id, ctx.author.id, channel.id)
        async with self.bot.get_cog("SemiRooms").lock(("ring", *key)):
            previous = self.pending.get(key)
            if previous is not None and time.monotonic() >= previous.deadline:
                await previous.on_timeout()
            if key in self.pending:
                raise commands.BadArgument("You already have a pending ring for this room. Wait for a response or its 1-minute expiry.")
            member = await ctx.guild.fetch_member(ctx.author.id)
            channel = await self.validate(member, channel.id)
            if sum(request.room_id == channel.id for request in self.pending.values()) >= 5:
                raise commands.BadArgument("That room already has several requests waiting. Please try later.")
            event = SimpleNamespace(guild=ctx.guild, author=ctx.author, room_id=channel.id)
            retry = self.cooldowns.get_bucket(event).update_rate_limit()
            if retry:
                raise commands.BadArgument(f"Please wait {int(retry) + 1} seconds before ringing again.")
            request = RingRequest(self, ctx, channel.id, member.voice.channel.id)
            embed = discord.Embed(title="🛎️ Someone is at the door!", colour=discord.Colour.blurple(),
                                  description=f"{member.mention} would like to join from {member.voice.channel.mention}.\n"
                                  "Owner or managers: **Allow** to move them in, or **Ignore**. "
                                  "**Deny** blocks future rings after confirmation. A reason is optional via **Ignore with reason**.")
            embed.set_footer(text="Expires in 1 minute. Approval is for this visit only. Requests do not survive a bot restart.")
            self.pending[key] = request
            try:
                row = await self.bot.db.get_semi_room(ctx.guild.id, channel.id)
                if row is None:
                    raise commands.BadArgument("That private room was removed. Please choose another room.")
                recipients = {row["owner_id"]}
                recipients.update(person.id for person in channel.members
                                  if not person.bot and can_manage_room(channel, person, row["owner_id"]))
                mentions = [discord.Object(id=user_id) for user_id in sorted(recipients)[:50]]
                request.message = await channel.send(
                    " ".join(f"<@{person.id}>" for person in mentions), embed=embed, view=request,
                    allowed_mentions=discord.AllowedMentions(users=mentions, roles=False, everyone=False))
                await self.bot.db.schedule_ring_cleanup(ctx.guild.id, channel.id, request.message.id,
                                                        int(time.time()) + REQUEST_TIMEOUT + 10)
            except Exception:
                self.pending.pop(key, None)
                request.stop()
                if request.message is not None:
                    try:
                        await request.message.delete()
                    except discord.HTTPException:
                        log.warning("Failed to remove incomplete ring message_id=%s", request.message.id)
                raise
            request.expiry_task = asyncio.create_task(request.expire_at_deadline())
            await ctx.reply("Doorbell rung! Stay in the lobby while the room's owner or managers respond.",
                            ephemeral=True, delete_after=REQUEST_TIMEOUT, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        requests = [request for (guild_id, user_id, _), request in self.pending.items()
                    if guild_id == member.guild.id and user_id == member.id]
        for request in requests:
            if after.channel is None or after.channel.id != request.lobby_id:
                async with request.lock:
                    if not request.done:
                        await request.finish("Request cancelled: the requester left the waiting lobby.")
        if after.channel is None:
            return
        try:
            rows = await self.bot.db.list_lobbies(member.guild.id)
            if not any(row["settings_json"].get("RingLobbyID") == after.channel.id
                       and row["settings_json"]["RoomMode"] == "semi" for row in rows):
                return
            # CooldownMapping only needs guild and author for the member bucket.
            event = SimpleNamespace(guild=member.guild, author=member)
            if self.prompts.get_bucket(event).update_rate_limit():
                return
            await member.send(LOBBY_HELP, delete_after=REQUEST_TIMEOUT, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.info("Lobby DM unavailable member_id=%s; sending temporary lobby help", member.id)
            try:
                message = await after.channel.send(LOBBY_HELP, delete_after=REQUEST_TIMEOUT,
                                                  allowed_mentions=discord.AllowedMentions.none())
                await self.bot.db.schedule_ring_cleanup(member.guild.id, after.channel.id, message.id,
                                                        int(time.time()) + REQUEST_TIMEOUT)
            except discord.HTTPException:
                log.info("Lobby help unavailable channel_id=%s", after.channel.id)
        except Exception:
            log.exception("Doorbell lobby join failed member_id=%s", member.id)
