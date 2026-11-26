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
from music import Music, youtube_query


class MusicTests(unittest.IsolatedAsyncioTestCase):
    def test_inputs(self):
        self.assertEqual(youtube_query("some song"), "ytsearch1:some song")
        self.assertEqual(youtube_query("https://youtu.be/dQw4w9WgXcQ?t=30"),
                         "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(commands.BadArgument):
            youtube_query("https://example.org/video")

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
                new_callable=AsyncMock, return_value=("Song", "https://example.org/audio")) as lookup, patch(
                "music.discord.FFmpegOpusAudio"):
            await Music.play.callback(Music(), ctx, query="song")
            lookup.assert_not_awaited()
            ctx.author.roles = [Mock(id=42)]
            await asyncio.wait_for(Music.play.callback(Music(), ctx, query="song"), 2)
            lookup.assert_not_awaited()
            voice.play.assert_not_called()
            ctx.guild.voice_client = None
            channel.connect = AsyncMock(return_value=voice)
            await asyncio.wait_for(Music.play.callback(Music(), ctx, query="song"), 2)
            voice.play.assert_called_once()
            self.assertEqual(ctx.bot.voice_activity, {})

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
