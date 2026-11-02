import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView
from data.db_helper import DBHelper
from data.sql_runner import execute_sql, run_sql
from room_commands import RoomCommands, DeleteRoomView
from room_permissions import can_manage_room, visibility_overwrites
from semi_rooms import SemiRooms
from sql_console import SQLConsole, SQLView, SQLModal
from voice_channels import VoiceChannels


def discord_error(kind=discord.NotFound):
    return kind(SimpleNamespace(status=404 if kind is discord.NotFound else 403, reason="test"), "test")


def permission_fixture():
    everyone = Mock(spec=discord.Role, id=1, permissions=discord.Permissions.none())
    access = Mock(spec=discord.Role, id=2, permissions=discord.Permissions.none())
    manager_role = Mock(spec=discord.Role, id=3, permissions=discord.Permissions.none())
    owner = Mock(spec=discord.Member, id=10, roles=[everyone], guild_permissions=discord.Permissions.none())
    manager = Mock(spec=discord.Member, id=11, roles=[everyone, manager_role], guild_permissions=discord.Permissions.none())
    guest = Mock(spec=discord.Member, id=12, roles=[everyone, access], guild_permissions=discord.Permissions.none())
    bot_member = Mock(spec=discord.Member, id=13, roles=[everyone], guild_permissions=discord.Permissions.none())
    guild = Mock(id=1, owner_id=999, default_role=everyone, me=bot_member, members=[owner, manager, guest, bot_member], unavailable=False)
    channel = Mock(spec=discord.VoiceChannel, id=20, guild=guild)
    channel.overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=True, connect=False, send_messages=False),
        access: discord.PermissionOverwrite(view_channel=True, connect=True),
        manager_role: discord.PermissionOverwrite(manage_channels=True, connect=True),
        owner: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
        guest: discord.PermissionOverwrite(view_channel=True, speak=False),
        bot_member: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
    }
    channel.overwrites_for.side_effect = lambda who: channel.overwrites.get(who, discord.PermissionOverwrite())
    return channel, owner, manager, guest, access


class PermissionTests(unittest.TestCase):
    def test_visibility_changes_only_view_permission(self):
        channel, owner, manager, guest, access = permission_fixture()
        hidden = visibility_overwrites(channel, owner.id, True)
        self.assertTrue(hidden[owner].view_channel)
        self.assertNotIn(manager, hidden)
        self.assertTrue(hidden[manager.roles[-1]].view_channel)
        self.assertFalse(hidden[guest].view_channel)
        self.assertFalse(hidden[access].view_channel)
        view_bit = discord.Permissions(view_channel=True).value
        for target, overwrite in hidden.items():
            old = channel.overwrites.get(target, discord.PermissionOverwrite())
            self.assertEqual(tuple(p.value & ~view_bit for p in overwrite.pair()),
                             tuple(p.value & ~view_bit for p in old.pair()))
        channel.overwrites = hidden
        shown = visibility_overwrites(channel, owner.id, False)
        self.assertTrue(all(item.view_channel for item in shown.values()))
        self.assertFalse(shown[channel.guild.default_role].connect)
        self.assertTrue(shown[access].connect)
        self.assertFalse(shown[guest].speak)

    def test_visibility_never_adds_users_roles_or_bots(self):
        channel, owner, manager, guest, access = permission_fixture()
        admin_bot = Mock(spec=discord.Member, id=99, guild_permissions=discord.Permissions(administrator=True))
        channel.guild.members.append(admin_bot)
        original_targets = set(channel.overwrites)
        hidden = visibility_overwrites(channel, owner.id, True)
        self.assertEqual(set(hidden), original_targets)
        self.assertNotIn(admin_bot, hidden)
        self.assertNotIn(manager, hidden)
        channel.overwrites = hidden
        self.assertEqual(set(visibility_overwrites(channel, owner.id, False)), original_targets)

    def test_visibility_does_not_reinsert_deleted_default_or_bot_overwrites(self):
        channel, owner, manager, guest, access = permission_fixture()
        del channel.overwrites[channel.guild.default_role]
        del channel.overwrites[channel.guild.me]
        self.assertEqual(set(visibility_overwrites(channel, owner.id, True)), set(channel.overwrites))

    def test_manager_resolution_ignores_visibility_but_respects_explicit_denial(self):
        channel, owner, manager, guest, access = permission_fixture()
        channel.overwrites[manager] = discord.PermissionOverwrite(view_channel=False)
        self.assertTrue(can_manage_room(channel, manager, owner.id))
        channel.overwrites[manager].manage_channels = False
        self.assertFalse(can_manage_room(channel, manager, owner.id))
        self.assertFalse(can_manage_room(channel, guest, owner.id))


class LobbyGateTests(unittest.TestCase):
    def test_gate_requires_role_even_for_administrator(self):
        from lobby_access import may_create_room
        channel, owner, manager, guest, access = permission_fixture()
        for member in (owner, guest):
            member.guild = channel.guild
        settings = {"GateRoleID": access.id}
        self.assertTrue(may_create_room(guest, settings))
        owner.guild_permissions.administrator = True
        self.assertFalse(may_create_room(owner, settings))
        self.assertTrue(may_create_room(owner, {"GateRoleID": channel.guild.default_role.id}))

    def test_other_role_and_member_allows_cannot_bypass_gate(self):
        from lobby_access import lobby_overwrites
        channel, owner, manager, guest, access = permission_fixture()
        updated = lobby_overwrites(channel, access)
        self.assertFalse(updated[channel.guild.default_role].connect)
        self.assertTrue(updated[access].connect)
        self.assertIsNone(updated[owner].connect)
        for role in manager.roles:
            if role != channel.guild.default_role:
                self.assertIsNone(updated[role].connect)
        self.assertFalse(updated[guest].speak)



class FeatureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DBHelper(str(Path(self.temp.name) / "test.db"))
        await self.db.init()
        await self.db.get_settings_guild(1)
        await self.db.set_lobby(1, 2)
        await self.db.set_voice_channel(channel_id=20, guild_id=1, lobby_id=2, owner_id=10, channel_name="room")

    async def test_sql_read_update_and_backup(self):
        result = await execute_sql(self.db.path, "SELECT channel_id FROM voice_channels")
        self.assertEqual(result.rows, [[20]])
        self.assertIsNone(result.backup)
        result = await execute_sql(self.db.path, "UPDATE voice_channels SET channel_name='new' WHERE channel_id=20")
        self.assertEqual(result.changes, 1)
        self.assertEqual((await self.db.get_voice_channel(1, 20))["channel_name"], "new")
        with closing(sqlite3.connect(Path(self.temp.name) / result.backup)) as backup:
            self.assertEqual(backup.execute("SELECT channel_name FROM voice_channels").fetchone()[0], "room")

    async def test_sql_failure_is_atomic(self):
        with self.assertRaises(sqlite3.IntegrityError):
            await execute_sql(self.db.path, "UPDATE voice_channels SET lobby_id=999")
        self.assertEqual((await self.db.get_voice_channel(1, 20))["lobby_id"], 2)

    async def test_sql_cte_delete_and_schema_queries(self):
        result = await execute_sql(self.db.path, "PRAGMA table_info(semi_rooms)")
        self.assertTrue(any(row[1] == "owner_id" for row in result.rows))
        result = await execute_sql(self.db.path, "WITH chosen AS (SELECT 20 AS id) DELETE FROM voice_channels WHERE channel_id IN (SELECT id FROM chosen)")
        self.assertEqual(result.changes, 1)
        self.assertIsNotNone(result.backup)
        self.assertIsNone(await self.db.get_voice_channel(1, 20))

    async def test_sql_rejects_external_files_and_multiple_statements(self):
        for statement in ("ATTACH DATABASE 'external.db' AS x", "PRAGMA foreign_keys=OFF", "SELECT load_extension('bad')", "SELECT 1; DELETE FROM voice_channels", "COMMIT"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                await execute_sql(self.db.path, statement)
        self.assertIsNotNone(await self.db.get_voice_channel(1, 20))

    async def test_sql_time_and_result_limits(self):
        result = await execute_sql(self.db.path, "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<200) SELECT n FROM x")
        self.assertEqual(len(result.rows), 100)
        self.assertTrue(result.truncated)
        with self.assertRaises(sqlite3.OperationalError):
            await asyncio.to_thread(run_sql, self.db.path, "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x", seconds=0.01)

    async def test_semi_room_uniqueness_and_roundtrip(self):
        await self.db.set_semi_room(1, 21, 10, 30, True)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.set_semi_room(1, 22, 10, 31, False)
        await self.db.update_semi_state(21, hidden=False, expires_at=123)
        row = await self.db.get_semi_room(1, 21)
        self.assertEqual(row["expires_at"], 123)
        self.assertEqual(row["hidden"], 0)
        # Temporary and semi-temporary quotas are separate.
        self.assertEqual(len(await self.db.get_member_voice_channels(1, 10)), 1)
        await self.db.delete_semi_room(1, 21)
        self.assertEqual(await self.db.list_semi_rooms(), [])

    async def test_native_delete_releases_slot_immediately(self):
        bot = Mock()
        bot.db.list_room_openers = AsyncMock(return_value=set())
        bot.db.replace_room_openers = AsyncMock()
        channel = Mock(id=20, guild=Mock(id=1))
        await VoiceChannels(bot, self.db).on_guild_channel_delete(channel)
        self.assertEqual(await self.db.get_member_voice_channels(1, 10), [])

    async def test_offline_deleted_room_is_removed_before_limit_check(self):
        guild = Mock(id=1, bitrate_limit=64000)
        guild.get_channel.return_value = None
        guild.fetch_channel = AsyncMock(side_effect=discord_error())
        member = Mock(id=10, guild=guild)
        lobby = Mock(id=2, send=AsyncMock())
        member.voice = SimpleNamespace(channel=lobby)
        member.move_to = AsyncMock()
        new_channel = Mock(id=21, send=AsyncMock())
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        await VoiceChannels(Mock(), self.db).create_room(member, lobby)
        self.assertIsNone(await self.db.get_voice_channel(1, 20))
        self.assertIsNotNone(await self.db.get_voice_channel(1, 21))
        lobby.send.assert_not_awaited()

    async def test_limit_applies_across_lobbies(self):
        await self.db.set_lobby(1, 3)
        guild = Mock(id=1)
        member = Mock(id=10, guild=guild)
        lobby = Mock(id=3, send=AsyncMock())
        guild.create_voice_channel = AsyncMock()
        await VoiceChannels(Mock(), self.db).create_room(member, lobby)
        guild.create_voice_channel.assert_not_awaited()
        lobby.send.assert_awaited_once()

    def semi_creation_fixture(self):
        guild = Mock(id=1, default_role=Mock(), me=Mock())
        role = Mock(id=30, guild=guild)
        role.is_default.return_value = False
        role.delete = AsyncMock()
        guild.create_role = AsyncMock(return_value=role)
        channel = Mock(id=21, guild=guild, delete=AsyncMock(), send=AsyncMock(), voice_states={})
        guild.create_voice_channel = AsyncMock(return_value=channel)
        member = Mock(id=10, guild=guild)
        service = SemiRooms(Mock(), self.db)
        service.set_visibility = AsyncMock()
        return service, guild, member, channel, role

    async def test_private_creation_requires_gate_role_on_lobby_and_command_paths(self):
        service, guild, member, channel, access = self.semi_creation_fixture()
        member.roles = []
        await self.db.update_lobby_setting(1, 2, "RoomMode", "semi")
        await self.db.update_lobby_setting(1, 2, "GateRoleID", 99)
        lobby = Mock(id=2)
        member.voice = SimpleNamespace(channel=lobby)
        self.assertIsNone(await service.create_room(member, None, "Private room", source_lobby=lobby))
        with self.assertRaises(commands.CheckFailure):
            await service.create_room(member, None, "Private room")
        guild.create_role.assert_not_awaited()
        guild.create_voice_channel.assert_not_awaited()
        member.roles = [Mock(id=99)]
        member.move_to = AsyncMock()
        await service.create_room(member, None, "Private room", source_lobby=lobby)
        overwrites = guild.create_voice_channel.await_args.kwargs["overwrites"]
        self.assertFalse(overwrites[guild.default_role].connect)
        self.assertNotIn(member.roles[0], overwrites)
        self.assertTrue(overwrites[member].manage_channels)
        member.move_to.assert_awaited_once()

    async def test_lobby_event_does_not_pass_gate_role_into_private_room(self):
        service, guild, member, channel, access = self.semi_creation_fixture()
        member.roles = [Mock(id=99)]
        member.bot = False
        member.display_name = "Owner"
        await self.db.update_lobby_setting(1, 2, "RoomMode", "semi")
        await self.db.update_lobby_setting(1, 2, "AccessRoleID", 99)
        lobby = Mock(id=2)
        service.create_room = AsyncMock()
        await service.on_voice_state_update(member, SimpleNamespace(channel=None), SimpleNamespace(channel=lobby))
        self.assertEqual(len(service.create_room.await_args.args), 3)
        self.assertNotIn("role", service.create_room.await_args.kwargs)

    async def test_semi_lobby_creates_and_moves_owner(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        lobby = Mock(id=2)
        member.voice = SimpleNamespace(channel=lobby)
        member.move_to = AsyncMock()
        await service.create_room(member, None, "Private room", source_lobby=lobby)
        member.move_to.assert_awaited_once_with(channel, reason="Moving to newly created private room")
        self.assertIsNotNone(await self.db.get_semi_room(1, 21))

    async def test_existing_owner_forced_into_lobby_is_noop(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        await self.db.set_semi_room(1, 21, 10, 30, True)
        member.move_to = AsyncMock()
        result = await service.create_room(member, None, "Private room", source_lobby=Mock())
        self.assertIsNone(result)
        guild.create_voice_channel.assert_not_awaited()
        guild.create_role.assert_not_awaited()
        member.move_to.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_failed_lobby_move_rolls_back_room_without_touching_roles(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        lobby = Mock()
        member.voice = SimpleNamespace(channel=lobby)
        member.move_to = AsyncMock(side_effect=discord_error(discord.Forbidden))
        with self.assertRaises(discord.Forbidden):
            await service.create_room(member, None, "Private room", source_lobby=lobby)
        channel.delete.assert_awaited_once()
        role.delete.assert_not_awaited()
        self.assertIsNone(await self.db.get_semi_room(1, 21))

    async def test_lobby_lock_restores_previous_connect_after_restart(self):
        await self.db.update_lobby_setting(1, 2, "RoomMode", "semi")
        await self.db.set_semi_room(1, 21, 10, 30, True)
        guild = Mock(id=1, unavailable=False)
        member = Mock(id=10)
        guild.get_member.return_value = member
        overwrite = discord.PermissionOverwrite(connect=True, speak=False)
        lobby = Mock()
        lobby.overwrites_for.side_effect = lambda target: discord.PermissionOverwrite.from_pair(*overwrite.pair())
        async def apply(target, *, overwrite=None, reason=None):
            saved = overwrite
            state[0] = saved
        state = [overwrite]
        lobby.overwrites_for.side_effect = lambda target: discord.PermissionOverwrite.from_pair(*state[0].pair())
        lobby.set_permissions = AsyncMock(side_effect=apply)
        guild.fetch_channel = AsyncMock(return_value=lobby)
        await SemiRooms(Mock(), self.db).sync_lobby_locks(guild)
        self.assertFalse(state[0].connect)
        self.assertFalse(state[0].speak)
        self.assertEqual((await self.db.list_lobby_locks(1))[0]["previous_connect"], 1)
        await self.db.delete_semi_room(1, 21)
        await SemiRooms(Mock(), self.db).sync_lobby_locks(guild)
        self.assertTrue(state[0].connect)
        self.assertFalse(state[0].speak)
        self.assertEqual(await self.db.list_lobby_locks(1), [])

    async def test_temporary_handler_ignores_semi_lobby(self):
        await self.db.update_lobby_setting(1, 2, "RoomMode", "semi")
        guild = Mock(id=1, create_voice_channel=AsyncMock())
        await VoiceChannels(Mock(), self.db).create_room(Mock(id=10, guild=guild), Mock(id=2))
        guild.create_voice_channel.assert_not_awaited()

    async def test_semi_creation_creates_owner_only_room_without_roles(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        created = await service.create_room(member, None, "Private room")
        self.assertIs(created, channel)
        guild.create_role.assert_not_awaited()
        overwrites = guild.create_voice_channel.await_args.kwargs["overwrites"]
        self.assertEqual(set(overwrites), {guild.default_role, guild.me, member})
        self.assertFalse(overwrites[guild.default_role].connect)
        row = await self.db.get_semi_room(1, 21)
        self.assertEqual(row["hidden"], 1)
        self.assertEqual(row["created_role"], 0)
        self.assertIsNone(row["role_id"])
        channel.send.assert_awaited_once()
        welcome = channel.send.await_args.kwargs
        self.assertTrue(welcome["view"].is_persistent())
        self.assertIn("5 minutes empty", welcome["embed"].description)
        self.assertEqual(welcome["allowed_mentions"].users, [member])
        service.set_visibility.assert_awaited_once_with(channel, 10, hidden=True)

    async def test_welcome_failure_does_not_undo_private_room(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        channel.send.side_effect = discord_error(discord.Forbidden)
        with self.assertLogs("semi_rooms", level="WARNING"):
            await service.create_room(member, None, "Private room")
        self.assertIsNotNone(await self.db.get_semi_room(1, 21))
        channel.delete.assert_not_awaited()
        role.delete.assert_not_awaited()

    async def test_legacy_room_role_migration_preserves_rows(self):
        await self.db.set_semi_room(1, 21, 10, 30, True)
        with closing(sqlite3.connect(self.db.path)) as conn, conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='semi_rooms'").fetchone()[0]
            rows = conn.execute("SELECT * FROM semi_rooms").fetchall()
            conn.execute("DROP TABLE semi_rooms")
            conn.execute(sql.replace("role_id INTEGER,", "role_id INTEGER NOT NULL,"))
            conn.executemany("INSERT INTO semi_rooms VALUES (?,?,?,?,?,?,?)", rows)
        await self.db.init()
        row = await self.db.get_semi_room(1, 21)
        self.assertEqual(row["role_id"], 30)
        await self.db.set_semi_room(1, 22, 11)
        self.assertIsNone((await self.db.get_semi_room(1, 22))["role_id"])
        self.assertTrue(list(Path(self.temp.name).glob("*.backup-*.db")))

    async def test_confirmed_deletion_clears_saved_preset_after_event(self):
        await self.db.set_settings_user(1, 2, 10, {"channel_name": "old saved name"})
        channel, member, _, _, _ = permission_fixture()
        async def native_event(**kwargs):
            await self.db.delete_voice_channel(1, 2, 20)
        channel.delete = AsyncMock(side_effect=native_event)
        guild = channel.guild
        guild.fetch_member = AsyncMock(return_value=member)
        guild.get_channel.return_value = channel
        bot = Mock(db=self.db)
        bot.get_guild.return_value = guild
        view = DeleteRoomView(RoomCommands(bot), 10, 1, 20)
        interaction = Mock(user=member, response=Mock(defer=AsyncMock()), followup=Mock(send=AsyncMock()))
        await view.confirm.callback(interaction)
        channel.delete.assert_awaited_once()
        self.assertIsNone(await self.db.get_voice_channel(1, 20))
        self.assertIsNone(await self.db.get_settings_user(1, 2, 10))
        self.assertTrue(view.used)


class SemiRoomLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel, self.owner, _, _, _ = permission_fixture()
        self.channel.voice_states = {}
        self.channel.edit = AsyncMock()
        self.channel.delete = AsyncMock()
        self.channel.guild.get_channel.return_value = self.channel
        self.bot = Mock()
        self.bot.get_guild.return_value = self.channel.guild
        self.db = Mock()
        self.db.list_room_openers = AsyncMock(return_value=set())
        self.db.replace_room_openers = AsyncMock()
        self.row = dict(guild_id=1, channel_id=20, owner_id=10, role_id=2, hidden=0, expires_at=None)
        self.db.get_semi_room = AsyncMock(side_effect=lambda *args: dict(self.row))
        self.db.update_semi_state = AsyncMock()
        self.db.delete_semi_room = AsyncMock()
        self.service = SemiRooms(self.bot, self.db)

    async def test_empty_room_gets_timer_not_deleted(self):
        await self.service.reconcile_room(self.row)
        values = self.db.update_semi_state.await_args.kwargs
        self.assertAlmostEqual(values["expires_at"], int(time.time()) + 300, delta=2)
        self.assertFalse(values["hidden"])
        self.channel.delete.assert_not_awaited()

    async def test_expired_room_hides_and_preserves_connect(self):
        self.row["expires_at"] = int(time.time()) - 1
        await self.service.reconcile_room(self.row)
        overwrites = self.channel.edit.await_args.kwargs["overwrites"]
        self.assertFalse(overwrites[self.channel.guild.default_role].view_channel)
        self.assertFalse(overwrites[self.channel.guild.default_role].connect)
        self.db.update_semi_state.assert_awaited_once_with(20, hidden=True, expires_at=None)
        self.channel.delete.assert_not_awaited()

    async def test_join_reveals_without_granting_connect(self):
        self.row["hidden"] = 1
        self.channel.voice_states = {10: object()}
        await self.service.reconcile_room(self.row)
        overwrites = self.channel.edit.await_args.kwargs["overwrites"]
        self.assertTrue(overwrites[self.channel.guild.default_role].view_channel)
        self.assertFalse(overwrites[self.channel.guild.default_role].connect)
        self.db.update_semi_state.assert_awaited_once_with(20, hidden=False, expires_at=None)

    async def test_visibility_failure_keeps_retryable_state(self):
        self.row["expires_at"] = 1
        self.channel.edit.side_effect = discord_error(discord.Forbidden)
        with self.assertRaises(discord.Forbidden):
            await self.service.reconcile_room(self.row)
        self.db.update_semi_state.assert_not_awaited()
        self.db.delete_semi_room.assert_not_awaited()

    async def test_old_deadline_is_shortened_to_five_minutes(self):
        self.row["expires_at"] = int(time.time()) + 850
        await self.service.reconcile_room(self.row)
        self.assertAlmostEqual(self.db.update_semi_state.await_args.kwargs["expires_at"], int(time.time()) + 300, delta=2)

    async def test_native_room_delete_releases_lobby_lock(self):
        self.service.sync_lobby_locks = AsyncMock()
        await self.service.on_guild_channel_delete(self.channel)
        self.db.delete_semi_room.assert_awaited_once_with(1, 20)
        self.service.sync_lobby_locks.assert_awaited_once_with(self.channel.guild)

    async def test_missing_semi_room_releases_slot(self):
        self.channel.guild.get_channel.return_value = None
        self.channel.guild.fetch_channel = AsyncMock(side_effect=discord_error())
        await self.service.reconcile_room(self.row)
        self.db.delete_semi_room.assert_awaited_once_with(1, 20)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def fix_fixture(self, *, owns=True, has_permission=False):
        channel, owner, _, _, _ = permission_fixture()
        channel.overwrites[owner].manage_channels = has_permission
        channel.set_permissions = AsyncMock()
        channel.guild.fetch_channel = AsyncMock(return_value=channel)
        row = {"owner_id": owner.id if owns else 9999, "channel_id": channel.id}
        db = Mock(list_semi_rooms=AsyncMock(return_value=[row]), get_semi_room=AsyncMock(return_value=row),
                  get_member_voice_channels=AsyncMock(return_value=[]))
        bot = Mock(db=db)
        bot.get_cog.return_value = SemiRooms(bot, db)
        ctx = Mock(author=owner, guild=channel.guild, reply=AsyncMock())
        return RoomCommands(bot), ctx, channel

    async def test_fix_restores_only_owner_manage_permission(self):
        cog, ctx, channel = self.fix_fixture()
        original = dict(channel.overwrites[ctx.author])
        await cog.fix.callback(cog, ctx)
        channel.set_permissions.assert_awaited_once()
        updated = dict(channel.set_permissions.await_args.kwargs["overwrite"])
        expected = {**original, "manage_channels": True}
        self.assertEqual(updated, expected)
        self.assertIn("restored", ctx.reply.await_args.args[0])

    async def test_fix_existing_permission_is_noop(self):
        cog, ctx, channel = self.fix_fixture(has_permission=True)
        await cog.fix.callback(cog, ctx)
        channel.set_permissions.assert_not_awaited()
        self.assertIn("already have", ctx.reply.await_args.args[0])

    async def test_fix_both_room_types_asks_before_changing(self):
        cog, ctx, channel = self.fix_fixture()
        cog.bot.db.get_member_voice_channels.return_value = [{"owner_id": ctx.author.id, "channel_id": 21}]
        await cog.fix.callback(cog, ctx)
        text = ctx.reply.await_args.args[0]
        self.assertIn("**Private**: <#20>", text)
        self.assertIn("**Temporary**: <#21>", text)
        channel.set_permissions.assert_not_awaited()
        ctx.guild.fetch_channel.assert_not_awaited()

    async def test_fix_admin_without_channel_only_considers_own_rooms(self):
        cog, ctx, channel = self.fix_fixture()
        ctx.author.guild_permissions.administrator = True
        cog.bot.db.list_semi_rooms.return_value = [{"owner_id": 12345, "channel_id": 99}]
        await cog.fix.callback(cog, ctx)
        self.assertIn("don't own a room", ctx.reply.await_args.args[0])
        ctx.guild.fetch_channel.assert_not_awaited()

    async def test_fix_only_temporary_room_is_automatic(self):
        cog, ctx, channel = self.fix_fixture()
        row = {"owner_id": ctx.author.id, "channel_id": channel.id}
        cog.bot.db.list_semi_rooms.return_value = []
        cog.bot.db.get_semi_room.return_value = None
        cog.bot.db.get_member_voice_channels.return_value = [row]
        cog.bot.db.get_voice_channel = AsyncMock(return_value=row)
        await cog.fix.callback(cog, ctx)
        channel.set_permissions.assert_awaited_once()

    async def test_fix_rejects_non_owner_even_with_explicit_channel(self):
        cog, ctx, channel = self.fix_fixture(owns=False)
        await cog.fix.callback(cog, ctx, channel)
        channel.set_permissions.assert_not_awaited()
        ctx.guild.fetch_channel.assert_not_awaited()
        self.assertIn("Only the recorded owner", ctx.reply.await_args.args[0])

    async def test_fix_reports_deleted_room(self):
        cog, ctx, channel = self.fix_fixture()
        ctx.guild.fetch_channel.side_effect = discord_error()
        await cog.fix.callback(cog, ctx)
        channel.set_permissions.assert_not_awaited()
        self.assertIn("deleted", ctx.reply.await_args.args[0])

    async def test_lobby_command_order_and_everyone_converter(self):
        from utility_bot import set_lobby, AccessRoleConverter
        self.assertEqual(list(set_lobby.clean_params), ["mode", "channel", "role"])
        ctx = Mock()
        converter = AccessRoleConverter()
        self.assertIs(await converter.convert(ctx, "everyone"), ctx.guild.default_role)
        self.assertIs(await converter.convert(ctx, "@everyone"), ctx.guild.default_role)
        ctx.bot.db.get_lobby = AsyncMock(return_value={})
        ctx.bot.db.update_lobby_setting = AsyncMock()
        ctx.bot.get_cog.return_value.sync_lobby_locks = AsyncMock()
        ctx.reply = AsyncMock()
        channel = Mock(id=20)
        with patch("utility_bot.apply_lobby_gate", new=AsyncMock()):
            await set_lobby.callback(ctx, "temp", channel, ctx.guild.default_role)
        ctx.bot.db.update_lobby_setting.assert_any_await(ctx.guild.id, 20, "RoomMode", "temporary")
        ctx.guild.default_role.is_default.return_value = True
        with patch("utility_bot.apply_lobby_gate", new=AsyncMock()):
            await set_lobby.callback(ctx, "semi", channel, ctx.guild.default_role)
        ctx.bot.db.update_lobby_setting.assert_any_await(ctx.guild.id, 20, "GateRoleID", ctx.guild.default_role.id)

    async def test_setlobby_without_role_preserves_existing_gate_without_creating_roles(self):
        from utility_bot import set_lobby
        role = Mock(id=99)
        role.is_default.return_value = False
        ctx = Mock(reply=AsyncMock())
        ctx.guild.get_role.return_value = role
        ctx.guild.create_role = AsyncMock()
        ctx.bot.db.get_lobby = AsyncMock(return_value={"settings_json": {"GateRoleID": 99}})
        ctx.bot.db.update_lobby_setting = AsyncMock()
        ctx.bot.get_cog.return_value.sync_lobby_locks = AsyncMock()
        with patch("utility_bot.apply_lobby_gate", new=AsyncMock()):
            await set_lobby.callback(ctx, "semi", Mock(id=20))
        ctx.bot.db.update_lobby_setting.assert_any_await(ctx.guild.id, 20, "GateRoleID", 99)
        ctx.guild.create_role.assert_not_awaited()

    async def test_semi_parser_treats_whole_input_as_name(self):
        cog = RoomCommands(Mock())
        command = cog.semi
        ctx = Mock(command=command, guild=Mock(roles=[]), message=Mock(attachments=[]), interaction=None)
        ctx.view = StringView("My private room")
        await command._parse_arguments(ctx)
        self.assertEqual(ctx.kwargs["name"], "My private room")

    async def test_sql_command_and_modal_deny_non_owner(self):
        bot = Mock(is_owner=AsyncMock(return_value=False))
        cog = SQLConsole(bot)
        ctx = Mock(bot=bot)
        with self.assertRaises(commands.NotOwner):
            await cog.sql.checks[0](ctx)
        interaction = Mock(user=Mock(id=1), response=Mock(send_message=AsyncMock()))
        self.assertFalse(await SQLView(cog, 1).interaction_check(interaction))
        await SQLModal(cog).on_submit(interaction)
        self.assertEqual(interaction.response.send_message.await_count, 2)

    async def test_deletion_timeout_keeps_room(self):
        cog = Mock(bot=Mock())
        view = DeleteRoomView(cog, 1, 2, 3)
        view.message = Mock(edit=AsyncMock())
        await view.on_timeout()
        cog.managed_room.assert_not_called()
        self.assertTrue(view.used)
        self.assertTrue(all(item.disabled for item in view.children))

    async def test_manager_grant_and_hidden_visibility_are_one_edit(self):
        channel, owner, manager, guest, _ = permission_fixture()
        channel.edit = AsyncMock()
        channel.guild.fetch_channel = AsyncMock(return_value=channel)
        service = SemiRooms(Mock(), Mock())
        bot = Mock()
        bot.db.list_room_openers = AsyncMock(return_value=set())
        bot.db.replace_room_openers = AsyncMock()
        bot.get_cog.return_value = service
        cog = RoomCommands(bot)
        cog.managed_room = AsyncMock(return_value=("private", {"owner_id": owner.id, "hidden": 1}))
        ctx = Mock(author=owner, guild=channel.guild, reply=AsyncMock())
        await cog.update_members(ctx, channel, [guest], management=True, enabled=True)
        channel.edit.assert_awaited_once()
        overwrite = channel.edit.await_args.kwargs["overwrites"][guest]
        self.assertTrue(overwrite.manage_channels)
        self.assertTrue(overwrite.connect)
        self.assertTrue(overwrite.view_channel)
        channel.edit.reset_mock()
        await cog.update_members(ctx, channel, [guest], management=True, enabled=False)
        overwrite = channel.edit.await_args.kwargs["overwrites"][guest]
        self.assertFalse(overwrite.manage_channels)
        self.assertTrue(overwrite.view_channel)
        self.assertTrue(overwrite.connect)

    async def test_batch_access_does_not_grant_management_or_expose_hidden_room(self):
        channel, owner, manager, guest, _ = permission_fixture()
        channel.guild.fetch_channel = AsyncMock(return_value=channel)
        channel.edit = AsyncMock()
        bot = Mock()
        bot.db.list_room_openers = AsyncMock(return_value=set())
        bot.db.replace_room_openers = AsyncMock()
        bot.get_cog.return_value = SemiRooms(bot, Mock())
        cog = RoomCommands(bot)
        cog.managed_room = AsyncMock(return_value=("private", {"owner_id": owner.id, "hidden": 1}))
        ctx = Mock(guild=channel.guild, author=owner, reply=AsyncMock())
        another = Mock(spec=discord.Member, id=40, roles=[], guild_permissions=discord.Permissions.none())
        channel.guild.members.append(another)
        await cog.update_members(ctx, channel, [guest, another], management=False, enabled=True)
        channel.edit.assert_awaited_once()
        values = channel.edit.await_args.kwargs["overwrites"]
        for member in (guest, another):
            self.assertTrue(values[member].connect)
            self.assertIsNone(values[member].manage_channels)
            self.assertFalse(values[member].view_channel)
        self.assertFalse(values[guest].speak)

    async def test_batch_manager_owner_protection_and_authorization(self):
        channel, owner, manager, guest, _ = permission_fixture()
        channel.guild.fetch_channel = AsyncMock(return_value=channel)
        channel.edit = AsyncMock()
        bot = Mock()
        bot.db.list_room_openers = AsyncMock(return_value=set())
        bot.db.replace_room_openers = AsyncMock()
        bot.get_cog.return_value = SemiRooms(bot, Mock())
        cog = RoomCommands(bot)
        cog.managed_room = AsyncMock(return_value=("private", {"owner_id": owner.id, "hidden": 1}))
        ctx = Mock(guild=channel.guild, author=owner, reply=AsyncMock())
        await cog.update_members(ctx, channel, [manager, guest], management=True, enabled=True)
        values = channel.edit.await_args.kwargs["overwrites"]
        for member in (manager, guest):
            self.assertTrue(values[member].manage_channels)
            self.assertTrue(values[member].connect)
            self.assertTrue(values[member].view_channel)
        channel.edit.reset_mock()
        with self.assertRaises(commands.BadArgument):
            await cog.update_members(ctx, channel, [owner, guest], management=False, enabled=False)
        channel.edit.assert_not_awaited()
        cog.managed_room.side_effect = commands.CheckFailure("denied")
        with self.assertRaises(commands.CheckFailure):
            await cog.update_members(ctx, channel, [guest], management=False, enabled=True)
        channel.edit.assert_not_awaited()

    async def test_user_remove_revokes_manager_and_disconnects(self):
        channel, owner, manager, guest, _ = permission_fixture()
        channel.guild.fetch_channel = AsyncMock(return_value=channel)
        channel.edit = AsyncMock()
        manager.voice = SimpleNamespace(channel=channel)
        manager.move_to = AsyncMock()
        channel.overwrites[manager] = discord.PermissionOverwrite(manage_channels=True, manage_permissions=True, connect=True)
        bot = Mock()
        bot.db.list_room_openers = AsyncMock(return_value=set())
        bot.db.replace_room_openers = AsyncMock()
        bot.get_cog.return_value = SemiRooms(bot, Mock())
        cog = RoomCommands(bot)
        cog.managed_room = AsyncMock(return_value=("private", {"owner_id": owner.id, "hidden": 1}))
        ctx = Mock(guild=channel.guild, author=owner, reply=AsyncMock())
        await cog.update_members(ctx, channel, [manager], management=False, enabled=False)
        permissions = channel.edit.await_args.kwargs["overwrites"][manager]
        self.assertFalse(permissions.connect)
        self.assertFalse(permissions.manage_channels)
        self.assertFalse(permissions.manage_permissions)
        self.assertFalse(permissions.view_channel)
        manager.move_to.assert_awaited_once()
        self.assertIsNone(manager.move_to.await_args.args[0])

    async def test_member_list_deduplicates_and_rejects_unknown_member(self):
        cog = RoomCommands(Mock())
        member = Mock(id=10)
        with patch.object(commands.MemberConverter, "convert", new=AsyncMock(return_value=member)):
            self.assertEqual(await cog.parse_members(Mock(), "<@10>, <@10>"), [member])
        with patch.object(commands.MemberConverter, "convert", new=AsyncMock(side_effect=commands.BadArgument("unknown"))):
            with self.assertRaises(commands.BadArgument):
                await cog.parse_members(Mock(), "unknown")

    async def test_cancel_keeps_room_and_stops_confirmation(self):
        cog = Mock()
        view = DeleteRoomView(cog, 1, 2, 3)
        interaction = Mock(response=Mock(send_message=AsyncMock()))
        await view.cancel.callback(interaction)
        cog.managed_room.assert_not_called()
        self.assertTrue(view.used)
        self.assertTrue(view.is_finished())

    async def test_deletion_rechecks_permissions_at_confirmation(self):
        cog = RoomCommands(Mock())
        guild = Mock(fetch_member=AsyncMock(return_value=Mock()))
        guild.get_channel.return_value = Mock()
        cog.bot.get_guild.return_value = guild
        cog.managed_room = AsyncMock(side_effect=commands.CheckFailure("Permission revoked."))
        view = DeleteRoomView(cog, 1, 2, 3)
        interaction = Mock(user=Mock(id=1), response=Mock(defer=AsyncMock()), followup=Mock(send=AsyncMock()))
        await view.confirm.callback(interaction)
        guild.get_channel.return_value.delete.assert_not_called()
        self.assertIn("Permission revoked", interaction.followup.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
