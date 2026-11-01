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


def visibility_overwrites(channel, owner_id, hidden, *, source_overwrites=None, opener_ids=()):
    """Change only View Channel; Connect and all other flags are preserved."""
    source = channel.overwrites if source_overwrites is None else source_overwrites
    overwrites = {target: discord.PermissionOverwrite.from_pair(*value.pair())
                  for target, value in source.items()}
    for target, overwrite in overwrites.items():
        visible = not hidden
        if hidden:
            if target.id == owner_id or target.id in opener_ids or (channel.guild.me is not None and target.id == channel.guild.me.id):
                visible = True
            elif isinstance(target, discord.Member):
                visible = can_manage_room(channel, target, owner_id, overwrites=source)
            elif isinstance(target, discord.Role):
                # Resolve only this existing role's management grant. Never expand
                # role membership into new per-member overwrites.
                visible = target.permissions.manage_channels
                everyone = source.get(channel.guild.default_role, discord.PermissionOverwrite()).manage_channels
                if everyone is not None:
                    visible = everyone
                if overwrite.manage_channels is not None:
                    visible = overwrite.manage_channels
            else:
                # Uncached targets still retain an explicit management grant.
                visible = overwrite.manage_channels is True
        overwrite.view_channel = visible
    return overwrites
