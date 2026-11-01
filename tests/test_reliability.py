import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import discord
from data.db_helper import DBHelper
from voice_channels import VoiceChannels, DELETE_DELAY


def http_error(kind=discord.Forbidden):
    return kind(SimpleNamespace(status=403 if kind == discord.Forbidden else 404, reason="test"), "test")


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.db = DBHelper(str(self.path))
        await self.db.init()
        await self.db.get_settings_guild(1)
        await self.db.set_lobby(1, 2)

    async def test_defaults_and_json_types(self):
        self.assertEqual((await self.db.get_lobby(1, 2))["settings_json"]["MaxVoiceChannels"], 1)
        for key, value in (("MaxVoiceChannels", 3), ("enabled", True), ("NameDefaults", {"general": "Test"}), ("label", "hello")):
            await self.db.update_lobby_setting(1, 2, key, value)
            self.assertEqual((await self.db.get_settings_lobby(1, 2))[key], value)

    async def test_foreign_keys_and_cascade(self):
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.set_settings_user(1, 999, 3, {})
        await self.db.set_settings_user(1, 2, 3, {"channel_name": "saved"})
        await self.db.delete_lobby(1, 2)
        self.assertIsNone(await self.db.get_settings_user(1, 2, 3))

    async def test_snapshot_does_not_lock_writes(self):
        for channel_id in range(100, 170):
            await self.db.set_voice_channel(channel_id=channel_id, guild_id=1, lobby_id=2, owner_id=3, channel_name="room")
        async for row in self.db.iterate_voice_rows():
            await self.db.delete_voice_channel(1, 2, row[1])
        self.assertEqual(await self.db.get_count_voice_channels(1, 2), 0)

    async def test_legacy_migration_preserves_data_and_backs_up(self):
        await self.make_legacy()
        await self.db.init()
        self.assertEqual((await self.db.get_settings_user(1, 2, 3))["name"], "saved")
        self.assertEqual((await self.db.get_voice_channel(1, 4))["channel_name"], "room")
        self.assertEqual(len(list(Path(self.temp.name).glob("*.backup-*.db"))), 1)
        await self.db.init()  # Idempotent; no second backup needed.
        self.assertEqual(len(list(Path(self.temp.name).glob("*.backup-*.db"))), 1)
        await self.db.delete_lobby(1, 2)
        self.assertIsNone(await self.db.get_voice_channel(1, 4))

    async def make_legacy(self):
        # Recreate the old child definitions, including its single-column lobby FK.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for table in ("users", "voice_channels"):
                sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                conn.execute(f"DROP TABLE {table}")
                sql = sql.replace("FOREIGN KEY (guild_id, lobby_id) REFERENCES lobbies(guild_id, lobby_id)", "FOREIGN KEY (lobby_id) REFERENCES lobbies(lobby_id)")
                conn.execute(sql)
            conn.execute("INSERT INTO users VALUES (1, 2, 3, ?)", ('{"name":"saved"}',))
            conn.execute("INSERT INTO voice_channels (guild_id,lobby_id,channel_id,owner_id,channel_name) VALUES (1,2,4,3,'room')")

    async def test_orphan_migration_rolls_back(self):
        await self.make_legacy()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE users SET lobby_id=999")
        with self.assertRaisesRegex(RuntimeError, "orphaned"):
            await self.db.init()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT lobby_id FROM users").fetchone()[0], 999)
            self.assertIn("REFERENCES lobbies(lobby_id)", conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0])


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_and_shutdown_without_discord_login(self):
        from utility_bot import UtilityBot
        db = Mock(init=AsyncMock())
        bot = UtilityBot(db)
        bot.tree.sync = AsyncMock(return_value=[])
        async with bot:
            await bot.setup_hook()
            db.init.assert_awaited_once()
            bot.tree.sync.assert_awaited_once()
            cog = bot.get_cog("VoiceChannels")
            self.assertIsNotNone(cog)
            self.assertTrue(cog.prune_expired.is_running())
            task = cog.prune_expired.get_task()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.done())


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Mock()
        for method in ("update_voice_last_disconnect", "delete_voice_channel", "set_settings_user", "set_voice_channel"):
            setattr(self.db, method, AsyncMock())
        self.db.get_settings_lobby = AsyncMock(return_value={"NameDefaults": {"general": "Room %randnum%"}})
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 4
        self.channel.name = "room"
        self.channel.voice_states = {}
        self.channel.user_limit = 0
        self.channel.overwrites = {}
        self.channel.delete = AsyncMock()
        self.channel.send = AsyncMock()
        self.guild = Mock(id=1, unavailable=False, bitrate_limit=64000)
        self.guild.get_channel.return_value = self.channel
        self.guild.fetch_channel = AsyncMock()
        self.bot = Mock()
        self.bot.get_guild.return_value = self.guild
        self.service = VoiceChannels(self.bot, self.db)
        self.row = (1, 4, 2, 3, "room", "general", int(time.time()) - 10, True, "{}")

    async def test_failed_delete_retains_tracking(self):
        self.channel.delete.side_effect = http_error()
        with self.assertRaises(discord.Forbidden):
            await self.service.prune_row(self.row)
        self.db.delete_voice_channel.assert_not_awaited()

    async def test_save_failure_retains_room(self):
        self.db.set_settings_user.side_effect = sqlite3.OperationalError("locked")
        with self.assertRaises(sqlite3.OperationalError):
            await self.service.prune_row(self.row)
        self.channel.delete.assert_not_awaited()
        self.db.delete_voice_channel.assert_not_awaited()

    async def test_missing_channel_removes_record(self):
        self.guild.get_channel.return_value = None
        self.guild.fetch_channel.side_effect = http_error(discord.NotFound)
        await self.service.prune_row(self.row)
        self.db.delete_voice_channel.assert_awaited_once_with(1, 2, 4)

    async def test_missing_expiry_gets_grace_period(self):
        row = (*self.row[:6], None, *self.row[7:])
        await self.service.prune_row(row)
        args = self.db.update_voice_last_disconnect.await_args.args
        self.assertAlmostEqual(args[2], int(time.time()) + DELETE_DELAY, delta=2)
        self.channel.delete.assert_not_awaited()

    async def test_occupied_room_clears_expiry(self):
        self.channel.voice_states = {3: object()}
        await self.service.prune_row(self.row)
        self.db.update_voice_last_disconnect.assert_awaited_once_with(1, 4)
        self.channel.delete.assert_not_awaited()

    async def test_join_during_save_prevents_deletion(self):
        async def join(*args):
            self.channel.voice_states = {3: object()}
        self.db.set_settings_user.side_effect = join
        await self.service.prune_row(self.row)
        self.channel.delete.assert_not_awaited()

    async def test_cleanup_continues_after_one_bad_row(self):
        async def rows():
            yield self.row
            yield (*self.row[:1], 5, *self.row[2:])
        self.db.iterate_voice_rows = rows
        self.service.prune_row = AsyncMock(side_effect=[RuntimeError("bad room"), None])
        with self.assertLogs("voice_channels", level="ERROR"):
            await self.service.prune_expired()
        self.assertEqual(self.service.prune_row.await_count, 2)

    async def test_bot_departure_schedules_cleanup(self):
        member = Mock(bot=True, guild=self.guild)
        self.db.get_voice_channel = AsyncMock(return_value={"channel_id": 4})
        await self.service.on_voice_state_update(member, SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        self.db.update_voice_last_disconnect.assert_awaited_once()

    async def test_overlapping_events_do_not_create_twice(self):
        member, lobby = self.prepare_creation()
        member.bot = False
        self.db.get_voice_channel = AsyncMock(return_value=None)
        calls = []
        async def create(who, where):
            calls.append(who.id)
            await asyncio.sleep(0)
            who.voice.channel = self.channel
        self.service.create_room = create
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=lobby)
        await asyncio.gather(self.service.on_voice_state_update(member, before, after),
                             self.service.on_voice_state_update(member, before, after))
        self.assertEqual(calls, [3])

    def prepare_creation(self):
        member = Mock(id=3, guild=self.guild)
        lobby = Mock(id=2)
        member.voice = SimpleNamespace(channel=lobby)
        member.move_to = AsyncMock()
        self.guild.create_voice_channel = AsyncMock(return_value=self.channel)
        self.db.get_lobby = AsyncMock(return_value={"settings_json": {"MaxVoiceChannels": 1, "NameDefaults": {"general": "room"}}})
        self.db.get_member_voice_channels = AsyncMock(return_value=[])
        self.db.get_settings_user = AsyncMock(return_value=None)
        return member, lobby

    async def test_move_failure_rolls_back(self):
        member, lobby = self.prepare_creation()
        member.move_to.side_effect = http_error()
        with self.assertRaises(discord.Forbidden):
            await self.service.create_room(member, lobby)
        self.channel.delete.assert_awaited_once()
        self.db.delete_voice_channel.assert_awaited_once_with(1, 2, 4)

    async def test_failed_rollback_keeps_record(self):
        member, lobby = self.prepare_creation()
        member.move_to.side_effect = http_error()
        self.channel.delete.side_effect = http_error()
        with self.assertLogs("voice_channels", level="ERROR"):
            with self.assertRaises(discord.Forbidden):
                await self.service.create_room(member, lobby)
        self.db.delete_voice_channel.assert_not_awaited()

    async def test_welcome_failure_does_not_rollback(self):
        member, lobby = self.prepare_creation()
        self.channel.send.side_effect = http_error()
        with self.assertLogs("voice_channels", level="WARNING"):
            await self.service.create_room(member, lobby)
        self.channel.delete.assert_not_awaited()
        overwrites = self.guild.create_voice_channel.await_args.kwargs["overwrites"]
        self.assertEqual(list(overwrites), [member])
        self.assertTrue(overwrites[member].manage_channels)

    def test_saved_permissions_always_restore_owner(self):
        member, _ = self.prepare_creation()
        other = Mock()
        self.guild.get_role.return_value = other
        result = self.service.build_overwrites(member, {"overwrites": [{"snowflake": 9, "is_role": True, "flags": {"connect": False}}]})
        self.assertFalse(result[other].connect)
        self.assertTrue(result[member].connect)
        self.assertEqual(set(result), {member, other})


if __name__ == "__main__":
    unittest.main()
