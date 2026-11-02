"""Temporary voice-channel lifecycle, independent of the command interface."""
import asyncio
import logging
import time
from random import randint
from weakref import WeakValueDictionary

import discord
from discord.ext import commands, tasks
from lobby_access import may_create_room
from room_permissions import ROOM_PERMISSIONS

log = logging.getLogger(__name__)
CHECK_INTERVAL = 60
DELETE_DELAY = 15 * 60
OWNER_PERMISSIONS = ROOM_PERMISSIONS


class VoiceChannels(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self._locks = WeakValueDictionary()

    async def cog_load(self):
        self.prune_expired.start()

    async def cog_unload(self):
        self.prune_expired.cancel()

    def member_lock(self, guild_id, member_id, lobby_id):
        key = (guild_id, member_id, lobby_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        # Process departures even for bots, and before potentially failing creation.
        for channel in (before.channel, after.channel):
            if channel is not None:
                try:
                    row = await self.db.get_voice_channel(member.guild.id, channel.id)
                    if row:
                        expiry = None if channel.voice_states else int(time.time()) + DELETE_DELAY
                        await self.db.update_voice_last_disconnect(member.guild.id, channel.id, expiry)
                except Exception:
                    log.exception("Voice state reconciliation failed guild_id=%s channel_id=%s", member.guild.id, channel.id)
        if after.channel is not None and not member.bot:
            try:
                async with self.member_lock(member.guild.id, member.id, 0):
                    # Ignore stale queued events after the member has moved elsewhere.
                    if member.voice is None or member.voice.channel != after.channel:
                        return
                    await self.create_room(member, after.channel)
            except Exception:
                log.exception("Room setup failed guild_id=%s member_id=%s lobby_id=%s", member.guild.id, member.id, after.channel.id)
                try:
                    await member.send("I couldn't finish setting up your voice channel. Please try again later.")
                except discord.HTTPException:
                    log.debug("Could not notify member_id=%s", member.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """Native deletions cannot be cancelled; release the owner's slot immediately."""
        try:
            row = await self.db.get_voice_channel(channel.guild.id, channel.id)
            if row:
                await self.db.delete_voice_channel(channel.guild.id, row["lobby_id"], channel.id)
                log.info("Deleted room tracking removed guild_id=%s channel_id=%s", channel.guild.id, channel.id)
        except Exception:
            log.exception("Deletion event cleanup failed; periodic reconciliation will retry channel_id=%s", channel.id)

    def build_overwrites(self, member, settings):
        # Custom rooms deliberately do not inherit category overwrites.
        overwrites = {}
        for entry in settings.get("overwrites", []):
            target = (member.guild.get_role(entry["snowflake"]) if entry["is_role"]
                      else member.guild.get_member(entry["snowflake"]))
            if target is not None:
                overwrites[target] = discord.PermissionOverwrite(**entry["flags"])
        owner = overwrites.setdefault(member, discord.PermissionOverwrite())
        owner.update(**OWNER_PERMISSIONS)
        return overwrites

    async def create_room(self, member, lobby_channel):
        guild = member.guild
        lobby = await self.db.get_lobby(guild.id, lobby_channel.id)
        if lobby is None:
            return
        settings = lobby["settings_json"]
        if settings.get("RoomMode", "temporary") != "temporary" or not may_create_room(member, settings):
            return
        # Refresh missing records before applying the server-wide one-room limit.
        existing = await self.db.get_member_voice_channels(guild.id, member.id)
        for row in existing:
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                try:
                    channel = await guild.fetch_channel(row["channel_id"])
                except discord.NotFound:
                    await self.db.delete_voice_channel(guild.id, row["lobby_id"], row["channel_id"])
                    continue
            await lobby_channel.send(
                f"{member.mention} You already own a temporary room: <#{row['channel_id']}>. "
                "The limit is one per person in this server.", delete_after=60,
            )
            return
        saved = await self.db.get_settings_user(guild.id, lobby_channel.id, member.id) or {}
        purpose = "custom" if saved else "general"
        name = saved.get("channel_name", settings["NameDefaults"]["general"])
        if "%num%" in name:
            number = await self.db.get_count_voice_channels(guild.id, lobby_channel.id)
            name = name.replace("%num%", str(number))
        name = name.replace("%randnum%", str(randint(0, 999999)).zfill(6))
        if not name.strip() or len(name) > 100:
            raise ValueError("Expanded channel name must contain 1â€“100 characters")
        channel = await guild.create_voice_channel(
            name=name, category=lobby_channel.category,
            reason=f"Temporary room requested by {member.id}",
            bitrate=int(guild.bitrate_limit), user_limit=saved.get("user_limit", 0),
            overwrites=self.build_overwrites(member, saved),
        )
        tracked = False
        try:
            await self.db.set_voice_channel(channel_id=channel.id, guild_id=guild.id,
                                           lobby_id=lobby_channel.id, owner_id=member.id,
                                           purpose=purpose, channel_name=name)
            tracked = True
            await self.db.update_voice_last_disconnect(guild.id, channel.id, int(time.time()) + DELETE_DELAY)
            if member.voice is None or member.voice.channel != lobby_channel:
                raise RuntimeError("Member left the lobby during room creation")
            await member.move_to(channel, reason="Moving to requested room")
        except Exception:
            # Retain tracking if Discord refuses rollback so cleanup can retry.
            try:
                await channel.delete(reason="Rolling back incomplete room creation")
            except discord.NotFound:
                if tracked:
                    await self.db.delete_voice_channel(guild.id, lobby_channel.id, channel.id)
            except discord.HTTPException:
                log.exception("Room rollback failed guild_id=%s channel_id=%s tracked=%s", guild.id, channel.id, tracked)
                if not tracked:
                    # A transient DB failure may have cleared during the API call.
                    try:
                        await self.db.set_voice_channel(channel_id=channel.id, guild_id=guild.id,
                                                       lobby_id=lobby_channel.id, owner_id=member.id,
                                                       purpose=purpose, channel_name=name)
                    except Exception:
                        log.critical("Untracked room requires manual cleanup guild_id=%s channel_id=%s", guild.id, channel.id, exc_info=True)
            else:
                if tracked:
                    await self.db.delete_voice_channel(guild.id, lobby_channel.id, channel.id)
            raise
        log.info("Room created guild_id=%s channel_id=%s owner_id=%s", guild.id, channel.id, member.id)
        try:
            await channel.send("Your temporary room is ready — no setup needed. It will be deleted after 15 minutes empty.")
        except discord.HTTPException:
            log.warning("Welcome message failed guild_id=%s channel_id=%s", guild.id, channel.id, exc_info=True)

    async def prune_row(self, row):
        guild_id, channel_id, lobby_id, owner_id, original_name, purpose, expiry, save_settings, _ = row
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.unavailable:
            return  # Missing cache state does not prove a guild/channel was deleted.
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                await self.db.delete_voice_channel(guild_id, lobby_id, channel_id)
                log.info("Removed missing room record guild_id=%s channel_id=%s", guild_id, channel_id)
                return
        if not isinstance(channel, discord.VoiceChannel):
            log.warning("Tracked channel is not a voice channel guild_id=%s channel_id=%s", guild_id, channel_id)
            return
        if channel.voice_states:
            if expiry is not None:
                await self.db.update_voice_last_disconnect(guild_id, channel_id)
            return
        now = int(time.time())
        if expiry is None:
            await self.db.update_voice_last_disconnect(guild_id, channel_id, now + DELETE_DELAY)
            log.info("Started empty-room expiry guild_id=%s channel_id=%s", guild_id, channel_id)
            return
        if expiry > now:
            return
        if save_settings:
            name = channel.name
            if purpose != "custom" and original_name == name:
                settings = await self.db.get_settings_lobby(guild_id, lobby_id)
                name = settings["NameDefaults"].get(purpose, name)
            saved = {
                "channel_name": name, "user_limit": channel.user_limit,
                "overwrites": [
                    {"snowflake": target.id, "is_role": isinstance(target, discord.Role),
                     "flags": {key: value for key, value in overwrite if value is not None}}
                    for target, overwrite in channel.overwrites.items()
                ],
            }
            await self.db.set_settings_user(guild_id, lobby_id, owner_id, saved)
        # A member may join during the database await above.
        if channel.voice_states:
            await self.db.update_voice_last_disconnect(guild_id, channel_id)
            return
        try:
            await channel.delete(reason="Temporary room expired after inactivity")
        except discord.NotFound:
            pass
        await self.db.delete_voice_channel(guild_id, lobby_id, channel_id)
        log.info("Room deleted guild_id=%s channel_id=%s owner_id=%s", guild_id, channel_id, owner_id)

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def prune_expired(self):
        if not self.bot.is_ready():
            return
        try:
            async for row in self.db.iterate_voice_rows():
                try:
                    await self.prune_row(row)
                except Exception:
                    log.exception("Room cleanup failed; will retry guild_id=%s channel_id=%s", row[0], row[1])
        except Exception:
            log.exception("Could not load cleanup records; will retry")

    @prune_expired.before_loop
    async def wait_until_ready(self):
        await self.bot.wait_until_ready()
