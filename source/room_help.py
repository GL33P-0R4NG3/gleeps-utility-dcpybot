"""First-time welcome and persistent, read-only setup help for private rooms."""
import discord


def welcome_embed(role):
    embed = discord.Embed(
        title="Welcome to your room!" if role.is_default() else "Welcome to your private room!",
        description=(
            "This room is yours. You can change its name, user limit and who is allowed to join. "
            "Your settings stay in place when the room hides — it isn't deleted.\n\n"
            "After **15 minutes empty**, the room hides. You and people with **Manage Channel** "
            "can still see it and reopen it by joining. Room managers also need permission to connect.\n\n"
            "When someone joins, the room becomes visible again. **Visibility doesn't grant access**: "
            "anyone without permission to connect still cannot join."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Who can join?", value=(
        "Everyone can join this room while it is visible. When hidden, only you and room managers "
        "can see and reopen it (server administrators retain their normal access)."
        if role.is_default() else
        f"Other members are blocked by default unless they have the access role {role.mention} "
        "or you explicitly allow them in the channel's permissions. "
        "If you linked an existing role, its members already have access while the room is visible."
    ), inline=False)
    embed.add_field(name="Need a hand?", value=(
        "Use **Setup help** below for a quick guide. If you accidentally remove your own "
        "Manage Channel permission, type **`g!room fix`** in any server text channel."
    ), inline=False)
    return embed


class RoomHelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Setup help", style=discord.ButtonStyle.secondary,
                       custom_id="room:setup-help:v1")
    async def help(self, interaction, button):
        channel_id = interaction.channel_id
        await interaction.response.send_message(
            "**Make this room your own**\n\n"
            "• Open **Edit Channel** to change its name or user limit.\n"
            "• Under **Permissions**, choose a member or role and allow **Connect** to let them join. "
            "Keep **Connect** denied for `@everyone` if you want the room to stay private. "
            "You can also ask a server administrator to assign the room's access role.\n"
            f"• To let someone reopen the hidden room, use `g!room manager {channel_id} @member`. "
            "This grants both management and join permission.\n"
            "• Accidentally removed your own management permission? Use `g!room fix`. "
            "It restores only that permission, and does nothing if you already have it.\n"
            f"• To delete the room, use `g!room delete {channel_id}` and confirm.\n\n"
            "Your room settings remain when it hides. The bot handles visibility automatically; "
            "server administrators retain Discord's normal access.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
