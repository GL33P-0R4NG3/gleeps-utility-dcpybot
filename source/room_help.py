"""First-time welcome and persistent, read-only setup help for private rooms."""
import discord


def welcome_embed():
    embed = discord.Embed(
        title="Welcome to your private room!",
        description=(
            "This room is yours. You can change its name, user limit and who is allowed to join. "
            "Your settings stay in place when the room hides — it isn't deleted.\n\n"
            "After **5 minutes empty**, the room hides. You and people with **Manage Channel** "
            "can still see it and reopen it by joining. Room managers also need permission to connect.\n\n"
            "When someone joins, the room becomes visible again. **Visibility doesn't grant access**: "
            "anyone without permission to connect still cannot join."
        ),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Who can join?", value=(
        "Only you have access initially (server administrators retain their normal access). "
        "Use `/room user add` to let people join, or "
        "`/room manager add` to let them manage and reopen the room too. Choose your room in the channel picker and mention the people you want to add. "
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
            "Choose a slash command below, then select your room in the **channel** picker. "
            "For commands with a **members** field, mention one or more people, separated by spaces. "
            "No channel IDs needed!\n\n"
            "• **Personalize it:** open **Edit Channel** to change the name or user limit.\n"
            "• **Let people join:** `/room user add` grants joining without adding management permissions.\n"
            "• **Add room managers:** `/room manager add` lets people manage the room and reopen it when hidden.\n"
            "• **Remove managers:** `/room manager remove` removes management but keeps their join access.\n"
            "• **Remove join access:** `/room user remove` removes joining and management, and disconnects them if they are in the room.\n"
            "• **Recover your permissions:** `/room fix` restores your own missing Manage Channel permission. "
            "You can leave the channel field empty to find your private room automatically.\n"
            "• **Delete the room:** `/room delete` asks you to confirm first. Cancel or ignore the prompt to keep it.\n\n"
            "After **5 minutes empty**, the room hides. Managers with join access can reopen it; "
            "people with join-only access must wait until it is visible. Your settings stay in place. "
            "Server administrators retain their normal access.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
