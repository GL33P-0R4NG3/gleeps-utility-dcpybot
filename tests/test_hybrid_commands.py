from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import discord
from discord.ext import commands
from utility_bot import UtilityBot, set_lobby


class HybridCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = UtilityBot(Mock(init=AsyncMock()))
        self.bot.tree.sync = AsyncMock(return_value=[])
        await self.bot.__aenter__()
        self.addAsyncCleanup(self.bot.close)
        await self.bot.setup_hook()

    async def test_tree_contains_every_custom_command(self):
        payloads = {cmd.name: cmd.to_dict(self.bot.tree) for cmd in self.bot.tree.get_commands()}
        self.assertEqual(set(payloads), {"room", "sql", "setlobby"})
        self.assertEqual({option["name"] for option in payloads["room"]["options"]},
                         {"help", "semi", "fix", "manager", "delete"})
        options = {option["name"]: option for option in payloads["setlobby"]["options"]}
        self.assertEqual(options["role"]["type"], discord.AppCommandOptionType.role.value)
        self.assertEqual(options["channel"]["type"], discord.AppCommandOptionType.channel.value)
        self.assertEqual({choice["value"] for choice in options["mode"]["choices"]}, {"temp", "semi"})
        for name in ("setlobby", "sql", "room fix", "room semi", "room manager", "room delete"):
            self.assertIsInstance(self.bot.get_command(name), commands.HybridCommand)
        self.bot.tree.sync.assert_awaited_once()

    async def test_slash_sql_rejects_non_owner(self):
        self.bot.is_owner = AsyncMock(return_value=False)
        ctx = Mock(bot=self.bot, author=Mock())
        interaction = Mock(client=self.bot, _baton=ctx)
        with self.assertRaises(commands.NotOwner):
            await self.bot.tree.get_command("sql")._check_can_run(interaction)
        self.bot.is_owner.return_value = True
        self.assertTrue(await self.bot.tree.get_command("sql")._check_can_run(interaction))

    async def test_slash_setlobby_keeps_manage_server_check(self):
        permissions = discord.Permissions.none()
        ctx = Mock(bot=self.bot, author=Mock(guild_permissions=permissions))
        interaction = Mock(client=self.bot, _baton=ctx)
        with self.assertRaises(commands.MissingPermissions):
            await self.bot.tree.get_command("setlobby")._check_can_run(interaction)
        permissions.manage_guild = True
        self.assertTrue(await self.bot.tree.get_command("setlobby")._check_can_run(interaction))

    async def test_defer_only_applies_to_slash_commands(self):
        ctx = Mock(interaction=object(), defer=AsyncMock())
        await self.bot.defer_slash_response(ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        ctx.interaction = None
        ctx.defer.reset_mock()
        await self.bot.defer_slash_response(ctx)
        ctx.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
