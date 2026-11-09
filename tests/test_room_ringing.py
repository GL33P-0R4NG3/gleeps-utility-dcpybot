from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import discord
from discord.ext import commands

from data.db_helper import DBHelper
from room_ringing import RoomRinging, RingRequest, IgnoreReasonModal, DenyConfirmation, LOBBY_PERMISSIONS
from semi_rooms import SemiRooms
from test_room_features import permission_fixture, discord_error


class RingingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DBHelper(str(Path(self.temp.name) / "rooms.db"))
        await self.db.init()
        await self.db.get_settings_guild(1)
        await self.db.set_lobby(1, 30, {"RoomMode": "semi", "RingLobbyID": 40})
        await self.db.set_semi_room(1, 20, 10)
        await self.db.update_semi_state(20, hidden=False, expires_at=None)
        self.room, self.owner, self.manager, self.guest, self.role = permission_fixture()
        self.guild = self.room.guild
        self.guild.unavailable = False
        self.room.category_id = 50
        self.room.user_limit = 0
        self.owner.bot = False
        self.room.members = [self.owner]
        self.room.send = AsyncMock(return_value=Mock(id=200, edit=AsyncMock(), delete=AsyncMock()))
        self.room.edit = AsyncMock()
        async def set_permissions(target, *, overwrite, reason):
            self.room.overwrites[target] = overwrite
        self.room.set_permissions = AsyncMock(side_effect=set_permissions)
        self.room.permissions_for.side_effect = lambda member: (
            discord.Permissions.all() if member.id == self.guild.me.id else discord.Permissions.none())
        self.creation = Mock(spec=discord.VoiceChannel, id=30, guild=self.guild,
                             category_id=50, category=Mock())
        self.lobby = Mock(spec=discord.VoiceChannel, id=40, guild=self.guild, category_id=50,
                          mention="<#40>", overwrites={})
        self.lobby.permissions_for.return_value = discord.Permissions.all()
        self.lobby.edit = AsyncMock()
        self.lobby.send = AsyncMock(return_value=Mock(id=201))
        self.lobby.delete = AsyncMock()
        self.guild.create_voice_channel = AsyncMock(return_value=self.lobby)
        self.channels = {20: self.room, 30: self.creation, 40: self.lobby}
        async def fetch_channel(channel_id):
            if channel_id not in self.channels:
                raise discord_error()
            return self.channels[channel_id]
        self.guild.fetch_channel = AsyncMock(side_effect=fetch_channel)
        self.people = {person.id: person for person in (self.owner, self.manager, self.guest)}
        for member in self.people.values():
            member.guild = self.guild
            member.is_timed_out.return_value = False
            member.send = AsyncMock()
            member.move_to = AsyncMock()
            member.mention = f"<@{member.id}>"
            member.bot = False
        self.guild.fetch_member = AsyncMock(side_effect=lambda member_id: self.people[member_id])
        self.guest.voice = Mock(channel=self.lobby)
        self.bot = Mock(db=self.db)
        self.bot.get_guild.return_value = self.guild
        self.semi = SemiRooms(self.bot, self.db)
        self.bot.get_cog.return_value = self.semi
        self.service = RoomRinging(self.bot)
        self.ctx = Mock(guild=self.guild, author=self.guest, reply=AsyncMock(),
                        interaction=Mock(followup=Mock(send=AsyncMock())),
                        message=Mock(guild=self.guild, author=self.guest))
        self.addAsyncCleanup(self.service.cog_unload)

    def interaction(self, member=None):
        return Mock(guild_id=1, user=member or self.manager,
                    response=Mock(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
                    followup=Mock(send=AsyncMock()))

    async def request(self):
        await self.service.ring(self.ctx, self.room)
        return self.service.pending[(1, self.guest.id, self.room.id)]

    async def test_validation_rejects_wrong_lobby_target_and_section(self):
        with self.assertRaisesRegex(commands.BadArgument, "lobby or a room-creation"):
            await self.service.validate(self.guest, 30)
        with self.assertRaisesRegex(commands.BadArgument, "lobby or a room-creation"):
            await self.service.validate(self.guest, 40)
        with self.assertRaisesRegex(commands.BadArgument, "managed by this bot"):
            await self.service.validate(self.guest, 99)
        self.room.category_id = 51
        with self.assertRaisesRegex(commands.BadArgument, "outside"):
            await self.service.validate(self.guest, 20)
        self.guest.voice = None
        with self.assertRaisesRegex(commands.BadArgument, "Join the doorbell"):
            await self.service.validate(self.guest, 20)

    async def test_default_deny_is_allowed_but_personal_and_role_denies_are_not(self):
        self.assertIs(await self.service.validate(self.guest, 20), self.room)
        for permission in ("connect", "view_channel"):
            for target in (self.guest, self.role):
                with self.subTest(permission=permission, target=target.id):
                    original = self.room.overwrites[target]
                    self.room.overwrites[target] = discord.PermissionOverwrite(**{permission: False})
                    with self.assertRaises(commands.CheckFailure):
                        await self.service.validate(self.guest, 20)
                    self.room.overwrites[target] = original

    async def test_empty_hidden_full_timeout_and_existing_access(self):
        self.room.members = []
        with self.assertRaisesRegex(commands.BadArgument, "inactive or empty"):
            await self.service.validate(self.guest, 20)
        self.room.members = [self.owner]
        await self.db.update_semi_state(20, hidden=True, expires_at=None)
        with self.assertRaisesRegex(commands.BadArgument, "inactive or empty"):
            await self.service.validate(self.guest, 20)
        await self.db.update_semi_state(20, hidden=False, expires_at=None)
        self.room.user_limit = 1
        with self.assertRaisesRegex(commands.BadArgument, "full"):
            await self.service.validate(self.guest, 20)
        self.room.user_limit = 0
        self.guest.is_timed_out.return_value = True
        with self.assertRaisesRegex(commands.CheckFailure, "timed out"):
            await self.service.validate(self.guest, 20)
        self.guest.is_timed_out.return_value = False
        self.room.permissions_for.side_effect = lambda member: discord.Permissions.all()
        with self.assertRaisesRegex(commands.BadArgument, "already have access"):
            await self.service.validate(self.guest, 20)

    async def test_accept_only_once_and_no_permission_grant(self):
        request = await self.request()
        before = {target: flags.pair() for target, flags in self.room.overwrites.items()}
        interaction = self.interaction()
        await request.decide(interaction, allow=True)
        await request.decide(self.interaction(), allow=True)
        self.guest.move_to.assert_awaited_once_with(self.room, reason=f"Doorbell accepted by {self.manager.id}")
        self.assertEqual(before, {target: flags.pair() for target, flags in self.room.overwrites.items()})
        self.assertTrue(request.done)
        self.assertFalse(self.service.pending)
        self.room.edit.assert_not_awaited()

    async def test_guest_and_opener_cannot_approve(self):
        request = await self.request()
        await self.db.replace_room_openers(1, 20, {self.guest.id})
        await request.decide(self.interaction(self.guest), allow=True)
        self.guest.move_to.assert_not_awaited()
        self.assertFalse(request.done)

    async def test_rechecks_manager_and_requester_restrictions(self):
        request = await self.request()
        self.room.overwrites[self.manager.roles[-1]].manage_channels = False
        await request.decide(self.interaction(), allow=True)
        self.guest.move_to.assert_not_awaited()
        self.room.overwrites[self.manager.roles[-1]].manage_channels = True
        self.room.overwrites[self.guest].connect = False
        await request.decide(self.interaction(), allow=True)
        self.guest.move_to.assert_not_awaited()
        self.assertTrue(request.done)

    async def test_accept_after_departure_cancels(self):
        request = await self.request()
        self.guest.voice = None
        await request.decide(self.interaction(), allow=True)
        self.guest.move_to.assert_not_awaited()
        self.assertTrue(request.done)

    async def test_ignore_reason_expiry_and_move_failure(self):
        request = await self.request()
        self.guest.move_to.side_effect = discord_error(discord.Forbidden)
        with self.assertLogs("room_ringing", level="ERROR"):
            await request.decide(self.interaction(), allow=True)
        self.assertFalse(request.done)
        await request.decide(self.interaction(), allow=False, reason="Finishing a game")
        self.assertIn("Finishing a game", self.ctx.interaction.followup.send.await_args.args[0])
        self.assertTrue(request.done)
        another = RingRequest(self.service, self.ctx, 20, 40)
        await another.on_timeout()
        self.assertTrue(another.done)

    async def test_duplicate_and_cooldown(self):
        request = await self.request()
        with self.assertRaisesRegex(commands.BadArgument, "pending"):
            await self.service.ring(self.ctx, self.room)
        await request.decide(self.interaction(), allow=False)
        with self.assertRaisesRegex(commands.BadArgument, "seconds"):
            await self.service.ring(self.ctx, self.room)

    async def test_other_server_and_missing_bot_permissions(self):
        other = Mock(guild=Mock(id=2))
        with self.assertRaisesRegex(commands.BadArgument, "this server"):
            await self.service.ring(self.ctx, other)
        self.lobby.permissions_for.return_value = discord.Permissions.none()
        with self.assertRaisesRegex(commands.BadArgument, "Move Members"):
            await self.service.validate(self.guest, 20)

    async def test_setup_reuses_lobby_and_opens_restricted_overwrites(self):
        self.lobby.overwrites = {self.guest: discord.PermissionOverwrite(connect=False, speak=False, manage_channels=True)}
        result = await self.service.configure(self.guild, self.creation)
        self.assertIs(result, self.lobby)
        self.guild.create_voice_channel.assert_not_awaited()
        overwrites = self.lobby.edit.await_args.kwargs["overwrites"]
        for name in LOBBY_PERMISSIONS:
            self.assertTrue(getattr(overwrites[self.guild.default_role], name))
        self.assertIsNone(overwrites[self.guest].connect)
        self.assertTrue(overwrites[self.guest].manage_channels)

    async def test_setup_creates_and_persists_missing_lobby(self):
        await self.db.update_lobby_setting(1, 30, "RingLobbyID", 99)
        await self.service.configure(self.guild, self.creation)
        self.guild.create_voice_channel.assert_awaited_once()
        self.assertEqual(self.guild.create_voice_channel.await_args.args[0], "🛎️ Lobby")
        row = await self.db.get_lobby(1, 30)
        self.assertEqual(row["settings_json"]["RingLobbyID"], 40)
        await self.service.configure(self.guild, self.creation)
        self.guild.create_voice_channel.assert_awaited_once()

    async def test_setup_rejects_rooms_and_creation_lobbies(self):
        for channel in (self.creation, self.room):
            with self.assertRaises(commands.BadArgument):
                await self.service.configure(self.guild, self.creation, channel)
        self.lobby.edit.assert_not_awaited()

    async def test_join_dm_and_blocked_dm(self):
        self.guest.send.side_effect = discord_error(discord.Forbidden)
        with self.assertLogs("room_ringing", level="INFO"):
            await self.service.on_voice_state_update(self.guest, Mock(channel=None), Mock(channel=self.lobby))
        self.guest.send.assert_awaited_once()
        await self.service.on_voice_state_update(self.guest, Mock(channel=None), Mock(channel=self.lobby))
        self.guest.send.assert_awaited_once()

    async def test_modal_payload_is_valid(self):
        modal = IgnoreReasonModal(RingRequest(self.service, self.ctx, 20, 40))
        payload = modal.to_dict()
        field = payload["components"][0]["components"][0]
        self.assertLessEqual(len(field["label"]), 45)
        self.assertFalse(field["required"])

    async def test_deny_cancel_does_not_change_request_or_permissions(self):
        request = await self.request()
        interaction = self.interaction()
        interaction.response.edit_message = AsyncMock()
        confirmation = DenyConfirmation(request, self.manager.id)
        await confirmation.cancel.callback(interaction)
        self.assertFalse(request.done)
        self.room.set_permissions.assert_not_awaited()

    async def test_confirmed_deny_blocks_future_rings_only_in_this_room(self):
        request = await self.request()
        original = dict(self.room.overwrites[self.guest])
        confirmation = DenyConfirmation(request, self.manager.id)
        await confirmation.confirm.callback(self.interaction())
        self.assertEqual(dict(self.room.overwrites[self.guest]), {**original, "connect": False})
        self.guest.move_to.assert_not_awaited()
        self.assertTrue(request.done)
        with self.assertRaisesRegex(commands.CheckFailure, "explicitly denies"):
            await self.service.validate(self.guest, self.room.id)
        request.message.delete.assert_awaited_once_with(delay=10)

    async def test_confirmation_cannot_deny_after_another_manager_accepts(self):
        request = await self.request()
        confirmation = DenyConfirmation(request, self.manager.id)
        await request.decide(self.interaction(self.owner), allow=True)
        await confirmation.confirm.callback(self.interaction())
        self.room.set_permissions.assert_not_awaited()
        self.guest.move_to.assert_awaited_once()

    async def test_confirmation_rechecks_manager_permissions(self):
        request = await self.request()
        confirmation = DenyConfirmation(request, self.manager.id)
        self.room.overwrites[self.manager.roles[-1]].manage_channels = False
        await confirmation.confirm.callback(self.interaction())
        self.room.set_permissions.assert_not_awaited()
        self.assertFalse(request.done)

    async def test_cooldown_is_five_minutes_per_person_per_room(self):
        from types import SimpleNamespace
        first = SimpleNamespace(guild=self.guild, author=self.guest, room_id=20)
        other_room = SimpleNamespace(guild=self.guild, author=self.guest, room_id=21)
        other_person = SimpleNamespace(guild=self.guild, author=self.manager, room_id=20)
        self.assertIsNone(self.service.cooldowns.get_bucket(first, current=1000).update_rate_limit(current=1000))
        self.assertEqual(self.service.cooldowns.get_bucket(first, current=1100).update_rate_limit(current=1100), 200)
        self.assertIsNone(self.service.cooldowns.get_bucket(other_room, current=1100).update_rate_limit(current=1100))
        self.assertIsNone(self.service.cooldowns.get_bucket(other_person, current=1100).update_rate_limit(current=1100))
        self.assertIsNone(self.service.cooldowns.get_bucket(first, current=1301).update_rate_limit(current=1301))

    async def test_expiry_ignores_and_deletes_card(self):
        request = await self.request()
        self.assertEqual(request.timeout, 60)
        await request.on_timeout()
        self.assertTrue(request.done)
        self.assertFalse(self.service.pending)
        request.message.delete.assert_awaited_once_with(delay=10)
        self.assertIn("automatically ignored", self.ctx.interaction.followup.send.await_args.args[0])

    async def test_reason_submission_rechecks_manager(self):
        request = await self.request()
        modal = IgnoreReasonModal(request)
        self.room.overwrites[self.manager.roles[-1]].manage_channels = False
        await modal.on_submit(self.interaction())
        self.assertFalse(request.done)
        self.ctx.interaction.followup.send.assert_not_awaited()

    async def test_room_empties_before_approval(self):
        request = await self.request()
        self.room.members = []
        await request.decide(self.interaction(), allow=True)
        self.guest.move_to.assert_not_awaited()
        self.assertTrue(request.done)

    async def test_request_departure_disables_buttons(self):
        request = await self.request()
        await self.service.on_voice_state_update(self.guest, Mock(channel=self.lobby), Mock(channel=None))
        self.assertTrue(request.done)
        self.assertTrue(all(item.disabled for item in request.children))
        self.guest.move_to.assert_not_awaited()

    async def test_ignored_request_restarts_cooldown_from_dismissal(self):
        from types import SimpleNamespace
        request = await self.request()
        event = SimpleNamespace(guild=self.guild, author=self.guest, room_id=20)
        bucket = self.service.cooldowns.get_bucket(event)
        bucket._window -= 290
        await request.decide(self.interaction(), allow=False)
        self.assertGreater(self.service.cooldowns.get_bucket(event).get_retry_after(), 299)
        with self.assertRaisesRegex(commands.BadArgument, "seconds"):
            await self.service.ring(self.ctx, self.room)


    async def test_cleanup_survives_restart_and_retries_discord_failure(self):
        await self.db.schedule_ring_cleanup(1, 20, 999, 0)
        message = Mock(delete=AsyncMock(side_effect=discord_error(discord.Forbidden)))
        self.room.get_partial_message.return_value = message
        restarted = RoomRinging(self.bot)
        with self.assertLogs("room_ringing", level="WARNING"):
            await restarted.cleanup_messages()
        self.assertEqual(len(await self.db.due_ring_messages(1)), 1)
        message.delete.side_effect = discord_error(discord.NotFound)
        await restarted.cleanup_messages()
        self.assertEqual(await self.db.due_ring_messages(1), [])

    async def test_effective_join_access_rejects_ring_before_lower_priority_denies(self):
        self.room.overwrites[self.role].connect = False
        self.room.permissions_for.side_effect = lambda member: discord.Permissions(view_channel=True, connect=True)
        with self.assertRaisesRegex(commands.BadArgument, "already have access.*join it directly"):
            await self.service.ring(self.ctx, self.room)
        self.room.send.assert_not_awaited()
        self.assertFalse(self.service.pending)

    async def test_admin_can_ring_for_testing_but_personal_denies_still_apply(self):
        self.guest.guild_permissions.administrator = True
        self.room.permissions_for.side_effect = lambda member: discord.Permissions.all()
        request = await self.request()
        self.assertFalse(request.done)
        self.room.overwrites[self.guest].connect = False
        await request.decide(self.interaction(), allow=True)
        self.guest.move_to.assert_not_awaited()
        self.assertTrue(request.done)


if __name__ == "__main__":
    unittest.main()
