import os
import logging
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

import checks
from help_command import TourneyHelp
from persistence import TournamentStore

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
# Lets the bot see who holds a role (the stream cog's casters). Turn on "Server Members Intent"
# in the Discord developer portal (Bot page), or the bot can't log in.
intents.members = True


class TournamentBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            # Let <@id> mentions display as names without actually pinging anyone.
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=TourneyHelp(),
        )
        # Saved tournaments live in ./data next to this file (one JSON file per server).
        self.store = TournamentStore(Path(__file__).parent / "data")

    async def setup_hook(self):
        await self.load_extension("cogs.teams")
        await self.load_extension("cogs.selection")
        await self.load_extension("cogs.util")
        await self.load_extension("cogs.conjoined_cog")
        await self.load_extension("cogs.report_cog")
        await self.load_extension("cogs.bala_cog")
        await self.load_extension("cogs.stream_cog")


bot = TournamentBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Bot is online and ready.")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    """Bot-wide: anything a user isn't allowed to do (or an unknown command) is ignored silently.
    Cogs with their own error handler have already replied to everything else."""
    if checks.is_silent(error):
        return
    if ctx.cog is not None and ctx.cog.has_error_handler():
        return
    log.error("Unexpected error in %s", ctx.command, exc_info=getattr(error, "original", error))


@bot.command()
@checks.manager_only()
async def ping(ctx: commands.Context):
    """Basic health check command."""
    await ctx.send("pong")


if __name__ == "__main__":
    bot.run(TOKEN)
