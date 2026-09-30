"""!util commands: admin helpers (managers only).

!util initemojis uploads one custom emoji per deck and per stake to the server,
using the PNGs in assets/. It is safe to run again: emojis that already exist
(matched by name) are skipped, so it only fills in what's missing.

Emoji names:  deck_<name>  and  stake_<name>  (lowercase, e.g. deck_red, stake_spectral).
Later code can find an emoji with:
    discord.utils.get(guild.emojis, name=deck_emoji_name(deck))

The bot needs the "Manage Expressions" permission (called "Manage Emojis and
Stickers" in older Discord versions). Run the bot from the project folder, since
the asset paths are relative (./assets/...).
"""
import logging
import re
from pathlib import Path

import discord
from discord.ext import commands

from models.deck import DECKS, Deck
from models.stake import STAKES, Stake
from persistence import StorageError
from bot import is_manager

log = logging.getLogger(__name__)

MAX_EMOJI_BYTES = 256 * 1024  # Discord's limit for an emoji image


class UtilError(Exception):
    """A rule violation. The message is shown to the user in Discord as-is."""


def _safe(name: str) -> str:
    """Emoji names only allow letters, digits and underscores ('Spectral+' -> 'spectral')."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def deck_emoji_name(deck: Deck) -> str:
    return f"deck_{_safe(deck.name)}"


def stake_emoji_name(stake: Stake) -> str:
    return f"stake_{_safe(stake.name)}"


class Util(commands.Cog):
    """Admin utilities. Managers (Administrator permission) only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not is_manager(ctx.author):
            raise commands.MissingPermissions(["administrator"])
        return True

    @commands.group(name="util", invoke_without_command=True)
    async def util(self, ctx: commands.Context):
        """Admin utilities."""
        await ctx.send_help(ctx.command)

    @util.command(name="initemojis")
    async def initemojis(self, ctx: commands.Context):
        """Add an emoji for every deck and stake to this server (skips ones that already exist)."""
        wanted = ([(deck_emoji_name(d), d.png_path) for d in DECKS]
                  + [(stake_emoji_name(s), s.png_path) for s in STAKES])

        existing = await ctx.guild.fetch_emojis()  # fresh from Discord, not the cache
        have = {e.name for e in existing}
        todo = [(name, path) for name, path in wanted if name not in have]
        if not todo:
            await ctx.send("All deck and stake emojis already exist. Nothing to do.")
            return

        free = ctx.guild.emoji_limit - sum(1 for e in existing if not e.animated)
        if len(todo) > free:
            raise UtilError(f"I need {len(todo)} free emoji slots but this server only has {free}. "
                            "Delete some emojis (or boost the server) and try again. Nothing was added.")

        await ctx.send(f"Adding {len(todo)} emoji(s). Discord rate-limits this, so it can take a minute...")

        added: list[discord.Emoji] = []
        failed: list[str] = []
        async with ctx.typing():
            for name, path in todo:
                try:
                    data = Path(path).read_bytes()
                    if len(data) > MAX_EMOJI_BYTES:
                        raise ValueError(f"file is over {MAX_EMOJI_BYTES // 1024} KB")
                    emoji = await ctx.guild.create_custom_emoji(
                        name=name, image=data, reason="Tournament deck/stake emoji")
                except discord.Forbidden:
                    raise  # missing permission: no point trying the rest
                except (OSError, ValueError, discord.HTTPException) as e:
                    log.warning("Couldn't create emoji %s from %s: %r", name, path, e)
                    reason = (str(e) if isinstance(e, ValueError)
                              else "couldn't read the file" if isinstance(e, OSError)
                              else "Discord rejected it")
                    failed.append(f"`{name}` ({reason})")
                else:
                    added.append(emoji)

        lines = []
        if added:
            lines.append(f"Added {len(added)} emoji(s): " + " ".join(str(e) for e in added))
        if failed:
            lines.append(f":warning: {len(failed)} failed: " + ", ".join(failed)
                         + ". Fix the problem and run the command again; it only adds what's missing.")
        await ctx.send("\n".join(lines))

    # -- errors ---------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, UtilError):
            await ctx.send(str(error))
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Only administrators can use util commands.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Util commands only work inside a server.")
        elif isinstance(error, discord.Forbidden):
            log.warning("Missing permission while running %s: %r", ctx.command, error)
            await ctx.send("I'm missing the **Manage Expressions** permission (Manage Emojis and Stickers). "
                           "Grant it and run the command again; it only adds what's missing.")
        elif isinstance(error, (StorageError, OSError)):
            log.error("Problem while running %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong reading data or files. Check the bot's log.")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Util(bot))