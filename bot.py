import os
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


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
