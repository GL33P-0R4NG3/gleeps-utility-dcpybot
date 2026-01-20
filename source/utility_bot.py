# utility_bot.py
import os
import re
from collections.abc import Mapping
from operator import contains
from random import randint

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set – check your .env file")

# ------------------------------------------------------------
#  Imports & global objects
# ------------------------------------------------------------
import discord
from discord.ext import commands, tasks
import time
import asyncio
from typing import Optional, Dict, Any

from data.db_helper import DBHelper          # <-- Custom database helper

# ----------------------------------------------------------------------
#  Bot & DB initialisation
# ----------------------------------------------------------------------
INTENTS = discord.Intents.all()

BOT = commands.Bot(command_prefix="g!", intents=INTENTS)
DB  = DBHelper()                     # singleton for the whole process

# ----------------------------------------------------------------------
#  Constants & Functions
# ----------------------------------------------------------------------
CHECK_INTERVAL  = 1 * 60     # seconds
DELETE_DELAY    = 15 * 60    # seconds

MAX_RANDNUM = 999999       # used for %randnum% macro max value
MIN_RANDNUM = 0             # used for %randnum% macro min value

async def send_temporary(
    channel: discord.abc.Messageable,
    content: str,
    *,
    delete_after: float = 120.0,   # seconds (2min by default)
    **send_kwargs,
) -> discord.Message:
    """
    Sends a message to ``channel`` and schedules it for deletion after
    ``delete_after`` seconds.

    Returns the sent :class:discord.Message so you can still interact with it
    (edit, add reactions, etc.) before it disappears.
    """
    msg = await channel.send(content, **send_kwargs)

    # Define a tiny coroutine that waits then deletes the message
    async def _deleter(message: discord.Message, delay: float):
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            # Message already gone or we lack perms – just ignore
            pass

    # Schedule the deleter as a background task (no awaiting here)
    asyncio.create_task(_deleter(msg, delete_after))

    # Return the message in case the caller wants to do something else with it
    return msg

# TODO: fix the command and make them all in the slash commands too
# ----------------------------------------------------------------------
#  COMMAND – let admins change the lobby channel
# ----------------------------------------------------------------------
@BOT.command(name="setlobby")
@commands.has_permissions(manage_guild=True)
async def set_lobby(ctx: commands.Context, channel: discord.VoiceChannel):
    """Save a new lobby channel ID in the guild‑wide settings JSON."""
    guild_id = ctx.guild.id
    print(f"DEBUG: channel {channel.name} guild {ctx.guild.name}")
    a = await DB.get_lobby(guild_id, channel.id)
    print(f"{a}")
    if a is not None:
        await ctx.reply(f"❌ **{channel.name}** is already a lobby channel.")
        return

    await DB.set_lobby(guild_id, channel.id, await DB.get_settings_guild(guild_id))
    await ctx.reply(f"✅ New lobby channel linked **{channel.name}**", ephemeral=True)


# ----------------------------------------------------------------------
#  EVENT – voice state updates (join detection)
# ----------------------------------------------------------------------
async def handle_lobby_update(
        member: discord.Member,
        after: discord.VoiceState
):
    guild_id = after.channel.guild.id
    lobby = await DB.get_lobby(guild_id, after.channel.id)
    if not lobby:
        return  # they joined some other channel

    member_voice_count = await DB.get_count_voice_channel_by_member(guild_id, member.id, after.channel.id)
    print(f"DEBUG: {member_voice_count} - Lobby:{lobby}")
    if member_voice_count < lobby['settings_json']['MaxVoiceChannels']:
        try:
            purpose = "general"

            default_overwrites = {
                member: discord.PermissionOverwrite(
                    speak=True,
                    connect=True,
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    manage_channels=True,
                    manage_permissions=True,
                    use_voice_activation=True
                )
            }

            # TODO: create function to handle all these - %macro% - macros
            vc_name = str(lobby['settings_json']['NameDefaults'][purpose])

            user_limit = 0

            user_settings = await DB.get_settings_user(guild_id, lobby['lobby_id'], member.id)

            if user_settings:
                purpose = "custom"
                print(f"DEBUG: {user_settings}")

                vc_name = user_settings["channel_name"]
                user_limit = user_settings["user_limit"]
                custom_overwrites = user_settings["overwrites"]

                new_overwrites = dict()
                for entry in custom_overwrites:
                    snowflake = entry["snowflake"]
                    is_role = entry["is_role"]
                    flags = entry["flags"]

                    if is_role:
                        valid_target = member.guild.get_role(snowflake)
                    else:
                        valid_target = member.guild.get_member(snowflake)

                    print(f"DEBUG: valid_target: {valid_target}")

                    if valid_target is None:
                        continue

                    overwrite = discord.PermissionOverwrite()
                    for name, value in flags.items():
                        setattr(overwrite, name, value)

                    if valid_target.id is member.id:
                        overwrite.update(
                            speak=True,
                            connect=True,
                            view_channel=True,
                            send_messages=True,
                            manage_messages=True,
                            manage_channels=True,
                            manage_permissions=True,
                            use_voice_activation=True
                        )

                    new_overwrites |= {
                        valid_target: overwrite,
                    }

                    print(f"DEBUG: {overwrite}")

                print(f"DEBUG: {new_overwrites.keys()}")

                default_overwrites = new_overwrites

            if vc_name.find("%num%") != -1:
                num = await DB.get_count_voice_channels(guild_id, lobby['lobby_id'])
                vc_name = vc_name.replace("%num%", str(num))

            vc_name = vc_name.replace("%randnum%", str(randint(MIN_RANDNUM, MAX_RANDNUM)).zfill(len(str(MAX_RANDNUM))))

            print(f"DEBUG: {vc_name}")

            new_vc = await after.channel.guild.create_voice_channel(
                name=vc_name,
                category=after.channel.category,
                reason=f"User {member.id} requested new channel",
                bitrate=int(after.channel.guild.bitrate_limit),
                user_limit=user_limit,
                overwrites=default_overwrites,
            )

            print(f"Voice channel created {new_vc.name}")

            await member.move_to(channel=new_vc, reason="Moving to the new requested channel")

            await DB.set_voice_channel(
                channel_id=new_vc.id,
                guild_id=guild_id,
                lobby_id=lobby['lobby_id'],
                owner_id=member.id,
                purpose=purpose,
                channel_name=vc_name,
            )

            await new_vc.send(
                content=f"You can now manually edit this channel and your setting will get saved and reused in future.\n\n**This feature is currently in beta!**\n*Please report any issues to the admins of this server. Thank you!*"
            )

        except discord.HTTPException:
            print(f"Could not create voice channel for member {member.id}. Notifying member through DM")
            try:
                dm = await member.create_dm()
                await dm.send(
                    content="Sorry we are having some issues with creating voice channels please try reconnecting or try again later.",
                    delete_after=120
                )
            finally:
                return

        print("DEBUG: Creating voice channel completed")
    else:
        await after.channel.send(
            content=f"{member.mention}\nYou already reached the maximum created channels ({lobby['settings_json']['MaxVoiceChannels']}) for this Lobby. Please use one of your created channels or contact server admins if there is an issue.",
            delete_after=60
        )

async def handle_voice_leave(member: discord.Member, before: discord.VoiceState):
    guild_id = before.channel.guild.id
    chan = await DB.get_voice_channel(guild_id, before.channel.id)
    print(f"DEBUG: handle_voice_leave {chan}")
    if chan is None:
        return

    if len(before.channel.voice_states) > 0:
        return              # if there is still someone in the channel

    await DB.update_voice_last_disconnect(guild_id, before.channel.id, int(time.time()) + DELETE_DELAY)

    print(f"DEBUG: Updated last_disconnect on {before.channel.name}")


@BOT.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Detect when a user joins the lobby voice channel and start the flow."""
    # --------------------------------------------------------------
    # Guard clauses
    # --------------------------------------------------------------
    if member.bot:
        return                      # ignore bots

    if before.channel == after.channel:
        return

    if after.channel is not None:
        await handle_lobby_update(member, after)

    if before.channel is not None:
        await handle_voice_leave(member, before)

    # await start_questionnaire(after, member)

# TODO: make the questionnaire better and functional (currently NOT working)
# ----------------------------------------------------------------------
#  QUESTIONNAIRE – UI that asks for Type & (optionally) game name
# ----------------------------------------------------------------------
# async def start_questionnaire(voice_state: discord.VoiceState, member: discord.Member) -> None:
#     """
#     Sends the interactive UI (Select → optional Modal) to member.
#     """
#
#
#     view = _TypeSelectView(member)
#     await voice_state.channel.send(
#         "👋 Hi! I’m going to set up a voice channel for you.\n"
#         "Please choose the type of room you’d like.",
#         view=view,
#     )
#
#
# class _TypeSelectView(discord.ui.View):
#     """First step – pick “gaming” or “general”. """
#     def __init__(self, author: discord.Member):
#         super().__init__(timeout=120)          # 2min auto‑close
#         self.author = author
#         self.add_item(_TypeSelect(author))
#
#
# class _TypeSelect(discord.ui.Select):
#     """Dropdown with the two possible types."""
#     def __init__(self, author: discord.Member):
#         options = [
#             discord.SelectOption(
#                 label="Gaming room",
#                 value="gaming",
#                 description="A room for playing a games together",
#             ),
#             discord.SelectOption(
#                 label="General room",
#                 value="general",
#                 description="Regular voice channel without any changes",
#             ),
#         ]
#         super().__init__(
#             placeholder="What kind of game you want?",
#             min_values=1,
#             max_values=1,
#             options=options,
#         )
#         self.author = author
#
#     async def callback(self, interaction: discord.Interaction):
#         # Only the user who started the flow may interact
#         if interaction.user.id != self.author.id:
#             await interaction.followup.send_message(
#                 "❌ This menu isn’t for you.", ephemeral=True
#             )
#             return
#
#         type_purpose = self.values[0]
#         await interaction.followup.defer()
#
#         if type_purpose == "gaming":
#             await interaction.followup.send_modal(_GameNameModal(self.author, type_purpose))
#         else:
#             # General room – we can create it immediately
#             await _create_temp_room(
#                 interaction,
#                 channel_name=type_purpose,
#             )
#
#
# class _GameNameModal(discord.ui.Modal, title="Game selection"):
#     """Modal that asks for the game name (optional)."""
#     game = discord.ui.TextInput(
#         label="Game name (leave blank for ‘Unknown’)",
#         placeholder="e.g. Valorant, Minecraft, …",
#         required=False,
#         max_length=50,
#     )
#
#     def __init__(self, author: discord.Member, purpose: str):
#         super().__init__()
#         self.author = author
#         self.purpose = purpose
#
#     async def on_submit(self, interaction: discord.Interaction):
#         if interaction.user.id != self.author.id:
#             await interaction.followup.send_message(
#                 "❌ Not your modal.", ephemeral=True
#             )
#             return
#
#         channel_name = self.game.value.strip() or "Unknown"
#         await _create_temp_room(
#             interaction,
#             purpose=self.purpose,
#             channel_name=channel_name,
#         )
#
#
# # ----------------------------------------------------------------------
# #  CREATE THE TEMPORARY VOICE CHANNEL
# # ----------------------------------------------------------------------
# async def _create_temp_room(
#     interaction: discord.Interaction,
#     *,
#     purpose: str,
#     channel_name: Optional[str] = None,
# ) -> None:
#     """
#     Handles the actual Discord channel creation, DB persistence,
#     and posting of the management embed.
#     """
#     guild   = interaction.guild
#     author  = interaction.user
#
#     # ----- Build a unique, friendly name ---------------------------------
#     if purpose == "gaming":
#         base_name = f"{channel_name or 'Game'} 🎮"
#     else:
#         base_name = "General Chat"
#
#     existing = [c for c in guild.voice_channels if c.name.startswith(base_name)]
#     suffix = len(existing) + 1 if existing else ""
#     channel_name = f"{base_name}{suffix}"
#
#     # ----- Permission overwrites -----------------------------------------
#     overwrites = {
#         guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
#         author: discord.PermissionOverwrite(
#             manage_channels=True, connect=True, speak=True
#         ),
#     }
#
#     # ----- Create the voice channel ---------------------------------------
#     new_vc = await guild.create_voice_channel(
#         name=channel_name,
#         overwrites=overwrites,
#         reason=f"Temporary channel created for {author}",
#     )
#
#     # ----- Store metadata ---------------------------
#     await DB.set_voice_channel(
#         channel_id=new_vc.id,
#         guild_id=guild.id,
#         owner_id=author.id,
#         purpose=purpose or None,
#         channel_name=channel_name,
#         private=False,
#         extra={"created_at": int(time.time())},
#     )
#
#     # ----- Send the embed with control buttons ---------------------------
#     embed = discord.Embed(
#         title="🔊 Temporary Voice Channel Created",
#         description=(
#             f"**Owner:** {author.mention}\n"
#             f"**Name:** {channel_name}\n"
#             f"**Purpose:** {purpose or 'N/A'}"
#         ),
#         colour=discord.Color.blurple(),
#         timestamp=discord.utils.utcnow(),
#     )
#     embed.add_field(name="Channel", value=new_vc.mention, inline=True)
#     embed.set_footer(text="Use the buttons below to manage the channel")
#
#     await interaction.followup.send(
#         embed=embed,
#         view=_ChannelControlView(new_vc.id, author.id),
#     )
#
#
# # ----------------------------------------------------------------------
# #  BUTTON VIEW – rename / toggle privacy / delete
# # ----------------------------------------------------------------------
# class _ChannelControlView(discord.ui.View):
#     """Three buttons that only the channel owner may press."""
#     def __init__(self, vc_id: int, owner_id: int):
#         super().__init__(timeout=None)   # persistent view if you ever need it
#         self.vc_id    = vc_id
#         self.owner_id = owner_id
#
#     async def interaction_check(self, interaction: discord.Interaction) -> bool:
#         if interaction.user.id != self.owner_id:
#             await interaction.followup.send_message(
#                 "❌ Only the channel owner can use these controls.", ephemeral=True
#             )
#             return False
#         return True
#
#     # ---------- Rename ---------------------------------------------------
#     @discord.ui.button(label="Rename", style=discord.ButtonStyle.primary)
#     async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
#         class _RenameModal(discord.ui.Modal, title="Rename Voice Channel"):
#             new_name = discord.ui.TextInput(
#                 label="New channel name",
#                 placeholder="Enter a new name",
#                 required=True,
#                 max_length=100,
#             )
#
#             async def on_submit(self, modal_inter: discord.Interaction):
#                 vc = interaction.guild.get_channel(self.vc_id)
#                 if not vc:
#                     await modal_inter.followup.send_message(
#                         "⚠️ Channel not found.", ephemeral=True
#                     )
#                     return
#                 await vc.edit(name=self.new_name.value)
#                 await modal_inter.followup.send_message(
#                     f"✅ Channel renamed to **{self.new_name.value}**", ephemeral=True
#                 )
#
#         await interaction.followup.send_modal(_RenameModal())
#
#     # ---------- Toggle privacy -------------------------------------------
#     @discord.ui.button(label="Make private", style=discord.ButtonStyle.secondary)
#     async def toggle_private(self, interaction: discord.Interaction, button: discord.ui.Button):
#         vc = interaction.guild.get_channel(self.vc_id)
#         if not vc:
#             await interaction.followup.send_message("⚠️ Channel not found.", ephemeral=True)
#             return
#
#         # Load current flag from DB (fallback to False)
#         row = await DB.get_voice_channel(vc.id)
#         current_private = row.get("private", False) if row else False
#         new_private = not current_private
#
#         # Update Discord permissions
#         overwrites = {
#             interaction.guild.default_role: discord.PermissionOverwrite(
#                 connect=not new_private
#             ),
#             interaction.user: discord.PermissionOverwrite(
#                 manage_channels=True, connect=True
#             ),
#         }
#         await vc.edit(overwrites=overwrites)
#
#         # Persist the new flag (whole‑JSON replace)
#         settings = row.get("settings", {}) if row else {}
#         settings["private"] = new_private
#         await DB.set_voice_channel_settings(vc.id, settings)
#
#         button.label = "Make public" if new_private else "Make private"
#         await interaction.followup.send_message(
#             f"🔒 Channel is now {'private' if new_private else 'public'}.", ephemeral=True
#         )
#
#     # ---------- Delete now -----------------------------------------------
#     @discord.ui.button(label="Delete now", style=discord.ButtonStyle.danger)
#     async def delete_now(self, interaction: discord.Interaction, button: discord.ui.Button):
#         vc = interaction.guild.get_channel(self.vc_id)
#         if vc:
#             await vc.delete(reason="Owner requested early deletion")
#         await DB.delete_voice_channel(guild_id=interaction.guild.id, channel_id=self.vc_id)
#         await interaction.followup.send_message("✅ Channel deleted.", ephemeral=True)


# ----------------------------------------------------------------------
#  PERIODIC CLEANUP – remove expired temporary channels
# ----------------------------------------------------------------------
@tasks.loop(seconds=CHECK_INTERVAL)
async def _prune_expired():
    """Runs every 2min, deletes DB rows & Discord channels that have expired."""
    # print("DEBUG: running prune_expired task")
    async for guild_id, channel_id, lobby_id, owner_id, channel_name, purpose, last_dc_time, save_settings, settings in DB.iterate_voice_rows():
        guild = BOT.get_guild(guild_id)
        channel = guild.get_channel(channel_id)

        if last_dc_time is None:
            # print(f"DEBUG: Nothing to do")
            continue

        if len(channel.voice_states) > 0:
            # await DB.update_voice_last_disconnect(guild_id, channel_id)
            print(f"DEBUG: Members are still in channel {channel.name} skipping...")
            continue

        if last_dc_time > int(time.time()):
            print(f"DEBUG: Channel {channel.name} did not reach its expiry yet, skipping...")
            continue

        try:
            if save_settings:
                print(f"DEBUG: {channel.overwrites}")

                chnl_name = channel.name
                if purpose != "custom" and channel_name == channel.name:
                    chnl_name = (await DB.get_settings_lobby(guild_id, lobby_id))["NameDefaults"][purpose]

                user_settings = {
                    "channel_name": chnl_name,
                    "user_limit": channel.user_limit,
                    "overwrites": [
                        {
                            "snowflake": target.id,
                            "is_role": isinstance(target, discord.Role),
                            "flags": {
                                perm_name: perm_value for perm_name, perm_value in (
                                    (perm, getattr(overwrite, perm)) for perm in overwrite.PURE_FLAGS
                                ) if perm_value is not None
                            }
                        }
                        for target, overwrite in channel.overwrites.items()
                    ],
                }

                print(f"DEBUG: {user_settings}")

                await DB.set_settings_user(guild_id, lobby_id, owner_id, user_settings)

            await channel.delete(reason="Auto-deleting temp channel due to long inactivity")
        except discord.HTTPException as e:
            print(f"Error while deleting channel {channel.name} on guild {guild.name}: {e}")
        finally:
            await DB.delete_voice_channel(guild_id, lobby_id, channel_id)
            print(f"Deleted channel {channel.name} on guild {guild.name} due to inactivity")



@BOT.event
async def on_ready():
    await DB.init()          # ensure tables exist
    _prune_expired.start()
    print(f"✅ Bot ready as {BOT.user}")