"""Single-track YouTube playback with per-server role access."""
import asyncio
import json
import re
import shutil
from urllib.parse import parse_qs, urlsplit

import discord
from discord.ext import commands


def youtube_query(text):
    text = text.strip().strip("<>")
    if not text or len(text) > 500 or any(ord(c) < 32 for c in text):
        raise commands.BadArgument("Enter a YouTube link or a song name of up to 500 characters.")
    if text.startswith(("www.youtube.com/", "youtube.com/", "youtu.be/", "music.youtube.com/")):
        text = "https://" + text
    if "://" not in text:
        return "ytsearch1:" + text
    try:
        url = urlsplit(text)
        host = url.hostname
    except ValueError:
        raise commands.BadArgument("That YouTube link is invalid.")
    video = ""
    if host == "youtu.be":
        video = url.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if url.path == "/watch":
            video = parse_qs(url.query).get("v", [""])[0]
        elif url.path.startswith(("/shorts/", "/live/", "/embed/")):
            video = url.path.rsplit("/", 1)[-1]
    if url.scheme not in {"http", "https"} or not re.fullmatch(r"[\w-]{11}", video, re.ASCII):
        raise commands.BadArgument("Use a YouTube video link or search by song name.")
    return "https://www.youtube.com/watch?v=" + video


async def resolve_track(query):
    process = await asyncio.create_subprocess_exec(
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-warnings", "--no-progress",
        "--encoding", "utf-8", "-f", "bestaudio[protocol=https]/best[protocol=https]",
        "--print", "%(.{title,url})j", "--", query,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 45)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode or not output.strip():
        raise commands.CommandError("YouTube returned no playable track. Try another name or video.")
    track = json.loads(output)
    return track["title"], track["url"]


class Music(commands.Cog):
    @commands.hybrid_command()
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def setplayrole(self, ctx, role: discord.Role = None):
        """Choose who can play music. Omit the role for administrators only."""
        await ctx.bot.db.set_play_role(ctx.guild.id, role.id if role else None)
        await ctx.reply(f"{role.mention} and administrators can play music." if role else
                        "Only administrators can play music.", allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command()
    @discord.app_commands.guild_only()
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.guild)
    async def play(self, ctx, *, query: str):
        """Play one YouTube video by link or song name in your voice channel."""
        settings = await ctx.bot.db.get_settings_guild(ctx.guild.id)
        if not ctx.author.guild_permissions.administrator and not any(
                role.id == settings.get("PlayRoleID") for role in ctx.author.roles):
            await ctx.reply("You need this server's music role or Administrator permission to play music.")
            return
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.reply("Join a voice channel first.")
            return
        if ctx.guild.voice_client is not None or ctx.guild.id in ctx.bot.voice_activity:
            await ctx.reply("I already have a voice session in this server. End it before starting music.")
            return
        ctx.bot.voice_activity[ctx.guild.id] = "music"
        try:
            await self.play_track(ctx, channel, query)
        finally:
            ctx.bot.voice_activity.pop(ctx.guild.id, None)

    async def play_track(self, ctx, channel, query):
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.connect and perms.speak):
            await ctx.reply("I need Connect and Speak in your voice channel.")
            return
        target = youtube_query(query)
        if any(shutil.which(tool) is None for tool in ("ffmpeg", "yt-dlp")):
            await ctx.reply("The server needs FFmpeg and yt-dlp installed for music playback.")
            return
        try:
            title, url = await resolve_track(target)
        except (TimeoutError, commands.CommandError) as error:
            await ctx.reply(str(error) or "YouTube took too long to respond. Try again.")
            return
        if not ctx.author.voice or ctx.author.voice.channel != channel:
            await ctx.reply("You left the voice channel before playback started.")
            return
        voice = await channel.connect(self_deaf=True, reconnect=False)
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def finish(error):
            if not finished.done():
                finished.set_result(error)

        try:
            source = discord.FFmpegOpusAudio(url, before_options="-nostdin -rw_timeout 15000000", options="-vn")
            try:
                voice.play(source, after=lambda error: loop.call_soon_threadsafe(finish, error))
            except Exception:
                source.cleanup()
                raise
            await ctx.reply(f"Playing **{discord.utils.escape_markdown(title)}** in {channel.mention}.",
                            allowed_mentions=discord.AllowedMentions.none())
            if await finished:
                await ctx.channel.send("Playback ended early because the audio stream failed.")
        finally:
            if ctx.guild.voice_client is voice and voice.is_connected():
                await voice.disconnect(force=True)
