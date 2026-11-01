"""Room-management checks independent of Discord's hidden-channel masking."""
import discord


def can_manage_room(channel, member, owner_id, *, overwrites=None):
    return member.id == owner_id or has_manage_channels(channel, member, overwrites=overwrites)


def has_manage_channels(channel, member, *, overwrites=None):
    """Check actual management permissions without the stored-owner shortcut."""
    if member.id == channel.guild.owner_id or member.guild_permissions.administrator:
        return True
    # permissions_for() strips channel permissions when View Channel is denied.
    # Resolve Manage Channels directly so hidden-room managers remain identifiable.
    if overwrites is None:
        overwrites = channel.overwrites
    def get(target):
        return overwrites.get(target, discord.PermissionOverwrite())

    allowed = member.guild_permissions.manage_channels
    everyone = get(channel.guild.default_role).manage_channels
    if everyone is not None:
        allowed = everyone
    role_values = [get(role).manage_channels for role in member.roles
                   if role.id != channel.guild.default_role.id]
    if False in role_values:
        allowed = False
    if True in role_values:
        allowed = True
    personal = get(member).manage_channels
    return allowed if personal is None else personal


def visibility_overwrites(channel, owner_id, hidden, *, source_overwrites=None):
    """Change only View Channel; Connect and all other flags are preserved."""
    source = channel.overwrites if source_overwrites is None else source_overwrites
    overwrites = {target: discord.PermissionOverwrite.from_pair(*value.pair())
                  for target, value in source.items()}
    overwrites.setdefault(channel.guild.default_role, discord.PermissionOverwrite())
    for overwrite in overwrites.values():
        overwrite.view_channel = not hidden
    if hidden:
        # Member-specific grants avoid a conflicting role's visibility exposing the
        # room to non-managers. Existing member denies cannot mask a real manager.
        for member in channel.guild.members:
            if can_manage_room(channel, member, owner_id, overwrites=source):
                overwrites.setdefault(member, discord.PermissionOverwrite()).view_channel = True
        if channel.guild.me is not None:
            overwrites.setdefault(channel.guild.me, discord.PermissionOverwrite()).view_channel = True
    return overwrites
