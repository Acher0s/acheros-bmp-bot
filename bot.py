import os
import logging
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from persistence import TournamentStore

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True


class TournamentBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            # Let <@id> mentions display as names without actually pinging anyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # Saved tournaments live in ./data next to this file (one JSON file per server).
        self.store = TournamentStore(Path(__file__).parent / "data")

    async def setup_hook(self):
        await self.load_extension("cogs.teams")
        await self.load_extension("cogs.selection")
        await self.load_extension("cogs.util")
        await self.load_extension("cogs.conjoined_cog")
        await self.load_extension("cogs.report_cog")


bot = TournamentBot()

def is_manager(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Bot is online and ready.")


@bot.command()
async def ping(ctx: commands.Context):
    """Basic health check command."""
    await ctx.send("pong")


if __name__ == "__main__":
    bot.run(TOKEN)
