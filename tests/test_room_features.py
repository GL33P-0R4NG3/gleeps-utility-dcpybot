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
    everyone = Mock(spec=discord.Role, id=1)
    access = Mock(spec=discord.Role, id=2)
    manager_role = Mock(spec=discord.Role, id=3)
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
        self.assertTrue(hidden[manager].view_channel)
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

    def test_manager_resolution_ignores_visibility_but_respects_explicit_denial(self):
        channel, owner, manager, guest, access = permission_fixture()
        channel.overwrites[manager] = discord.PermissionOverwrite(view_channel=False)
        self.assertTrue(can_manage_room(channel, manager, owner.id))
        channel.overwrites[manager].manage_channels = False
        self.assertFalse(can_manage_room(channel, manager, owner.id))
        self.assertFalse(can_manage_room(channel, guest, owner.id))


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

    async def test_failed_lobby_move_rolls_back_room_and_new_role(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        lobby = Mock()
        member.voice = SimpleNamespace(channel=lobby)
        member.move_to = AsyncMock(side_effect=discord_error(discord.Forbidden))
        with self.assertRaises(discord.Forbidden):
            await service.create_room(member, None, "Private room", source_lobby=lobby)
        channel.delete.assert_awaited_once()
        role.delete.assert_awaited_once()
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

    async def test_semi_creation_creates_minimal_role_and_hidden_record(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        created, access = await service.create_room(member, None, "Private room")
        self.assertIs(created, channel)
        self.assertIs(access, role)
        self.assertEqual(guild.create_role.await_args.kwargs["permissions"].value, 0)
        row = await self.db.get_semi_room(1, 21)
        self.assertEqual(row["hidden"], 1)
        self.assertEqual(row["created_role"], 1)
        channel.send.assert_awaited_once()
        welcome = channel.send.await_args.kwargs
        self.assertTrue(welcome["view"].is_persistent())
        self.assertIn("15 minutes empty", welcome["embed"].description)
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

    async def test_everyone_access_reuses_role_and_allows_connect(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        guild.default_role = role
        role.is_default.return_value = True
        await service.create_room(member, None, "Open room", role)
        guild.create_role.assert_not_awaited()
        overwrite = guild.create_voice_channel.await_args.kwargs["overwrites"][role]
        self.assertTrue(overwrite.connect)
        self.assertFalse(overwrite.view_channel)  # Still starts hidden.
        welcome = channel.send.await_args.kwargs["embed"]
        self.assertIn("Everyone can join", welcome.fields[0].value)
        self.assertEqual((await self.db.get_semi_room(1, 21))["created_role"], 0)

    async def test_semi_creation_links_existing_role_without_modifying_it(self):
        service, guild, member, channel, role = self.semi_creation_fixture()
        await service.create_room(member, None, "Private room", role)
        guild.create_role.assert_not_awaited()
        role.edit.assert_not_called()
        self.assertEqual((await self.db.get_semi_room(1, 21))["created_role"], 0)
        overwrites = guild.create_voice_channel.await_args.kwargs["overwrites"]
        self.assertTrue(overwrites[role].connect)
        self.assertFalse(overwrites[guild.default_role].connect)

    async def test_semi_failed_channel_creation_removes_only_new_role(self):
        for linked in (False, True):
            service, guild, member, channel, role = self.semi_creation_fixture()
            guild.create_voice_channel.side_effect = discord_error(discord.Forbidden)
            with self.assertRaises(discord.Forbidden):
                await service.create_room(member, None, "Private room", role if linked else None)
            if linked:
                role.delete.assert_not_awaited()
            else:
                role.delete.assert_awaited_once()

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
        self.row = dict(guild_id=1, channel_id=20, owner_id=10, role_id=2, hidden=0, expires_at=None)
        self.db.get_semi_room = AsyncMock(side_effect=lambda *args: dict(self.row))
        self.db.update_semi_state = AsyncMock()
        self.db.delete_semi_room = AsyncMock()
        self.service = SemiRooms(self.bot, self.db)

    async def test_empty_room_gets_timer_not_deleted(self):
        await self.service.reconcile_room(self.row)
        values = self.db.update_semi_state.await_args.kwargs
        self.assertAlmostEqual(values["expires_at"], int(time.time()) + 900, delta=2)
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
        db = Mock(list_semi_rooms=AsyncMock(return_value=[row]), get_semi_room=AsyncMock(return_value=row))
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
        await set_lobby.callback(ctx, "temp", channel)
        ctx.bot.db.update_lobby_setting.assert_any_await(ctx.guild.id, 20, "RoomMode", "temporary")
        ctx.guild.default_role.is_default.return_value = True
        await set_lobby.callback(ctx, "semi", channel, ctx.guild.default_role)
        ctx.bot.db.update_lobby_setting.assert_any_await(ctx.guild.id, 20, "AccessRoleID", ctx.guild.default_role.id)

    async def test_optional_role_parser_allows_name_only(self):
        cog = RoomCommands(Mock())
        command = cog.semi
        ctx = Mock(command=command, guild=Mock(roles=[]), message=Mock(attachments=[]))
        ctx.view = StringView("My private room")
        await command._parse_arguments(ctx)
        self.assertIsNone(ctx.args[-1])
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
        bot.get_cog.return_value = service
        cog = RoomCommands(bot)
        cog.managed_room = AsyncMock(return_value=("semi-temporary", {"owner_id": owner.id, "hidden": 1}))
        ctx = Mock(author=owner, guild=channel.guild, reply=AsyncMock())
        await cog.manager.callback(cog, ctx, channel, guest, True)
        channel.edit.assert_awaited_once()
        overwrite = channel.edit.await_args.kwargs["overwrites"][guest]
        self.assertTrue(overwrite.manage_channels)
        self.assertTrue(overwrite.connect)
        self.assertTrue(overwrite.view_channel)
        channel.edit.reset_mock()
        await cog.manager.callback(cog, ctx, channel, guest, False)
        overwrite = channel.edit.await_args.kwargs["overwrites"][guest]
        self.assertFalse(overwrite.manage_channels)
        self.assertFalse(overwrite.view_channel)
        self.assertTrue(overwrite.connect)

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
