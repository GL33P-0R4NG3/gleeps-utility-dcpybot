"""Lobby membership gates, separate from permissions inside created rooms."""
import discord


def gate_role_id(settings):
    # Treat existing setlobby role settings as gates after the behavior correction.
    return settings.get("GateRoleID", settings.get("AccessRoleID"))


def may_create_room(member, settings):
    role_id = gate_role_id(settings)
    if role_id is None:
        return True  # Legacy lobbies with no configured role remain open.
    return role_id == member.guild.default_role.id or any(role.id == role_id for role in member.roles)


def lobby_overwrites(channel, role):
    """Other roles/member allows must not bypass the gate; retain unrelated flags."""
    overwrites = {target: discord.PermissionOverwrite.from_pair(*value.pair())
                  for target, value in channel.overwrites.items()}
    for target, overwrite in overwrites.items():
        if target != channel.guild.me and overwrite.connect is True:
            overwrite.connect = None
    overwrites.setdefault(channel.guild.default_role, discord.PermissionOverwrite()).connect = False
    gate = overwrites.setdefault(role, discord.PermissionOverwrite())
    gate.connect = True
    gate.view_channel = True
    return overwrites


async def apply_lobby_gate(guild, lobby_id, settings):
    role_id = gate_role_id(settings)
    if role_id is None:
        return
    role = guild.get_role(role_id)
    channel = await guild.fetch_channel(lobby_id)
    if role is None:
        # Missing gate roles must fail closed, not turn the lobby public.
        role = discord.Object(id=role_id, type=discord.Role)
    overwrites = lobby_overwrites(channel, role)
    if guild.get_role(role_id) is None:
        overwrites.pop(role, None)
    if overwrites != channel.overwrites:
        await channel.edit(overwrites=overwrites, reason="Enforce room-creation role gate")
