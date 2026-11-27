"""Queued YouTube playback with per-server role access."""
import asyncio
import json
import logging
import re
import subprocess
import shutil
from dataclasses import dataclass, field
from weakref import WeakValueDictionary
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


IDLE_TIMEOUT = 3600


@dataclass
class Request:
    query: str
    chat: discord.abc.Messageable

    async def report(self, message):
        try:
            await self.chat.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logging.getLogger(__name__).warning("Could not post music status in the request channel")


@dataclass
class Track:
    request: Request
    title: str
    download: subprocess.Popen
    source: MusicAudio
    playing: bool = False

    def close(self):
        if self.download.poll() is None:
            self.download.kill()
        self.download.wait()
        if not self.playing:  # The voice player owns source cleanup after play().
            self.source.cleanup()
        self.download.stdout.close()


async def prepare_track(request):
    title, video_id, format_id = await resolve_track(youtube_query(request.query))
    download = stream_track(video_id, format_id)
    try:
        source = MusicAudio(download.stdout, pipe=True, options="-map 0:a:0 -vn")
    except BaseException:
        download.kill()
        download.wait()
        download.stdout.close()
        raise
    track = Track(request, title, download, source)
    try:
        if not await asyncio.wait_for(asyncio.to_thread(source.prime), 25):
            raise commands.CommandError("YouTube returned no playable audio.")
        return track
    except BaseException:
        await asyncio.to_thread(track.close)
        raise


@dataclass
class Session:
    voice: discord.VoiceClient
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    closing: bool = False
    playing: bool = False
    waiting: bool = False
    idle_timer: asyncio.TimerHandle | None = None

    def cancel_idle(self):
        if self.idle_timer:
            self.idle_timer.cancel()
            self.idle_timer = None

    def check_idle(self):
        self.cancel_idle()
        if self.waiting and not self.playing and self.queue.empty():
            self.idle_timer = asyncio.get_running_loop().call_later(IDLE_TIMEOUT, self.expire)

    def expire(self):
        self.closing = True
        self.worker.cancel()


async def next_track(session):
    while True:
        session.waiting = True
        session.check_idle()
        request = await session.queue.get()
        session.waiting = False
        session.cancel_idle()
        try:
            return await prepare_track(request)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, commands.CommandError) else "Could not load the audio."
            await request.report(f"Skipping **{discord.utils.escape_markdown(request.query)}**. {detail}")


class Music(commands.Cog):
    def __init__(self):
        self.sessions = {}
        self.locks = WeakValueDictionary()

    async def allowed(self, ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        settings = await ctx.bot.db.get_settings_guild(ctx.guild.id)
        return any(role.id == settings.get("PlayRoleID") for role in ctx.author.roles)

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
    async def play(self, ctx, *, query: str):
        """Queue a YouTube link or song name in your voice channel."""
        if not await self.allowed(ctx):
            await ctx.reply("You need this server's music role or Administrator permission to play music.")
            return
        youtube_query(query)  # Validate locally; only the worker contacts YouTube.
        guild_id = ctx.guild.id
        async with self.locks.setdefault(guild_id, asyncio.Lock()):
            channel = ctx.author.voice.channel if ctx.author.voice else None
            if not isinstance(channel, discord.VoiceChannel):
                await ctx.reply("Join a voice channel first.")
                return
            session = self.sessions.get(guild_id)
            if session:
                if session.closing or session.voice.channel != channel or not session.voice.is_connected():
                    await ctx.reply("Join my active music channel, or try again after I disconnect.")
                    return
            else:
                if ctx.guild.voice_client is not None or guild_id in ctx.bot.voice_activity:
                    await ctx.reply("I already have a voice session in this server.")
                    return
                perms = channel.permissions_for(ctx.guild.me)
                if not (perms.connect and perms.speak):
                    await ctx.reply("I need Connect and Speak in your voice channel.")
                    return
                if any(shutil.which(tool) is None for tool in ("ffmpeg", "yt-dlp")):
                    await ctx.reply("The server needs FFmpeg and yt-dlp installed for music playback.")
                    return
                ctx.bot.voice_activity[guild_id] = "music"
                try:
                    voice = await channel.connect(self_deaf=True, reconnect=False)
                except BaseException:
                    ctx.bot.voice_activity.pop(guild_id, None)
                    raise
                session = self.sessions[guild_id] = Session(voice)
                session.worker = asyncio.create_task(self.run_session(ctx.bot, guild_id, session))
            session.cancel_idle()
            session.queue.put_nowait(Request(query, ctx.channel))
        await ctx.reply("Added to the music queue.")

    async def run_session(self, bot, guild_id, session):
        voice = session.voice
        loop = asyncio.get_running_loop()
        upcoming = asyncio.create_task(next_track(session))
        try:
            while True:
                track = await upcoming
                upcoming = None
                try:
                    if not await wait_for_voice_encryption(voice):
                        log_voice_state("encryption-unavailable", voice, track.source)
                        await track.request.report("Voice encryption is unavailable. Ending this music session.")
                        break
                    finished = loop.create_future()
                    def finish(error, future=finished):
                        if not future.done():
                            future.set_result(error)
                    log_voice_state("start", voice, track.source)
                    voice.play(track.source, after=lambda error, callback=finish: loop.call_soon_threadsafe(callback, error))
                    track.playing = session.playing = True
                    upcoming = asyncio.create_task(next_track(session))
                    await track.request.report(f"Playing **{discord.utils.escape_markdown(track.title)}** in {voice.channel.mention}.")
                    error = await finished
                    log_voice_state("end", voice, track.source)
                    if error or track.download.poll() not in (None, 0):
                        await track.request.report(f"Playback failed for **{discord.utils.escape_markdown(track.title)}**. Skipping.")
                except discord.ClientException:
                    await track.request.report(f"Could not play **{discord.utils.escape_markdown(track.title)}**. Skipping.")
                finally:
                    if track.playing and (not finished.done() or finished.cancelled()):
                        voice.stop()
                    session.playing = False
                    await asyncio.to_thread(track.close)
                    session.check_idle()
                if upcoming is None:
                    upcoming = asyncio.create_task(next_track(session))
        finally:
            session.closing = True
            session.cancel_idle()
            try:
                if upcoming is not None:
                    upcoming.cancel()
                    result, = await asyncio.gather(upcoming, return_exceptions=True)
                    if isinstance(result, Track):
                        await asyncio.to_thread(result.close)
                if voice.guild.voice_client is voice:
                    await voice.disconnect(force=True)
            finally:
                self.sessions.pop(guild_id, None)
                bot.voice_activity.pop(guild_id, None)

    async def stop_session(self, session):
        if not session.closing:
            session.closing = True
            await asyncio.sleep(0)  # Let a newly scheduled worker enter its cleanup block.
            session.worker.cancel()
        await asyncio.gather(session.worker, return_exceptions=True)

    @commands.hybrid_command()
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def leave(self, ctx):
        """End the music session and clear its queue."""
        if not await self.allowed(ctx):
            await ctx.reply("You need this server's music role or Administrator permission to end music.")
            return
        session = self.sessions.get(ctx.guild.id)
        if session is None:
            await ctx.reply("There is no music session to end.")
            return
        if not ctx.author.guild_permissions.administrator and (
                not ctx.author.voice or ctx.author.voice.channel != session.voice.channel):
            await ctx.reply("Join my music channel first.")
            return
        await self.stop_session(session)
        await ctx.reply("Music disconnected and the queue was cleared.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        session = self.sessions.get(member.guild.id)
        if session and member.id == member.guild.me.id and before.channel and after.channel is None:
            if not session.closing:
                await self.stop_session(session)

    async def cog_unload(self):
        await asyncio.gather(*(self.stop_session(session) for session in list(self.sessions.values())))
