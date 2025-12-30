# bot.py
# ------------------------------------------------------------
#  Imports & global objects
# ------------------------------------------------------------
import discord
from discord.ext import commands, tasks
import asyncio
import time
from typing import Optional, Dict, Any, List

from data.db_helper import DBHelper          # <-- Custom database helper

# ----------------------------------------------------------------------
#  Bot & DB initialisation
# ----------------------------------------------------------------------
INTENTS = discord.Intents.all()

BOT = commands.Bot(command_prefix="!glup", intents=INTENTS)
DB  = DBHelper()                     # singleton for the whole process

# ----------------------------------------------------------------------
#  Configuration helpers
# ----------------------------------------------------------------------
DEFAULT_LOBBY_ID = 123456789012345678   # ← replace with a fallback ID

#async def get_lobby_id(guild_id: int) -> int:
#    """Return the lobby channel ID for a guild (fallback → DEFAULT_LOBBY_ID)."""
#    settings = await DB.get_guild_settings(guild_id)
#    return settings.get("lobby_channel_id", DEFAULT_LOBBY_ID)

# ----------------------------------------------------------------------
#  COMMAND – let admins change the lobby channel
# ----------------------------------------------------------------------
@BOT.command(name="setlobby")
@commands.has_permissions(manage_guild=True)
async def set_lobby(ctx: commands.Context, channel: discord.VoiceChannel):
    """Save a new lobby channel ID in the guild‑wide settings JSON."""
    await DB.set_guild_settings(ctx.guild.id, {"lobby_channel_id": channel.id})
    await ctx.send(f"✅ Lobby channel set to **{channel.name}**", ephemeral=True)


# ----------------------------------------------------------------------
#  EVENT – voice state updates (join detection)
# ----------------------------------------------------------------------
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

    if after.channel is None:
        return                      # user left a channel – nothing to do

    if before.channel == after.channel:
        return


    lobby = await DB.get_lobby(after.channel.guild.id, after.channel.id)
    if not lobby:
        return                      # they joined some other channel


    await _start_questionnaire(after, member)


# ----------------------------------------------------------------------
#  QUESTIONNAIRE – UI that asks for Type & (optionally) game name
# ----------------------------------------------------------------------
async def _start_questionnaire(voice_state: discord.VoiceState, member: discord.Member) -> None:
    """
    Sends the interactive UI (Select → optional Modal) to *target*.
    If *target* is None we DM the member.
    """


    view = _TypeSelectView(member)
    await voice_state.channel.send(
        "👋 Hi! I’m going to set up a voice channel for you.\n"
        "Please choose the type of room you’d like:",
        view=view,
    )


class _TypeSelectView(discord.ui.View):
    """First step – pick “gaming” or “general”. """
    def __init__(self, author: discord.Member):
        super().__init__(timeout=120)          # 2min auto‑close
        self.author = author
        self.add_item(_TypeSelect(author))


class _TypeSelect(discord.ui.Select):
    """Dropdown with the two possible types."""
    def __init__(self, author: discord.Member):
        options = [
            discord.SelectOption(
                label="Gaming room",
                value="gaming",
                description="A room for playing a game together",
            ),
            discord.SelectOption(
                label="General chat room",
                value="general",
                description="Just a regular voice hangout",
            ),
        ]
        super().__init__(
            placeholder="What kind of temporary room do you need?",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.author = author

    async def callback(self, interaction: discord.Interaction):
        # Only the user who started the flow may interact
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ This menu isn’t for you.", ephemeral=True
            )
            return

        type_purpose = self.values[0]
        await interaction.response.defer()

        if type_purpose == "gaming":
            await interaction.followup.send_modal(_GameNameModal(self.author, type_purpose))
        else:
            # General room – we can create it immediately
            await _create_temp_room(
                interaction,
                channel_name=type_purpose,
            )


class _GameNameModal(discord.ui.Modal, title="Game selection"):
    """Modal that asks for the game name (optional)."""
    game = discord.ui.TextInput(
        label="Game name (leave blank for ‘Unknown’)",
        placeholder="e.g. Valorant, Minecraft, …",
        required=False,
        max_length=50,
    )

    def __init__(self, author: discord.Member, purpose: str):
        super().__init__()
        self.author = author
        self.purpose = purpose

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Not your modal.", ephemeral=True
            )
            return

        channel_name = self.game.value.strip() or "Unknown"
        await _create_temp_room(
            interaction,
            purpose=self.purpose,
            channel_name=channel_name,
        )


# ----------------------------------------------------------------------
#  CREATE THE TEMPORARY VOICE CHANNEL
# ----------------------------------------------------------------------
async def _create_temp_room(
    interaction: discord.Interaction,
    *,
    purpose: str,
    channel_name: Optional[str] = None,
) -> None:
    """
    Handles the actual Discord channel creation, DB persistence,
    and posting of the management embed.
    """
    guild   = interaction.guild
    author  = interaction.user

    # ----- Build a unique, friendly name ---------------------------------
    if purpose == "gaming":
        base_name = f"{channel_name or 'Game'} 🎮"
    else:
        base_name = "General Chat"

    existing = [c for c in guild.voice_channels if c.name.startswith(base_name)]
    suffix = len(existing) + 1 if existing else ""
    channel_name = f"{base_name}{suffix}"

    # ----- Permission overwrites -----------------------------------------
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
        author: discord.PermissionOverwrite(
            manage_channels=True, connect=True, speak=True
        ),
    }

    # ----- Create the voice channel ---------------------------------------
    new_vc = await guild.create_voice_channel(
        name=channel_name,
        overwrites=overwrites,
        reason=f"Temporary channel created for {author}",
    )

    # ----- Store metadata ---------------------------
    await DB.add_voice_channel(
        channel_id=new_vc.id,
        guild_id=guild.id,
        owner_id=author.id,
        purpose=purpose or None,
        channel_name=channel_name,
        private=False,
        extra={"created_at": int(time.time())},
    )

    # ----- Send the embed with control buttons ---------------------------
    embed = discord.Embed(
        title="🔊 Temporary Voice Channel Created",
        description=(
            f"**Owner:** {author.mention}\n"
            f"**Name:** {channel_name}\n"
            f"**Purpose:** {purpose or 'N/A'}"
        ),
        colour=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Channel", value=new_vc.mention, inline=True)
    embed.set_footer(text="Use the buttons below to manage the channel")

    await interaction.followup.send(
        embed=embed,
        view=_ChannelControlView(new_vc.id, author.id),
    )


# ----------------------------------------------------------------------
#  BUTTON VIEW – rename / toggle privacy / delete
# ----------------------------------------------------------------------
class _ChannelControlView(discord.ui.View):
    """Three buttons that only the channel owner may press."""
    def __init__(self, vc_id: int, owner_id: int):
        super().__init__(timeout=None)   # persistent view if you ever need it
        self.vc_id    = vc_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.followup.send_message(
                "❌ Only the channel owner can use these controls.", ephemeral=True
            )
            return False
        return True

    # ---------- Rename ---------------------------------------------------
    @discord.ui.button(label="Rename", style=discord.ButtonStyle.primary)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        class _RenameModal(discord.ui.Modal, title="Rename Voice Channel"):
            new_name = discord.ui.TextInput(
                label="New channel name",
                placeholder="Enter a new name",
                required=True,
                max_length=100,
            )

            async def on_submit(self, modal_inter: discord.Interaction):
                vc = interaction.guild.get_channel(self.vc_id)
                if not vc:
                    await modal_inter.followup.send_message(
                        "⚠️ Channel not found.", ephemeral=True
                    )
                    return
                await vc.edit(name=self.new_name.value)
                await modal_inter.followup.send_message(
                    f"✅ Channel renamed to **{self.new_name.value}**", ephemeral=True
                )

        await interaction.followup.send_modal(_RenameModal())

    # ---------- Toggle privacy -------------------------------------------
    @discord.ui.button(label="Make private", style=discord.ButtonStyle.secondary)
    async def toggle_private(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.get_channel(self.vc_id)
        if not vc:
            await interaction.followup.send_message("⚠️ Channel not found.", ephemeral=True)
            return

        # Load current flag from DB (fallback to False)
        row = await DB.get_voice_channel(vc.id)
        current_private = row.get("private", False) if row else False
        new_private = not current_private

        # Update Discord permissions
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(
                connect=not new_private
            ),
            interaction.user: discord.PermissionOverwrite(
                manage_channels=True, connect=True
            ),
        }
        await vc.edit(overwrites=overwrites)

        # Persist the new flag (whole‑JSON replace)
        settings = row.get("settings", {}) if row else {}
        settings["private"] = new_private
        await DB.set_voice_channel_settings(vc.id, settings)

        button.label = "Make public" if new_private else "Make private"
        await interaction.followup.send_message(
            f"🔒 Channel is now {'private' if new_private else 'public'}.", ephemeral=True
        )

    # ---------- Delete now -----------------------------------------------
    @discord.ui.button(label="Delete now", style=discord.ButtonStyle.danger)
    async def delete_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.get_channel(self.vc_id)
        if vc:
            await vc.delete(reason="Owner requested early deletion")
        await DB.delete_voice_channel(guild_id=interaction.guild.id, channel_id=self.vc_id)
        await interaction.followup.send_message("✅ Channel deleted.", ephemeral=True)


# ----------------------------------------------------------------------
#  PERIODIC CLEANUP – remove expired temporary channels
# ----------------------------------------------------------------------
@tasks.loop(minutes=5)
async def _prune_expired():
    """Runs every 5min, deletes DB rows & Discord channels that have expired."""
    DB.


@BOT.event
async def on_ready():
    await DB.init()          # ensure tables exist
    _prune_expired.start()
    print(f"✅ Bot ready as {BOT.user}")