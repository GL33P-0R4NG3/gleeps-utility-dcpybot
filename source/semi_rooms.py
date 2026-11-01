"""Persistent private rooms that hide instead of being deleted when idle."""
import asyncio
import logging
import time
from weakref import WeakValueDictionary

import discord
from discord.ext import commands, tasks

from room_permissions import visibility_overwrites
from lobby_access import apply_lobby_gate, gate_role_id, may_create_room
from room_help import RoomHelpView, welcome_embed
from voice_channels import DELETE_DELAY, CHECK_INTERVAL, OWNER_PERMISSIONS

log = logging.getLogger(__name__)


class SemiRooms(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self._locks = WeakValueDictionary()

    def lock(self, key):
        value = self._locks.get(key)
        if value is None:
            value = asyncio.Lock()
            self._locks[key] = value
        return value

    async def cog_load(self):
        self.reconcile.start()

    async def cog_unload(self):
        self.reconcile.cancel()

    async def create_room(self, member, category, name, *, source_lobby=None):
        guild = member.guild
        if not name.strip() or len(name) > 100:
            raise commands.BadArgument("Room names must contain 1–100 characters.")
        async with self.lock(("owner", guild.id, member.id)):
            configured = [row for row in await self.db.list_lobbies(guild.id)
                          if row["settings_json"]["RoomMode"] == "semi"]
            if source_lobby is not None:
                selected = next((row for row in configured if row["lobby_id"] == source_lobby.id), None)
                if selected is not None and not may_create_room(member, selected["settings_json"]):
                    return None
            elif configured and not any(may_create_room(member, row["settings_json"]) for row in configured):
                raise commands.CheckFailure("You need a private-room lobby's creation role before creating a room.")
            for row in await self.db.list_semi_rooms(guild.id):
                if row["owner_id"] != member.id:
                    continue
                existing = guild.get_channel(row["channel_id"])
                if existing is None:
                    try:
                        existing = await guild.fetch_channel(row["channel_id"])
                    except discord.NotFound:
                        await self.db.delete_semi_room(guild.id, row["channel_id"])
                        continue
                if source_lobby is not None:
                    return None  # Forced re-entry by an existing owner is deliberately a no-op.
                raise commands.BadArgument(f"You already own a semi-temporary room: <#{row['channel_id']}>.")
            if source_lobby is not None and (member.voice is None or member.voice.channel != source_lobby):
                return None
            await self.db.get_settings_guild(guild.id)
            channel = None
            tracked = False
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                    member: discord.PermissionOverwrite(**OWNER_PERMISSIONS),
                    guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True,
                                                         manage_permissions=True, send_messages=True),
                }
                channel = await guild.create_voice_channel(name=name, category=category, overwrites=overwrites,
                                                           reason=f"Semi-temporary room requested by {member.id}")
                await self.db.set_semi_room(guild.id, channel.id, member.id)
                tracked = True
                if source_lobby is not None:
                    if member.voice is None or member.voice.channel != source_lobby:
                        raise RuntimeError("Member left the private-room lobby during creation")
                    await member.move_to(channel, reason="Moving to newly created private room")
            except Exception:
                channel_gone = channel is None
                if channel is not None:
                    try:
                        await channel.delete(reason="Rolling back incomplete private room")
                        channel_gone = True
                    except discord.NotFound:
                        channel_gone = True
                    except discord.HTTPException:
                        log.exception("Private room rollback failed channel_id=%s", channel.id)
                        if not tracked:
                            try:
                                await self.db.set_semi_room(guild.id, channel.id, member.id)
                            except Exception:
                                log.critical("Private room needs manual recovery channel_id=%s", channel.id, exc_info=True)
                if channel_gone:
                    if tracked:
                        await self.db.delete_semi_room(guild.id, channel.id)
                raise
            # Creation is complete. A visibility failure is retryable by reconciliation.
            try:
                await self.set_visibility(channel, member.id, hidden=not bool(channel.voice_states))
            except discord.HTTPException:
                log.exception("Initial hidden-room visibility needs retry channel_id=%s", channel.id)
            log.info("Private room created guild_id=%s channel_id=%s owner_id=%s", guild.id, channel.id, member.id)
            try:
                await channel.send(f"Hey {member.mention}, your room is ready!",
                                   embed=welcome_embed(), view=RoomHelpView(),
                                   allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
            except discord.HTTPException:
                log.warning("Private room welcome could not be delivered channel_id=%s", channel.id, exc_info=True)
            try:
                await self.sync_lobby_locks(guild)
            except Exception:
                log.exception("Lobby locking needs retry guild_id=%s", guild.id)
            return channel

    async def sync_lobby_locks(self, guild):
        """Persist Connect overrides so restrictions survive restarts and can be undone."""
        if guild.unavailable:
            return
        async with self.lock(("lobbies", guild.id)):
            configured = await self.db.list_lobbies(guild.id)
            for lobby in configured:
                try:
                    await apply_lobby_gate(guild, lobby["lobby_id"], lobby["settings_json"])
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception("Lobby gate enforcement failed lobby_id=%s", lobby["lobby_id"])
            settings_by_lobby = {row["lobby_id"]: row["settings_json"] for row in configured}
            lobbies = {row["lobby_id"] for row in configured
                       if row["settings_json"]["RoomMode"] == "semi"}
            owners = {row["owner_id"] for row in await self.db.list_semi_rooms(guild.id)}
            records = {(row["lobby_id"], row["user_id"]): row
                       for row in await self.db.list_lobby_locks(guild.id)}
            desired = {(lobby_id, owner_id) for lobby_id in lobbies for owner_id in owners}
            for lobby_id, user_id in desired | records.keys():
                try:
                    # Fetch fresh overwrites; cached state may predate another edit.
                    channel = await guild.fetch_channel(lobby_id)
                    target = guild.get_member(user_id)
                    if target is None:
                        try:
                            target = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            # Retain restoration data for members who may rejoin.
                            continue
                    overwrite = channel.overwrites_for(target)
                    record = records.get((lobby_id, user_id))
                    if (lobby_id, user_id) in desired:
                        if record is None:
                            await self.db.save_lobby_lock(guild.id, lobby_id, user_id, overwrite.connect)
                        if overwrite.connect is not False:
                            overwrite.connect = False
                            await channel.set_permissions(target, overwrite=overwrite, reason="Owner already has a private room")
                    else:
                        if overwrite.connect is False:
                            previous = record["previous_connect"]
                            overwrite.connect = None if previous is None else bool(previous)
                            # Do not restore an old member allow that bypasses a role gate.
                            if overwrite.connect is True and gate_role_id(settings_by_lobby.get(lobby_id, {})) is not None:
                                overwrite.connect = None
                            await channel.set_permissions(target, overwrite=None if overwrite.is_empty() else overwrite,
                                                          reason="Private-room lobby restriction released")
                        await self.db.delete_lobby_lock(guild.id, lobby_id, user_id)
                except discord.NotFound:
                    await self.db.delete_lobby_lock(guild.id, lobby_id, user_id)
                except Exception:
                    log.exception("Lobby restriction update failed lobby_id=%s user_id=%s", lobby_id, user_id)

    async def set_visibility(self, channel, owner_id, *, hidden):
        overwrites = visibility_overwrites(channel, owner_id, hidden)
        if overwrites != channel.overwrites:
            await channel.edit(overwrites=overwrites, reason="Hide idle room" if hidden else "Reveal occupied room")

    async def reconcile_room(self, row):
        async with self.lock(("channel", row["channel_id"])):
            # Reload under the lock so a queued event cannot restore an old timer.
            row = await self.db.get_semi_room(row["guild_id"], row["channel_id"])
            if row is None:
                return
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None or guild.unavailable:
                return
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                try:
                    channel = await guild.fetch_channel(row["channel_id"])
                except discord.NotFound:
                    await self.db.delete_semi_room(guild.id, row["channel_id"])
                    return
            if not isinstance(channel, discord.VoiceChannel):
                return
            if channel.voice_states:
                await self.set_visibility(channel, row["owner_id"], hidden=False)
                await self.db.update_semi_state(channel.id, hidden=False, expires_at=None)
            elif row["hidden"]:
                # Also handles changed roles/manager permissions while hidden.
                await self.set_visibility(channel, row["owner_id"], hidden=True)
            elif row["expires_at"] is None:
                await self.db.update_semi_state(channel.id, hidden=False, expires_at=int(time.time()) + DELETE_DELAY)
            elif row["expires_at"] <= time.time():
                await self.set_visibility(channel, row["owner_id"], hidden=True)
                await self.db.update_semi_state(channel.id, hidden=True, expires_at=None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        for channel in (before.channel, after.channel):
            if channel is not None:
                try:
                    row = await self.db.get_semi_room(member.guild.id, channel.id)
                    if row:
                        await self.reconcile_room(row)
                except Exception:
                    log.exception("Private room voice event failed channel_id=%s", channel.id)

        if after.channel is not None and not member.bot:
            try:
                lobby = await self.db.get_lobby(member.guild.id, after.channel.id)
                if lobby and lobby["settings_json"].get("RoomMode") == "semi":
                    if not may_create_room(member, lobby["settings_json"]):
                        return
                    await self.create_room(member, after.channel.category,
                                           f"{member.display_name}'s room"[:100],
                                           source_lobby=after.channel)
            except Exception:
                log.exception("Private lobby creation failed member_id=%s lobby_id=%s", member.id, after.channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        try:
            await self.db.delete_semi_room(channel.guild.id, channel.id)
            await self.sync_lobby_locks(channel.guild)
        except Exception:
            log.exception("Private room deletion event failed channel_id=%s", channel.id)

    async def refresh_hidden(self, guild):
        try:
            for row in await self.db.list_semi_rooms(guild.id):
                if row["hidden"]:
                    try:
                        await self.reconcile_room(row)
                    except Exception:
                        log.exception("Hidden room refresh failed channel_id=%s", row["channel_id"])
        except Exception:
            log.exception("Hidden room records unavailable guild_id=%s", guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            await self.refresh_hidden(after.guild)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if before.permissions != after.permissions:
            await self.refresh_hidden(after.guild)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if before.overwrites != after.overwrites:
            try:
                row = await self.db.get_semi_room(after.guild.id, after.id)
                if row and row["hidden"]:
                    await self.reconcile_room(row)
            except Exception:
                log.exception("Room permission refresh failed channel_id=%s", after.id)

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def reconcile(self):
        if not self.bot.is_ready():
            return
        try:
            for row in await self.db.list_semi_rooms():
                try:
                    await self.reconcile_room(row)
                except Exception:
                    log.exception("Private room reconciliation failed channel_id=%s", row["channel_id"])
            for guild in self.bot.guilds:
                await self.sync_lobby_locks(guild)
        except Exception:
            log.exception("Private room records unavailable")

    @reconcile.before_loop
    async def wait_until_ready(self):
        await self.bot.wait_until_ready()
