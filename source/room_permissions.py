"""Room-management checks independent of Discord's hidden-channel masking."""
import discord

# Channel-scoped only: never grant Administrator, Kick Members, or Ban Members.
ROOM_PERMISSIONS = {name: True for name, allowed in discord.Permissions.all_channel() if allowed}
# These also support voice-channel overwrites but are omitted by all_channel().
ROOM_PERMISSIONS.update(create_events=True, manage_events=True, set_voice_channel_status=True,
                        bypass_slowmode=True)
# These also support voice-channel overwrites but are omitted by all_channel().
ROOM_PERMISSIONS.update(create_events=True, manage_events=True, set_voice_channel_status=True,
                        bypass_slowmode=True)
ROOM_MEMBER_PERMISSIONS = (
    "send_messages", "read_message_history", "add_reactions", "embed_links",
    "attach_files", "external_emojis", "external_stickers", "send_voice_messages",
    "send_polls", "send_messages_in_threads", "speak", "stream",
    "use_voice_activation", "use_soundboard", "use_external_sounds",
    "use_application_commands", "use_embedded_activities",
)


def apply_member_defaults(overwrite):
    """Fill unspecified everyday permissions without undoing manual choices."""
    for name in ROOM_MEMBER_PERMISSIONS:
        if getattr(overwrite, name) is None:
            setattr(overwrite, name, True)


ROOM_MODERATION_PERMISSIONS = (
    "manage_channels", "manage_permissions", "manage_messages", "manage_webhooks",
    "manage_threads", "mute_members", "deafen_members", "move_members",
    "create_events", "manage_events", "set_voice_channel_status", "bypass_slowmode", "pin_messages",
    "create_events", "manage_events", "set_voice_channel_status", "bypass_slowmode", "pin_messages",
)



def can_manage_room(channel, member, owner_id, *, overwrites=None):
    return member.id == owner_id or has_manage_channels(channel, member, overwrites=overwrites)


def has_manage_channels(channel, member, *, overwrites=None):
    """Check actual management permissions without the stored-owner shortcut."""
    if isinstance(member, discord.Role):
        source = channel.overwrites if overwrites is None else overwrites
        if member.permissions.administrator:
            return True
        allowed = member.permissions.manage_channels
        for target in (channel.guild.default_role, member):
            value = source.get(target, discord.PermissionOverwrite()).manage_channels
            if value is not None:
                allowed = value
        return allowed
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
                visible = (can_manage_room(channel, target, owner_id, overwrites=source)
                           or any(role.id in opener_ids for role in target.roles))
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
