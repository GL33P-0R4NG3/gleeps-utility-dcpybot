"""Paginated live Discord view of a music session."""
import asyncio

import discord


class QueueView(discord.ui.View):
    page_size = 10

    def __init__(self, session, page, owner_id):
        super().__init__(timeout=600)
        self.session = session
        self.page = page
        self.owner_id = owner_id
        self.message = None
        self.lock = asyncio.Lock()
        self.update = None

    def render(self):
        session = self.session
        entries = list(session.pending.values())
        pages = max(1, (len(entries) + self.page_size - 1) // self.page_size)
        self.page = max(1, min(self.page, pages))
        self.previous.disabled = self.page == 1 or session.closing
        self.next_page.disabled = self.page >= pages or session.closing
        embed = discord.Embed(title="Music queue")
        if session.closing:
            embed.description = "Music session ended."
            return embed
        current = session.current
        embed.add_field(name="Now playing", inline=False,
                        value=discord.utils.escape_markdown(current.title[:300]) if current else "Nothing playing")
        start = (self.page - 1) * self.page_size
        lines = []
        for position, entry in enumerate(entries[start:start + self.page_size], start=start + 1):
            label = " ".join(entry.query.split())
            if len(label) > 150:
                label = label[:149] + "…"
            lines.append(f"**#{position}** · {discord.utils.escape_markdown(label)}")
        # Description renders before fields; keep the waiting list below Now playing.
        for offset in range(0, len(lines), 2):
            embed.add_field(name="Waiting" if offset == 0 else "\u200b",
                            value="\n".join(lines[offset:offset + 2]), inline=False)
        embed.set_footer(text=f"{len(entries)} waiting · Page {self.page}/{pages}")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own list with /queue list.", ephemeral=True)
            return False
        return True

    async def edit(self):
        async with self.lock:
            if self.message is None or self.is_finished():
                return
            try:
                await self.message.edit(embed=self.render(), view=None if self.session.closing else self)
            except discord.HTTPException:
                self.stop()
            if self.session.closing:
                self.stop()

    def refresh(self):
        if self.message and not self.is_finished() and self.update is None:
            self.update = asyncio.create_task(self.delayed_edit())

    async def delayed_edit(self):
        await asyncio.sleep(1)
        self.update = None
        await self.edit()

    async def on_timeout(self):
        self.session.views.discard(self)
        if self.message:
            try:
                await self.message.edit(content="Live updates ended. Use /queue list to reopen.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await interaction.response.defer()
        self.page -= 1
        await self.edit()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await interaction.response.defer()
        self.page += 1
        await self.edit()
