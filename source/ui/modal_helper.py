# modal_helper.py
import discord

class BaseView(discord.ui.View):
    """Base view for all views."""
    def __init__(self, channel_id: int, author: discord.Member):
        super().__init__(timeout=120)  # 2min auto‑close
        self.author = author