"""Owner-only SQL form. Authorization is repeated when the form is submitted."""
import asyncio
import io
import json
import logging
import sqlite3

import discord
from discord.ext import commands

from data.sql_runner import execute_sql

log = logging.getLogger(__name__)


class SQLModal(discord.ui.Modal, title="Bot database SQL console"):
    statement = discord.ui.TextInput(label="SQL statement (writes run immediately)",
                                    style=discord.TextStyle.paragraph, max_length=4000,
                                    placeholder="SELECT name, sql FROM sqlite_master WHERE type='table';")

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction):
        if not await self.cog.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only the bot owner can execute SQL.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.cog.lock:
                result = await execute_sql(self.cog.bot.db.path, str(self.statement))
        except (sqlite3.Error, ValueError) as error:
            log.warning("SQL failed actor_id=%s error_type=%s", interaction.user.id, type(error).__name__)
            await interaction.followup.send(f"SQL failed; changes were rolled back.\n{str(error)[:1500]}",
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        except Exception:
            log.exception("SQL console failed actor_id=%s", interaction.user.id)
            await interaction.followup.send("SQL could not be completed. Check the bot log for details.", ephemeral=True)
            return
        log.info("SQL completed actor_id=%s changes=%s backup=%s", interaction.user.id, result.changes, result.backup)
        summary = f"SQL completed. Changed rows (including cascades): {result.changes}."
        if result.backup:
            summary += f"\nBackup: `{result.backup}`"
        if result.truncated:
            summary += "\nResult preview truncated (100 rows / bounded cell and output sizes)."
        kwargs = {}
        if result.columns:
            payload = json.dumps({"columns": result.columns, "rows": result.rows}, indent=2, ensure_ascii=False)
            kwargs["file"] = discord.File(io.BytesIO(payload.encode("utf-8")), filename="sql-result.json")
        await interaction.followup.send(summary, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **kwargs)


class SQLView(discord.ui.View):
    def __init__(self, cog, actor_id):
        super().__init__(timeout=120)
        self.cog = cog
        self.actor_id = actor_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.actor_id or not await self.cog.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only the bot owner who opened this console can use it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Open SQL console", style=discord.ButtonStyle.primary)
    async def open_console(self, interaction, button):
        await interaction.response.send_modal(SQLModal(self.cog))


class SQLConsole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    @commands.hybrid_command(name="sql")
    @commands.is_owner()
    async def sql(self, ctx):
        """Open a private SQL form (bot owner only; works in DMs too)."""
        await ctx.reply("Bot database console. Reads and writes affect the **whole bot database**. "
                        "Writes run immediately with a backup; one statement per submission. "
                        "Deleting tracking rows does not delete Discord channels or roles. "
                        "The form and results are private.", view=SQLView(self, ctx.author.id), ephemeral=True)
