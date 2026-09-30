"""Custom emoji for decks and stakes.

The emojis are uploaded by `!util initemojis` and named deck_<name> / stake_<name>.
The helpers here look them up in a server and build the text shown in Discord
messages. If an emoji is missing (initemojis not run yet) the text is shown
without it, so nothing breaks.
"""
import re

import discord

from models.deck import Deck
from models.stake import Stake


def _safe(name: str) -> str:
    """Emoji names only allow letters, digits and underscores ('Spectral+' -> 'spectral')."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def deck_emoji_name(deck: Deck) -> str:
    return f"deck_{_safe(deck.name)}"


def stake_emoji_name(stake: Stake) -> str:
    return f"stake_{_safe(stake.name)}"


def _emoji(guild: discord.Guild, name: str) -> str:
    """The emoji followed by a space, or '' if this server doesn't have it."""
    emoji = discord.utils.get(guild.emojis, name=name)
    return f"{emoji} " if emoji else ""


def deck_label(guild: discord.Guild, deck: Deck) -> str:
    return f"{_emoji(guild, deck_emoji_name(deck))}**{deck}** deck"


def stake_label(guild: discord.Guild, stake: Stake) -> str:
    return f"{_emoji(guild, stake_emoji_name(stake))}**{stake}** stake"


def combo_label(guild: discord.Guild, deck: Deck, stake: Stake) -> str:
    """e.g. '<emoji> **Red** deck / <emoji> **White** stake'"""
    return f"{deck_label(guild, deck)} / {stake_label(guild, stake)}"