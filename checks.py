"""Who may use what, and the bot-wide rule: anything a user isn't allowed to do is ignored silently.

A *manager* is anyone with Discord's Administrator permission in the server. Permission rules are
real command checks (`manager_only()` on a command, or `require_manager(ctx)` in a cog_check), so
`!help` and the silent error handling both see them. A failed check raises a `CheckFailure`; the
cogs' error handlers and the bot-wide handler in bot.py drop those without replying (see
`is_silent`). Commands sent in DMs fail `NoPrivateMessage`, also a `CheckFailure`, so they're
ignored the same way.

Buttons can't simply be ignored (Discord shows "This interaction failed" when a click gets no
answer), so a click from someone who isn't allowed to use it is acknowledged with `ignore_click`:
nothing is shown or changed.
"""
import discord
from discord.ext import commands


class NotPermitted(commands.CheckFailure):
    """The user isn't allowed to run this command. Never shown to anyone."""


def is_manager(member) -> bool:
    return isinstance(member, discord.Member) and member.guild_permissions.administrator


def require_manager(ctx: commands.Context) -> bool:
    """For a cog_check: every command in the cog is manager-only."""
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    if not is_manager(ctx.author):
        raise NotPermitted()
    return True


async def manager_predicate(ctx: commands.Context) -> bool:
    return require_manager(ctx)


def manager_only():
    """Decorator for a single manager-only command."""
    return commands.check(manager_predicate)


def is_silent(error: Exception) -> bool:
    """True for errors that must not produce a reply: failed permission checks and unknown commands."""
    error = getattr(error, "original", error)
    return isinstance(error, (commands.CheckFailure, commands.CommandNotFound))


async def ignore_click(interaction: discord.Interaction) -> None:
    """Acknowledge a button click without showing or changing anything."""
    if not interaction.response.is_done():
        await interaction.response.defer()
