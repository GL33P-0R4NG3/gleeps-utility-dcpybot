"""Single-track YouTube playback with per-server role access."""
import asyncio
import json
import logging
import re
import subprocess
import shutil
from pathlib import Path
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


COOKIE_FILE = Path(__file__).resolve().parents[2] / "yt-cookies.txt"
AUDIO_FORMAT = ("bestaudio[protocol=https]/best[protocol=https]/"
                "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]")


def cookie_options():
    return ["--cookies", str(COOKIE_FILE)] if COOKIE_FILE.is_file() else []


async def resolve_track(query):
    cookie_args = cookie_options()
    process = await asyncio.create_subprocess_exec(
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
        *cookie_args, "--encoding", "utf-8", "-f", AUDIO_FORMAT,
        "--check-formats", "--print", "%(.{title,id,format_id,acodec,protocol})j", "--", query,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, diagnostics = await asyncio.wait_for(process.communicate(), 45)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        if b"403" in diagnostics and b"forbidden" in diagnostics.lower():
            raise commands.CommandError("YouTube refused the media request (HTTP 403).")
        if cookie_args:
            # Cookie parser errors can quote malformed rows, including session values.
            if b"confirm" in diagnostics.lower() and b"bot" in diagnostics.lower():
                raise commands.CommandError("YouTube still requires verification with the configured cookies.")
            raise commands.CommandError("Authenticated YouTube lookup failed. Check the cookie file and video on the server.")
        lines = diagnostics.decode("utf-8", errors="replace").splitlines()
        detail = next((line for line in reversed(lines) if line.startswith("ERROR:")),
                      next(iter(reversed(lines)), "No diagnostic was returned."))
        # Media URLs can contain signed parameters; keep them out of replies and logs.
        detail = re.sub(r"https?://\S+", "[link]", detail)[:500]
        logging.getLogger(__name__).warning("YouTube lookup failed (exit %s): %s", process.returncode, detail)
        raise commands.CommandError(f"YouTube lookup failed. {discord.utils.escape_markdown(detail)}")
    if not output.strip():
        raise commands.CommandError("No YouTube video matched that search.")
    track = json.loads(output)
    logging.getLogger(__name__).info("Selected YouTube audio format=%s codec=%s protocol=%s",
                                     track.get("format_id"), track.get("acodec"), track.get("protocol"))
    return track["title"], track["id"], track["format_id"]


class MusicAudio(discord.FFmpegOpusAudio):
    """Buffer the first audio packet before announcing playback."""
    first_packet = b""
    frames_read = 0

    def prime(self):
        packet = super().read()
        while packet.startswith((b"OpusHead", b"OpusTags")):
            packet = super().read()
        self.first_packet = packet
        return bool(packet)

    def read(self):
        if self.first_packet:
            packet, self.first_packet = self.first_packet, b""
        else:
            packet = super().read()
        if packet:
            self.frames_read += 1
        return packet


def stream_track(video_id, format_id):
    # Let the extractor handle authenticated HTTP requests. FFmpeg receives media bytes.
    return subprocess.Popen(
        ["yt-dlp", "--ignore-config", "--no-playlist", "--quiet", "--no-progress",
         *cookie_options(), "--downloader", "native", "-f", format_id, "-o", "-", "--",
         f"https://www.youtube.com/watch?v={video_id}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


async def wait_for_voice_encryption(voice, timeout=15):
    # discord.py 2.7 checks this same internal state when encrypting an Opus frame.
    connection = voice._connection
    try:
        async with asyncio.timeout(timeout):
            while voice.is_connected() and connection.dave_protocol_version and not connection.can_encrypt:
                await asyncio.sleep(0.1)
    except TimeoutError:
        return False
    return voice.is_connected()


def log_voice_state(phase, voice, source):
    # discord.py 2.7 exposes DAVE readiness on its connection, not on VoiceClient.
    connection = getattr(voice, "_connection", None)
    logging.getLogger(__name__).info(
        "Voice playback %s guild=%s frames_read=%s transport=%s dave_version=%s dave_ready=%s",
        phase, voice.guild.id, source.frames_read, voice.mode,
        getattr(connection, "dave_protocol_version", None), getattr(connection, "can_encrypt", None))


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
            title, video_id, format_id = await resolve_track(target)
        except (TimeoutError, commands.CommandError) as error:
            await ctx.reply(str(error) or "YouTube took too long to respond. Try again.",
                            allowed_mentions=discord.AllowedMentions.none())
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

        download = None
        try:
            download = stream_track(video_id, format_id)
            source = MusicAudio(download.stdout, pipe=True, options="-map 0:a:0 -vn")
            try:
                if not await asyncio.wait_for(asyncio.to_thread(source.prime), 25):
                    source.cleanup()
                    await ctx.reply("FFmpeg received no playable audio from YouTube.")
                    return
                if not await wait_for_voice_encryption(voice):
                    log_voice_state("encryption-unavailable", voice, source)
                    source.cleanup()
                    await ctx.reply("The voice session did not become ready for playback. Please try again.")
                    return
                log_voice_state("start", voice, source)
                voice.play(source, after=lambda error: loop.call_soon_threadsafe(finish, error))
            except (TimeoutError, discord.ClientException):
                source.cleanup()
                await ctx.reply("I found the video, but could not start its audio stream.")
                return
            except BaseException:
                source.cleanup()
                raise
            await ctx.reply(f"Playing **{discord.utils.escape_markdown(title)}** in {channel.mention}.",
                            allowed_mentions=discord.AllowedMentions.none())
            error = await finished
            log_voice_state("end", voice, source)
            if error or download.poll() not in (None, 0):
                await ctx.channel.send("Playback ended early because the audio stream failed.")
        finally:
            if download is not None:
                if download.poll() is None:
                    download.kill()
                await asyncio.to_thread(download.wait)
                download.stdout.close()
            if ctx.guild.voice_client is voice and voice.is_connected():
                await voice.disconnect(force=True)
