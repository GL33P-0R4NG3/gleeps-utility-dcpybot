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
        voice = ctx.guild.voice_client
        if voice and voice.channel == channel:
            await ctx.reply("I'm already in your voice channel.")
            return
        if not channel.permissions_for(ctx.guild.me).connect:
            await ctx.reply("I need Connect permission in your voice channel.")
            return
        try:
            if voice:
                await voice.move_to(channel)
            else:
                await channel.connect(reconnect=False, self_mute=True, self_deaf=True)
        except (TimeoutError, discord.DiscordException):
            await ctx.reply("I couldn't join your voice channel. You can try again.")
            return
        await ctx.reply(f"I'm idling in {channel.mention}.")

    @afk.command()
    @commands.guild_only()
    @commands.is_owner()
    async def leave(self, ctx):
        """End my idle voice session in this server."""
        voice = ctx.guild.voice_client
        if voice is None:
            await ctx.reply("I'm already out of voice.")
            return
        await voice.disconnect(force=True)
        await ctx.reply("I've left voice.")
