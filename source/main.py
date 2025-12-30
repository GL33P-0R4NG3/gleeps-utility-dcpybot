import os
from dotenv import load_dotenv

# Load variables from .env into the process environment
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not set – check your .env file")

import discord

class MyClient(discord.Client):
    async def on_ready(self):
        print(f'Logged on as {self.user}!')

    async def on_message(self, message):
        print(f'Message from {message.author}: {message.content}')


def main():

    intents = discord.Intents.all()

    client = MyClient(intents=intents)
    client.run(TOKEN)


if __name__ == "__main__":
    main()
