import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
import discord
from discord.ext import commands
from data.db_helper import DBHelper
from idle_voice import IdleVoice
from music_queue_view import QueueView
from music import Music, Request, Session, Track, resolve_track, youtube_query, wait_for_voice_encryption


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_encryption_gate(self):
        voice = Mock()
        voice.is_connected.return_value = True
        connection = voice._connection
        connection.dave_protocol_version = 1
        connection.can_encrypt = False
        self.assertFalse(await wait_for_voice_encryption(voice, timeout=0))
        asyncio.get_running_loop().call_soon(setattr, connection, "can_encrypt", True)
        self.assertTrue(await wait_for_voice_encryption(voice))
        connection.dave_protocol_version = 0
        connection.can_encrypt = False
        self.assertTrue(await wait_for_voice_encryption(voice))
        voice.is_connected.return_value = False
        self.assertFalse(await wait_for_voice_encryption(voice))

    def test_inputs(self):
        self.assertEqual(youtube_query("some song"), "ytsearch1:some song")
        self.assertEqual(youtube_query("https://youtu.be/dQw4w9WgXcQ?t=30"),
                         "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(commands.BadArgument):
            youtube_query("https://example.org/video")

    async def test_extractor_error_is_distinct_from_empty_search(self):
        link = "https://www.youtube.com/watch?v=YDI3p1hIvdE"
        self.assertEqual(youtube_query(link), link)
        process = Mock(returncode=1, communicate=AsyncMock(return_value=(
            b"", b"ERROR: Sign in to confirm your age https://example.org/?secret=123")))
        with patch("music.COOKIE_FILE", new=Mock(is_file=lambda: False)), patch(
                "music.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process):
            with self.assertLogs("music", level="WARNING"), self.assertRaises(commands.CommandError) as caught:
                await resolve_track(link)
            self.assertIn("confirm your age", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
            process.returncode = 0
            with self.assertRaisesRegex(commands.CommandError, "No YouTube video matched"):
                await resolve_track("ytsearch1:song")

    async def test_optional_cookie_file(self):
        process = Mock(returncode=0, communicate=AsyncMock(return_value=(b'{"title":"song","id":"YDI3p1hIvdE","format_id":"251"}', b"")))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "yt-cookies.txt"
            with patch("music.COOKIE_FILE", path), patch("music.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=process) as launch:
                await resolve_track("ytsearch1:song")
                self.assertNotIn("--cookies", launch.await_args.args)
                path.touch()
                await resolve_track("ytsearch1:song")
                args = launch.await_args.args
                self.assertEqual(args[args.index("--cookies") + 1], str(path))
                process.returncode = 1
                process.communicate.return_value = (b"", b"ERROR: invalid cookie row containing SECRET")
                with self.assertRaises(commands.CommandError) as caught:
                    await resolve_track("ytsearch1:song")
                self.assertNotIn("SECRET", str(caught.exception))

    async def test_role_is_persistent_and_guild_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DBHelper(str(Path(tmp) / "test.db"))
            await db.init()
            await db.set_settings_guild(1, {"custom": True})
            await db.set_play_role(1, 123)
            self.assertEqual((await db.get_settings_guild(1))["PlayRoleID"], 123)
            self.assertIsNone((await db.get_settings_guild(2)).get("PlayRoleID"))
            await db.set_play_role(1, None)
            settings = await db.get_settings_guild(1)
            self.assertTrue(settings["custom"])
            self.assertIsNone(settings["PlayRoleID"])

    async def test_play_role_and_busy_voice_session(self):
        channel = Mock(spec=discord.VoiceChannel)
        ctx = Mock(reply=AsyncMock())
        ctx.author.voice.channel = channel
        ctx.author.guild_permissions.administrator = False
        ctx.bot.voice_activity = {}
        ctx.author.roles = []
        ctx.bot.db.get_settings_guild = AsyncMock(return_value={"PlayRoleID": 42})
        ctx.guild.change_voice_state = AsyncMock()
        voice = ctx.guild.voice_client
        voice.channel = channel
        voice.is_playing.return_value = voice.is_paused.return_value = False
        voice.play.side_effect = lambda source, after: after(None)
        with patch("music.shutil.which", return_value="tool"), patch("music.resolve_track",
                new_callable=AsyncMock, return_value=("Song", "YDI3p1hIvdE", "251")) as lookup, patch(
                "music.MusicAudio") as audio, patch("music.stream_track") as stream:
            stream.return_value.poll.return_value = 0
            await Music.play.callback(Music(), ctx, query="song")
            lookup.assert_not_awaited()
            ctx.author.roles = [Mock(id=42)]
            await asyncio.wait_for(Music.play.callback(Music(), ctx, query="song"), 2)
            lookup.assert_not_awaited()
            voice.play.assert_not_called()
            ctx.guild.voice_client = None
            channel.connect = AsyncMock(return_value=voice)
            cog = Music()
            voice.guild = ctx.guild
            ctx.channel.send = AsyncMock()
            await Music.play.callback(cog, ctx, query="song")
            for _ in range(100):
                if stream.return_value.stdout.close.called:
                    break
                await asyncio.sleep(0.01)
            stream.assert_called_once_with("YDI3p1hIvdE", "251")
            audio.assert_called_once_with(stream.return_value.stdout, pipe=True, options="-map 0:a:0 -vn")
            stream.return_value.stdout.close.assert_called_once()
            voice.play.assert_called_once()
            self.assertIn(ctx.guild.id, cog.sessions)
            await cog.leave.callback(cog, ctx)
            self.assertEqual(ctx.bot.voice_activity, {})

    async def test_limit_and_live_positions(self):
        cog = Music()
        ctx = Mock(reply=AsyncMock())
        ctx.author.guild_permissions.administrator = True
        session = cog.sessions[ctx.guild.id] = Session(Mock())
        for n in range(1000):
            self.assertTrue(session.enqueue(Request(f"song {n + 1}", ctx.channel)))
        self.assertFalse(session.enqueue(Request("overflow", ctx.channel)))
        session.remove([1])  # Former #2 becomes #1.
        self.assertTrue(session.enqueue(Request("tail", ctx.channel)))
        await cog.queue_clear.callback(cog, ctx, positions="1,3,3")
        self.assertEqual([r.query for r in list(session.pending.values())[:2]], ["song 3", "song 5"])
        await cog.queue_clear.callback(cog, ctx, positions="1,1000")
        self.assertEqual(len(session.pending), 998)
        view = QueueView(session, 1, 1)
        self.assertIn("**#1** \u00b7 song 3", view.render().fields[1].value)
        view.page = 100
        self.assertIn("tail", view.render().fields[-1].value)
        await cog.queue_clear.callback(cog, ctx, positions="all")
        self.assertEqual(len(session.pending), 0)
        self.assertIn("Page 1/1", view.render().footer.text)
        view.stop()

    async def test_live_list_edits_existing_message_when_first_track_starts(self):
        session = Session(Mock())
        first = Request("one", Mock())
        session.enqueue(first)
        session.enqueue(Request("two", Mock()))
        view = QueueView(session, 1, 42)
        session.views.add(view)
        view.message = Mock(edit=AsyncMock())
        session.current = Track(first, "Resolved song title", Mock(), Mock())
        session.remove([first.id])
        await asyncio.wait_for(view.update, 3)
        embed = view.message.edit.await_args.kwargs["embed"]
        self.assertEqual(embed.fields[0].value, "Resolved song title")
        self.assertIn("**#1** \u00b7 two", embed.fields[1].value)
        view.stop()

    async def test_removing_prefetched_track_prepares_next_while_song_plays(self):
        from music import next_track
        session = Session(Mock(), playing=True)
        chat = Mock()
        requests = [Request(name, chat) for name in ("removed", "kept")]
        for request in requests:
            session.enqueue(request)
        prepared = asyncio.Queue()
        async def prepare(request):
            track = Mock(request=request)
            prepared.put_nowait(track)
            return track
        with patch("music.prepare_track", side_effect=prepare):
            task = asyncio.create_task(next_track(session))
            first = await asyncio.wait_for(prepared.get(), 1)
            session.remove([requests[0].id])
            second = await asyncio.wait_for(prepared.get(), 1)
            first.close.assert_called_once()
            self.assertFalse(task.done())
            session.playing = False
            session.notify()
            self.assertIs(await asyncio.wait_for(task, 1), second)

    async def test_fifo_prefetch_failure_and_idle_disconnect(self):
        cog = Music()
        voice = Mock(disconnect=AsyncMock())
        voice.guild.voice_client = voice
        session = Session(voice)
        cog.sessions[1] = session
        bot = Mock(voice_activity={1: "music"})
        chat = Mock(send=AsyncMock())
        requests = [Request(name, chat) for name in ("first", "bad", "second", "third")]
        for request in requests:
            session.enqueue(request)
        tracks = {r.query: Track(r, r.query, Mock(), Mock()) for r in requests}
        for track in tracks.values():
            track.download.poll.return_value = 0
        prefetched = asyncio.Event()
        prepared = []
        callbacks = asyncio.Queue()
        async def prepare(request):
            prepared.append(request.query)
            if request.query == "bad":
                raise commands.CommandError("No song found.")
            if request.query == "second":
                prefetched.set()
            return tracks[request.query]
        voice.play.side_effect = lambda source, after: callbacks.put_nowait(after)
        with patch("music.prepare_track", side_effect=prepare), patch("music.IDLE_TIMEOUT", 0.05):
            session.worker = asyncio.create_task(cog.run_session(bot, 1, session))
            first_finished = await asyncio.wait_for(callbacks.get(), 1)
            await asyncio.wait_for(prefetched.wait(), 1)
            self.assertEqual(prepared, ["first", "bad", "second"])
            self.assertTrue(any("Skipping **bad**" in call.args[0] for call in chat.send.await_args_list))
            first_finished(None)
            for _ in range(2):
                callback = await asyncio.wait_for(callbacks.get(), 1)
                callback(None)
            await asyncio.wait_for(asyncio.gather(session.worker, return_exceptions=True), 2)
        self.assertEqual([call.args[0] for call in voice.play.call_args_list],
                         [tracks[name].source for name in ("first", "second", "third")])
        voice.disconnect.assert_awaited_once_with(force=True)
        self.assertFalse(cog.sessions)
        self.assertFalse(bot.voice_activity)

    async def test_afk_cannot_take_over_connected_or_loading_session(self):
        ctx = Mock(reply=AsyncMock())
        channel = ctx.author.voice.channel
        channel.connect = AsyncMock()
        voice = ctx.guild.voice_client
        voice.move_to = AsyncMock()
        voice.disconnect = AsyncMock()
        ctx.bot.voice_activity = {}
        await IdleVoice.join.callback(IdleVoice(), ctx)
        voice.move_to.assert_not_awaited()
        ctx.bot.voice_activity[ctx.guild.id] = "music"
        await IdleVoice.leave.callback(IdleVoice(), ctx)
        voice.disconnect.assert_not_awaited()
        ctx.guild.voice_client = None
        await IdleVoice.join.callback(IdleVoice(), ctx)
        channel.connect.assert_not_awaited()
