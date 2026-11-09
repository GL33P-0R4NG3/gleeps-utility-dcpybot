"""Bot composition and Discord command entry points."""
import logging

import discord
from discord.ext import commands

from data.db_helper import DBHelper
from voice_channels import VoiceChannels
from semi_rooms import SemiRooms
from room_commands import RoomCommands
from sql_console import SQLConsole
from room_help import RoomHelpView
from lobby_access import apply_lobby_gate
from room_ringing import RoomRinging

log = logging.getLogger(__name__)


class UtilityBot(commands.Bot):
    def __init__(self, db=None):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="g!", intents=intents)
        self.db = db if db is not None else DBHelper()
        self.before_invoke(self.defer_slash_response)

    async def defer_slash_response(self, ctx):
        # Room creation and DB operations can exceed Discord's response deadline.
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

    async def setup_hook(self):
        await self.db.init()
        self.add_view(RoomHelpView())
        await self.add_cog(VoiceChannels(self, self.db))
        await self.add_cog(SemiRooms(self, self.db))
        await self.add_cog(RoomRinging(self))
        await self.add_cog(RoomCommands(self))
        await self.add_cog(SQLConsole(self))
        self.add_command(set_lobby.copy())
        synced = await self.tree.sync()
        log.info("Slash commands registered count=%s", len(synced))

    async def on_ready(self):
        log.info("Bot ready user_id=%s", self.user.id)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NotOwner):
            await ctx.reply("Only the bot owner can use this command.")
        elif isinstance(error, commands.UserInputError):
            await ctx.reply(f"{error}\nUse `g!help {ctx.command.qualified_name}` for command syntax.",
                            allowed_mentions=discord.AllowedMentions.none())
        elif isinstance(error, commands.CheckFailure):
            await ctx.reply(str(error) or "You do not have permission to use this command.",
                            allowed_mentions=discord.AllowedMentions.none())
        else:
            original = getattr(error, "original", error)
            log.error("Command failed command=%s guild_id=%s", ctx.command,
                      getattr(ctx.guild, "id", None), exc_info=(type(original), original, original.__traceback__))
            await ctx.reply("The command failed. The error has been logged; please try again later.")


BOT = UtilityBot()


class AccessRoleConverter(commands.RoleConverter, discord.app_commands.Transformer):
    @property
    def type(self):
        return discord.AppCommandOptionType.role

    async def transform(self, interaction, value):
        return value

    async def convert(self, ctx, argument):
        if argument.lower() in ("everyone", "@everyone"):
            return ctx.guild.default_role
        return await super().convert(ctx, argument)


@commands.hybrid_command(name="setlobby")
@discord.app_commands.guild_only()
@discord.app_commands.default_permissions(manage_guild=True)
@discord.app_commands.choices(mode=[discord.app_commands.Choice(name="Temporary", value="temp"),
                                   discord.app_commands.Choice(name="Private", value="semi")])
@discord.app_commands.describe(mode="Room type", channel="Voice channel members join to create a room",
                               role="Existing role allowed to create rooms; omit to keep the gate (everyone for a new lobby)",
                               lobbychannel="Public doorbell lobby for private rooms; omit to reuse or create 🛎️ Lobby")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def set_lobby(ctx: commands.Context, mode: str, channel: discord.VoiceChannel,
                    role: AccessRoleConverter = None, lobbychannel: discord.VoiceChannel = None):
    """Configure a creation lobby, with an optional private-room doorbell lobby."""
    mode = {"temp": "temporary", "temporary": "temporary", "semi": "semi", "private": "semi"}.get(mode.lower())
    if mode is None:
        raise commands.BadArgument("Choose temp or private as the lobby type.")
    if lobbychannel is not None and mode != "semi":
        raise commands.BadArgument("A doorbell lobby is only used with Private mode.")
    if lobbychannel is not None and lobbychannel.id == channel.id:
        raise commands.BadArgument("The waiting lobby must be separate from the creation channel.")
    configured = await ctx.bot.db.list_lobbies(ctx.guild.id)
    if any(row["settings_json"].get("RingLobbyID") == channel.id for row in configured):
        raise commands.BadArgument("That channel is a waiting lobby. Choose a separate creation channel.")
    if mode == "semi":
        await ctx.bot.get_cog("RoomRinging").validate_configuration(ctx.guild, channel, lobbychannel)
    existing = await ctx.bot.db.get_lobby(ctx.guild.id, channel.id)
    if role is None:
        from lobby_access import gate_role_id
        previous = gate_role_id(existing["settings_json"]) if existing else None
        role = ctx.guild.get_role(previous) if previous else ctx.guild.default_role
        if role is None:
            raise commands.BadArgument("The previous gate role was deleted. Choose an existing role or everyone.")
    if existing is None:
        settings = await ctx.bot.db.get_settings_guild(ctx.guild.id)
        await ctx.bot.db.set_lobby(ctx.guild.id, channel.id, settings)
    await ctx.bot.db.update_lobby_setting(ctx.guild.id, channel.id, "RoomMode", mode)
    await ctx.bot.db.update_lobby_setting(ctx.guild.id, channel.id, "GateRoleID", role.id)
    waiting = None
    if mode == "semi":
        waiting = await ctx.bot.get_cog("RoomRinging").configure(ctx.guild, channel, lobbychannel)
    await apply_lobby_gate(ctx.guild, channel.id, {"GateRoleID": role.id})
    await ctx.bot.get_cog("SemiRooms").sync_lobby_locks(ctx.guild)
    log.info("Lobby configured guild_id=%s channel_id=%s mode=%s", ctx.guild.id, channel.id, mode)
    access = ("Everyone may enter this lobby and create a room." if role.is_default()
              else f"Only members with {role.mention} may enter this lobby and create a room.")
    if waiting is not None:
        access += f" Visitors can wait in {waiting.mention} and use `/room ring`."
    await ctx.reply(f"**{channel.name}** is now a {'private' if mode == 'semi' else 'temporary'} room lobby. {access}",
                    allowed_mentions=discord.AllowedMentions.none())
