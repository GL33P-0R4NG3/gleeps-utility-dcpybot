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
                                   discord.app_commands.Choice(name="Semi-temporary", value="semi")])
@discord.app_commands.describe(mode="Room type", channel="Voice channel members join to create a room",
                               role="Access role for semi rooms; omit to create one for each room")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def set_lobby(ctx: commands.Context, mode: str, channel: discord.VoiceChannel,
                    role: AccessRoleConverter = None):
    """Configure a lobby: g!setlobby temp|semi <channel> [@Role|everyone]."""
    mode = {"temp": "temporary", "temporary": "temporary", "semi": "semi"}.get(mode.lower())
    if mode is None:
        raise commands.BadArgument("Choose temp or semi as the lobby type.")
    if role is not None and mode != "semi":
        raise commands.BadArgument("Access roles apply to semi lobbies; temporary rooms keep their normal permissions.")
    existing = await ctx.bot.db.get_lobby(ctx.guild.id, channel.id)
    if existing is None:
        settings = await ctx.bot.db.get_settings_guild(ctx.guild.id)
        await ctx.bot.db.set_lobby(ctx.guild.id, channel.id, settings)
    await ctx.bot.db.update_lobby_setting(ctx.guild.id, channel.id, "RoomMode", mode)
    await ctx.bot.db.update_lobby_setting(ctx.guild.id, channel.id, "AccessRoleID", role.id if role else None)
    await ctx.bot.get_cog("SemiRooms").sync_lobby_locks(ctx.guild)
    log.info("Lobby configured guild_id=%s channel_id=%s mode=%s", ctx.guild.id, channel.id, mode)
    access = ""
    if mode == "semi":
        access = (" Everyone can join created rooms while visible." if role and role.is_default()
                  else f" Created rooms use {role.mention}." if role
                  else " A new access role will be created for each room.")
    await ctx.reply(f"**{channel.name}** is now a {mode} room lobby.{access}",
                    allowed_mentions=discord.AllowedMentions.none())
