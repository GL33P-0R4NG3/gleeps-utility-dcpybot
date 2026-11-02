from pathlib import Path
import tempfile
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import discord
from data.db_helper import DBHelper
from room_commands import RoomCommands
from semi_rooms import SemiRooms
from room_help import RoomHelpView
from test_room_features import permission_fixture, discord_error


class OpenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DBHelper(str(Path(self.temp.name) / "rooms.db"))
        await self.db.init()
        await self.db.get_settings_guild(1)
        await self.db.set_semi_room(1, 20, 10)
        self.channel, self.owner, self.manager, self.guest, _ = permission_fixture()
        self.guest.voice = None
        self.channel.voice_states = {}
        self.channel.guild.fetch_channel = AsyncMock(return_value=self.channel)
        self.channel.guild.get_channel.return_value = self.channel
        async def edit(**kwargs):
            self.channel.overwrites = kwargs["overwrites"]
        self.channel.edit = AsyncMock(side_effect=edit)
        self.bot = Mock(db=self.db)
        self.bot.get_guild.return_value = self.channel.guild
        self.service = SemiRooms(self.bot, self.db)
        self.bot.get_cog.return_value = self.service
        self.cog = RoomCommands(self.bot)
        self.ctx = Mock(author=self.owner, guild=self.channel.guild, reply=AsyncMock())

    async def change(self, members=None, **kwargs):
        await self.cog.update_members(self.ctx, self.channel, members or [self.guest], **kwargs)

    async def test_opener_survives_restart_without_management_or_extra_overwrites(self):
        original = set(self.channel.overwrites)
        await self.change(opener=True, enabled=True)
        flags = self.channel.overwrites[self.guest]
        self.assertTrue(flags.connect)
        self.assertTrue(flags.view_channel)
        self.assertIsNone(flags.manage_channels)
        self.assertIsNone(flags.manage_permissions)
        self.assertEqual(await self.db.list_room_openers(1, 20), {self.guest.id})
        restarted = SemiRooms(self.bot, DBHelper(self.db.path))
        await restarted.set_visibility(self.channel, self.owner.id, hidden=True)
        self.assertTrue(self.channel.overwrites[self.guest].view_channel)
        self.assertEqual(set(self.channel.overwrites), original)

    async def test_full_demotion_chain(self):
        await self.change(management=True, enabled=True)
        self.assertTrue(self.channel.overwrites[self.guest].manage_channels)
        await self.change(management=True, enabled=False)
        self.assertFalse(self.channel.overwrites[self.guest].manage_channels)
        self.assertTrue(self.channel.overwrites[self.guest].view_channel)
        self.assertIn(self.guest.id, await self.db.list_room_openers(1, 20))
        await self.change(opener=True, enabled=False)
        self.assertFalse(self.channel.overwrites[self.guest].view_channel)
        self.assertTrue(self.channel.overwrites[self.guest].connect)
        self.assertEqual(await self.db.list_room_openers(1, 20), set())
        await self.change(enabled=False)
        self.assertFalse(self.channel.overwrites[self.guest].connect)

    async def test_opener_remove_skips_managers_but_processes_eligible_members(self):
        await self.change(opener=True, enabled=True)
        self.channel.edit.reset_mock()
        await self.change([self.manager, self.guest], opener=True, enabled=False)
        self.channel.edit.assert_awaited_once()
        self.assertEqual(await self.db.list_room_openers(1, 20), set())
        self.assertIn("Remove their manager permission first", self.ctx.reply.await_args.args[0])
        self.assertIn(f"<@{self.manager.id}>", self.ctx.reply.await_args.args[0])
        self.channel.edit.reset_mock()
        await self.change([self.manager], opener=True, enabled=False)
        self.channel.edit.assert_not_awaited()

    async def test_user_remove_clears_opener_and_manager(self):
        await self.change(management=True, enabled=True)
        await self.change(enabled=False)
        self.assertEqual(await self.db.list_room_openers(1, 20), set())
        flags = self.channel.overwrites[self.guest]
        self.assertFalse(flags.connect)
        self.assertFalse(flags.manage_channels)
        self.assertFalse(flags.view_channel)

    async def test_discord_failure_restores_opener_records(self):
        self.channel.edit.side_effect = discord_error(discord.Forbidden)
        with self.assertRaises(discord.Forbidden):
            await self.change(opener=True, enabled=True)
        self.assertEqual(await self.db.list_room_openers(1, 20), set())

    async def test_room_deletion_clears_opener_records(self):
        await self.change(opener=True, enabled=True)
        await self.db.delete_semi_room(1, 20)
        self.assertEqual(await self.db.list_room_openers(1, 20), set())

    async def test_guest_cannot_grant_opener_access(self):
        self.ctx.author = self.guest
        from discord.ext import commands
        with self.assertRaises(commands.CheckFailure):
            await self.change(opener=True, enabled=True)
        self.channel.edit.assert_not_awaited()
        self.assertEqual(await self.db.list_room_openers(1, 20), set())

    async def test_setup_help_fits_discord_message_limit(self):
        view = RoomHelpView()
        interaction = Mock(response=Mock(send_message=AsyncMock()))
        await view.help.callback(interaction)
        text = interaction.response.send_message.await_args.args[0]
        self.assertLessEqual(len(text), 2000)
        self.assertIn("/room opener add", text)

    async def test_opener_cannot_manage_room(self):
        await self.change(opener=True, enabled=True)
        self.ctx.author = self.guest
        self.channel.edit.reset_mock()
        from discord.ext import commands
        with self.assertRaises(commands.CheckFailure):
            await self.change(management=True, enabled=True)
        self.channel.edit.assert_not_awaited()

    async def test_server_admin_can_manage_hidden_room(self):
        self.ctx.author = self.guest
        self.guest.guild_permissions.administrator = True
        await self.change([self.manager], management=False, enabled=True)
        self.channel.edit.assert_awaited_once()
        mode, row = await self.cog.managed_room(self.channel, self.guest)
        self.assertEqual(row["owner_id"], self.owner.id)

    async def test_admin_fix_repairs_owner_not_admin(self):
        self.ctx.author = self.guest
        self.guest.guild_permissions.administrator = True
        self.channel.overwrites[self.owner].manage_channels = False
        self.channel.guild.fetch_member = AsyncMock(return_value=self.owner)
        self.channel.set_permissions = AsyncMock()
        await self.cog.fix.callback(self.cog, self.ctx, self.channel)
        self.channel.guild.fetch_member.assert_awaited_once_with(self.owner.id)
        self.channel.set_permissions.assert_awaited_once()
        self.assertIs(self.channel.set_permissions.await_args.args[0], self.owner)
        self.assertTrue(self.channel.set_permissions.await_args.kwargs["overwrite"].manage_channels)

    async def test_admin_fix_handles_departed_owner(self):
        self.ctx.author = self.guest
        self.guest.guild_permissions.administrator = True
        self.channel.guild.fetch_member = AsyncMock(side_effect=discord_error())
        self.channel.set_permissions = AsyncMock()
        await self.cog.fix.callback(self.cog, self.ctx, self.channel)
        self.channel.set_permissions.assert_not_awaited()
        self.assertIn("left this server", self.ctx.reply.await_args.args[0])

    async def test_role_opener_reveals_existing_member_overwrite_after_restart(self):
        role = self.guest.roles[1]
        await self.change([role], opener=True, enabled=True)
        restarted = SemiRooms(self.bot, DBHelper(self.db.path))
        await restarted.set_visibility(self.channel, self.owner.id, hidden=True)
        self.assertTrue(self.channel.overwrites[role].view_channel)
        self.assertTrue(self.channel.overwrites[self.guest].view_channel)
        self.assertIsNone(self.channel.overwrites[role].manage_channels)

    async def test_role_removal_keeps_personal_access_and_does_not_disconnect(self):
        role = self.guest.roles[1]
        role.members = [self.guest]
        await self.change(opener=True, enabled=True)
        await self.change([role], management=True, enabled=True)
        self.guest.voice = Mock(channel=self.channel)
        self.guest.move_to = AsyncMock()
        self.channel.permissions_for.return_value = discord.Permissions(connect=True)
        await self.change([role], enabled=False)
        self.assertFalse(self.channel.overwrites[role].connect)
        self.assertTrue(self.channel.overwrites[self.guest].connect)
        self.assertTrue(self.channel.overwrites[self.guest].view_channel)
        self.assertEqual(await self.db.list_room_openers(1, 20), {self.guest.id})
        self.guest.move_to.assert_not_awaited()

    async def test_role_manager_demotion_chain(self):
        role = self.guest.roles[1]
        await self.change([role], management=True, enabled=True)
        await self.change([role], opener=True, enabled=False)
        self.assertTrue(self.channel.overwrites[role].manage_channels)
        self.assertIn("Remove their manager permission first", self.ctx.reply.await_args.args[0])
        await self.change([role], management=True, enabled=False)
        self.assertIn(role.id, await self.db.list_room_openers(1, 20))
        await self.change([role], opener=True, enabled=False)
        self.assertTrue(self.channel.overwrites[role].connect)
        self.assertFalse(self.channel.overwrites[role].view_channel)

    async def test_mixed_member_role_mentions_deduplicated(self):
        from unittest.mock import patch
        role = self.guest.roles[1]
        with patch("room_commands.commands.MemberConverter.convert", new=AsyncMock(return_value=self.guest)), \
             patch("room_commands.commands.RoleConverter.convert", new=AsyncMock(return_value=role)):
            result = await self.cog.parse_members(self.ctx, "<@12>, <@&2> <@12>")
        self.assertEqual(result, [self.guest, role])


if __name__ == "__main__":
    unittest.main()
