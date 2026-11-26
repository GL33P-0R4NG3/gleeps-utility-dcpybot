"""Owner commands for an idle voice session."""
import discord
from discord.ext import commands


class IdleVoice(commands.Cog):
    @commands.hybrid_group(invoke_without_command=True)
    @discord.app_commands.guild_only()
    async def afk(self, ctx):
        """Control my idle voice session."""
        await ctx.reply("Use `g!afk join` or `g!afk leave`.")

    @afk.command()
    @commands.guild_only()
    @commands.is_owner()
    async def join(self, ctx):
        """Join your voice channel muted and deafened."""
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            await ctx.reply("Join a voice channel first.")
            return
        if ctx.guild.voice_client is not None or ctx.guild.id in ctx.bot.voice_activity:
            await ctx.reply("I already have a voice session in this server.")
            return
        if not channel.permissions_for(ctx.guild.me).connect:
            await ctx.reply("I need Connect permission in your voice channel.")
            return
        ctx.bot.voice_activity[ctx.guild.id] = "joining"
        try:
            await channel.connect(reconnect=False, self_mute=True, self_deaf=True)
        except (TimeoutError, discord.DiscordException):
            await ctx.reply("I couldn't join your voice channel. You can try again.")
            return
        finally:
            ctx.bot.voice_activity.pop(ctx.guild.id, None)
        await ctx.reply(f"I'm idling in {channel.mention}.")

    @afk.command()
    @commands.guild_only()
    @commands.is_owner()
    async def leave(self, ctx):
        """End my idle voice session in this server."""
        if ctx.guild.id in ctx.bot.voice_activity:
            await ctx.reply("There is no idle AFK session to end right now.")
            return
        voice = ctx.guild.voice_client
        if voice is None:
            await ctx.reply("I'm already out of voice.")
            return
        await voice.disconnect(force=True)
        await ctx.reply("I've left voice.")
