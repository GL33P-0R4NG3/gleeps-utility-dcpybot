"""Queued YouTube playback with per-server role access."""
import asyncio
import json
import logging
import re
import subprocess
import threading
from collections import deque
import shutil
from dataclasses import dataclass, field
from weakref import WeakValueDictionary, WeakSet
from music_queue_view import QueueView
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
    """Keep up to three seconds of encoded audio ahead of Discord's player."""
    buffer_frames = 150  # FFmpeg's Opus output uses 20 ms packets.

    def __init__(self, *args, **kwargs):
        self.frames_read = 0
        self._packets = deque()
        self._condition = threading.Condition()
        self._finished = False
        self._closed = False
        self._read_error = None
        super().__init__(*args, **kwargs)
        threading.Thread(target=self._fill_buffer, name="music-prebuffer", daemon=True).start()

    def _fill_buffer(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or len(self._packets) < self.buffer_frames)
                    if self._closed:
                        return
                packet = super().read()
                if not packet:
                    return
                if packet.startswith((b"OpusHead", b"OpusTags")):
                    continue
                with self._condition:
                    if self._closed:
                        return
                    self._packets.append(packet)
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                self._read_error = exc
        finally:
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    def prime(self):
        with self._condition:
            self._condition.wait_for(lambda: self._finished or self._closed or len(self._packets) >= self.buffer_frames)
            return bool(self._packets) and not self._closed

    def read(self):
        with self._condition:
            self._condition.wait_for(lambda: self._packets or self._finished or self._closed)
            if self._closed:
                return b""
            if not self._packets:
                if self._read_error:
                    raise self._read_error
                return b""
            packet = self._packets.popleft()
            self.frames_read += 1
            self._condition.notify_all()
            return packet

    def cleanup(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._packets.clear()
            self._condition.notify_all()
        super().cleanup()


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
QUEUE_LIMIT = 1000


@dataclass
class Request:
    query: str
    chat: discord.abc.Messageable
    id: int = 0

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
    skipped: bool = False

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
    pending: dict[int, Request] = field(default_factory=dict)
    last_id: int = 0
    current: Track | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    views: WeakSet = field(default_factory=WeakSet)
    worker: asyncio.Task | None = None
    closing: bool = False
    playing: bool = False
    waiting: bool = False
    pause_timer: asyncio.TimerHandle | None = None
    idle_timer: asyncio.TimerHandle | None = None

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()
        for view in list(self.views):
            view.refresh()

    def enqueue(self, request):
        if len(self.pending) >= QUEUE_LIMIT:
            return False
        self.cancel_idle()
        self.last_id += 1
        request.id = self.last_id
        self.pending[request.id] = request
        self.notify()
        return True

    def remove(self, ids):
        removed = 0
        for entry_id in ids:
            if self.pending.pop(entry_id, None) is not None:
                removed += 1
        if removed:
            self.notify()
        return removed

    def cancel_pause_timer(self):
        if self.pause_timer:
            self.pause_timer.cancel()
            self.pause_timer = None

    def cancel_idle(self):
        if self.idle_timer:
            self.idle_timer.cancel()
            self.idle_timer = None

    def check_idle(self):
        self.cancel_idle()
        if self.waiting and not self.playing and not self.pending:
            self.idle_timer = asyncio.get_running_loop().call_later(IDLE_TIMEOUT, self.expire)

    def expire(self):
        self.closing = True
        self.worker.cancel()


async def next_track(session):
    while True:
        while not session.pending:
            if not session.playing:
                session.cancel_pause_timer()
            session.waiting = True
            session.check_idle()
            await session.changed.wait()
        request = next(iter(session.pending.values()))
        session.waiting = False
        session.cancel_idle()
        try:
            track = await prepare_track(request)
        except Exception as exc:
            if session.remove([request.id]):
                detail = str(exc) if isinstance(exc, commands.CommandError) else "Could not load the audio."
                await request.report(f"Skipping **{discord.utils.escape_markdown(request.query)}**. {detail}")
            continue
        try:
            while session.playing and request.id in session.pending:
                await session.changed.wait()
            if request.id in session.pending:
                return track
        except BaseException:
            await asyncio.to_thread(track.close)
            raise
        await asyncio.to_thread(track.close)


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
            request = Request(query, ctx.channel)
            if not session.enqueue(request):
                await ctx.reply("This server has 1,000 tracks waiting. Please wait for a spot to open.")
                return
            position = len(session.pending)
        await ctx.reply(f"Added to the music queue at position #{position}.")

    async def run_session(self, bot, guild_id, session):
        voice = session.voice
        loop = asyncio.get_running_loop()
        upcoming = asyncio.create_task(next_track(session))
        try:
            while True:
                track = await upcoming
                upcoming = None
                try:
                    ready = await wait_for_voice_encryption(voice)
                    if track.request.id not in session.pending:
                        upcoming = asyncio.create_task(next_track(session))
                        continue
                    if not ready:
                        log_voice_state("encryption-unavailable", voice, track.source)
                        await track.request.report("Voice encryption is unavailable. Ending this music session.")
                        break
                    finished = loop.create_future()
                    def finish(error, future=finished):
                        if not future.done():
                            future.set_result(error)
                    session.playing = True
                    session.current = track
                    session.remove([track.request.id])
                    upcoming = asyncio.create_task(next_track(session))
                    # A skipped-to song stays at its first packet until explicitly resumed.
                    while session.pause_timer and not track.skipped:
                        await session.changed.wait()
                    if track.skipped:
                        continue
                    log_voice_state("start", voice, track.source)
                    voice.play(track.source, after=lambda error, callback=finish: loop.call_soon_threadsafe(callback, error))
                    track.playing = True
                    await track.request.report(f"Playing **{discord.utils.escape_markdown(track.title)}** in {voice.channel.mention}.")
                    error = await finished
                    log_voice_state("end", voice, track.source)
                    if not track.skipped and (error or track.download.poll() not in (None, 0)):
                        await track.request.report(f"Playback failed for **{discord.utils.escape_markdown(track.title)}**. Skipping.")
                except discord.ClientException:
                    await track.request.report(f"Could not play **{discord.utils.escape_markdown(track.title)}**. Skipping.")
                finally:
                    if track.playing and (not finished.done() or finished.cancelled()):
                        voice.stop()
                    if not track.skipped or not session.pending:
                        session.cancel_pause_timer()
                    session.playing = False
                    session.current = None
                    session.remove([track.request.id])
                    session.notify()
                    await asyncio.to_thread(track.close)
                    session.check_idle()
                if upcoming is None:
                    upcoming = asyncio.create_task(next_track(session))
        finally:
            session.closing = True
            session.cancel_pause_timer()
            session.pending.clear()
            session.notify()
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

    async def playback_session(self, ctx):
        if not await self.allowed(ctx):
            await ctx.reply("You need the music role or Administrator permission to control playback.")
            return None
        session = self.sessions.get(ctx.guild.id)
        if session is None or session.closing or session.current is None:
            await ctx.reply("There is no current song to control.")
            return None
        if not ctx.author.guild_permissions.administrator and (
                not ctx.author.voice or ctx.author.voice.channel != session.voice.channel):
            await ctx.reply("Join my music channel first.")
            return None
        return session

    @commands.hybrid_command()
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def stop(self, ctx):
        """Pause the current song in place. Use /resume to continue."""
        session = await self.playback_session(ctx)
        if session is None:
            return
        if session.pause_timer or session.voice.is_paused():
            await ctx.reply("Already paused. Use /resume to continue.")
        elif session.voice.is_playing():
            session.voice.pause()
            session.pause_timer = asyncio.get_running_loop().call_later(IDLE_TIMEOUT, session.expire)
            session.notify()
            await ctx.reply("Paused. Use /resume to continue.")
        else:
            await ctx.reply("The song is already ending.")

    @commands.hybrid_command()
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def resume(self, ctx):
        """Continue the paused song from the same position."""
        session = await self.playback_session(ctx)
        if session is None:
            return
        if not session.pause_timer and not session.voice.is_paused():
            await ctx.reply("Playback isn't paused.")
            return
        if session.voice.is_paused():
            session.voice.resume()
        session.cancel_pause_timer()
        session.notify()
        await ctx.reply("Playback resumed.")

    @commands.hybrid_command()
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def skip(self, ctx):
        """Skip the current song, including a paused song."""
        session = await self.playback_session(ctx)
        if session is None:
            return
        track = session.current
        held = bool(session.pause_timer and not track.playing)
        if track.skipped or not (held or session.voice.is_playing() or session.voice.is_paused()):
            await ctx.reply("The song is already ending.")
            return
        track.skipped = True
        has_next = bool(session.pending)
        keep_paused = bool(session.pause_timer and has_next)
        if not keep_paused:
            session.cancel_pause_timer()
        if held:
            session.notify()
        else:
            session.voice.stop()
            await asyncio.to_thread(track.source.cleanup)
        if keep_paused:
            await ctx.reply("Skipped. The next song will wait at the beginning until /resume.")
        else:
            await ctx.reply("Skipped the current song." if has_next else "Playback stopped. The queue is empty.")

    @commands.hybrid_group(name="queue", invoke_without_command=True)
    @discord.app_commands.guild_only()
    @commands.guild_only()
    async def queue_group(self, ctx):
        """View and manage the music queue."""
        await ctx.reply("Use /queue list or /queue clear.")

    @queue_group.command(name="list")
    @commands.guild_only()
    async def queue_list(self, ctx, page: int = 1):
        """Show what's playing and browse the live waiting list."""
        session = self.sessions.get(ctx.guild.id)
        if not session or session.closing:
            await ctx.reply("There is no music session in this server.")
            return
        if not ctx.author.guild_permissions.administrator and (
                not ctx.author.voice or ctx.author.voice.channel != session.voice.channel):
            await ctx.reply("Join my music channel to view its queue.")
            return
        view = QueueView(session, page, ctx.author.id)
        session.views.add(view)
        view.message = await ctx.reply(embed=view.render(), view=view, ephemeral=True)
        view.refresh()  # Include changes made while Discord was sending the reply.

    @queue_group.command(name="clear")
    @commands.guild_only()
    async def queue_clear(self, ctx, *, positions: str):
        """Remove waiting songs by their current positions, or enter all."""
        if not await self.allowed(ctx):
            await ctx.reply("You need the music role or Administrator permission to clear tracks.")
            return
        session = self.sessions.get(ctx.guild.id)
        if not session or session.closing:
            await ctx.reply("There is no music session in this server.")
            return
        if not ctx.author.guild_permissions.administrator and (
                not ctx.author.voice or ctx.author.voice.channel != session.voice.channel):
            await ctx.reply("Join my music channel first.")
            return
        if positions.strip().lower() == "all":
            selected = list(session.pending)
        else:
            parts = [part.strip().removeprefix("#") for part in positions.split(",")]
            if not all(re.fullmatch(r"[1-9][0-9]{0,15}", part) for part in parts):
                await ctx.reply("Enter all or comma-separated queue positions, for example 20,50,56.")
                return
            selected = set(map(int, parts))
            # Map every position before removing anything; lower positions may shift.
            snapshot = list(session.pending)
            if any(position > len(snapshot) for position in selected):
                await ctx.reply("A position is outside the current queue. Nothing was removed. Check /queue list.")
                return
            selected = [snapshot[position - 1] for position in selected]
        removed = session.remove(selected)
        await ctx.reply(f"Removed {removed} waiting " + ("track." if removed == 1 else "tracks."))

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
