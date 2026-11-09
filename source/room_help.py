"""First-time welcome and persistent, read-only setup help for private rooms."""
import discord


def welcome_embed():
    embed = discord.Embed(
        title="Welcome to your private room!",
        description=(
            "This room is yours. You can change its name, user limit and who is allowed to join. "
            "Your settings stay in place when the room hides — it isn't deleted.\n\n"
            "After **5 minutes empty**, the room hides. You, openers and managers "
            "can still see it and reopen it by joining. Managers automatically have opener access.\n\n"
            "When someone joins, the room becomes visible again. **Visibility doesn't grant access**: "
            "anyone without permission to connect still cannot join."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Who can join?", value=(
        "Only you have access initially (server administrators retain their normal access). "
        "Use `/room user add` to let people join, or "
        "`/room opener add` to let them reopen it without management. Use `/room manager add` for full room management. Choose your room in the channel picker and mention members or existing roles. "
        "No server roles are created."
    ), inline=False)
    embed.add_field(name="Need a hand?", value=(
        "Use **Setup help** below for a quick guide. If you accidentally remove your own "
        "Manage Channel permission, use **`/room fix`** in any server text channel."
    ), inline=False)
    return embed


class RoomHelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Setup help", style=discord.ButtonStyle.secondary,
                       custom_id="room:setup-help:v1")
    async def help(self, interaction, button):
        await interaction.response.send_message(
            "**Make this room your own**\n\n"
            "Choose a slash command, select your room in the **channel** picker, and mention one or more "
            "members or roles in **members**, separated by spaces. No channel IDs needed!\n\n"
            "**Guest → Opener → Manager**\n"
            "• `/room user add` — guests can join while the room is visible.\n"
            "• `/room opener add` — openers can see and join the hidden room to reveal it, without management rights.\n"
            "• `/room manager add` — managers can manage, open and join the room.\n"
            "• `/room user remove` — delete their permission entry and opener grant; other grants still apply.\n"
            "• `/room user deny` — explicitly deny joining and management.\n"
            "• `/room opener remove` — downgrade openers to guests. Managers are skipped; remove their manager permission first.\n"
            "• `/room manager remove` — downgrade managers to openers.\n\n"
            "Open **Edit Channel** to change the name or user limit. Use `/room fix` if you accidentally "
            "remove your own management permission; with one room it is automatic; with both types, choose your Private or Temporary room. "
            "Use `/room delete` to delete the room with confirmation.\n\n"
            "Visitors can use `/room ring` from the waiting lobby. Owners/managers can Allow, Ignore or confirm Deny.\n\n"
            "After **5 minutes empty**, the room hides. Owners, openers and managers can reopen it; "
            "guests wait until it is visible. Your settings stay in place. "
            "No server roles are created, and server administrators retain their normal access.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
