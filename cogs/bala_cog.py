"""!bala commands: control the Balatro multiplayer server during the tournament.

Talks to the server's admin HTTP API (see the server repo's README, "Tournament Mode").
The server keeps every lobby locked by default: hosts can't start games themselves,
only these commands can.

  Managers only (Administrator permission):
    !bala rollseed               roll a new hidden seed for every lobby's next game
    !bala setcombo <deck> <stake>  set the deck and stake for every lobby's next game
    !bala listlobbies            list the lobbies and the players in them
    !bala start                  start the game in every lobby that has two players

Settings (.env):
  BALA_ADMIN_URL     where the server's admin API is reachable, e.g. https://bala-admin.example.com
  BALA_ADMIN_TOKEN   the server's ADMIN_TOKEN
"""
import asyncio
import logging
import os

import aiohttp
import discord
from discord.ext import commands

import matchups
from bot import is_manager
from emojis import combo_label
from matchups import MatchupError
from models.deck import Deck
from models.stake import Stake

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# What the game calls each deck. Vanilla decks are "<name> Deck"; decks added by the
# Multiplayer mod are named after their internal key.
_MOD_DECK_NAMES = {"Violet": "b_mp_violet", "Orange": "b_mp_orange"}

# The game's stake numbers: vanilla White..Gold is 1-8, the mod adds Planet (9),
# Spectral (10) and Spectral+ (11) after Gold.
_STAKE_NUMBERS = {"White": 1, "Red": 2, "Green": 3, "Black": 4, "Blue": 5, "Purple": 6, "Orange": 7,
                  "Gold": 8, "Planet": 9, "Spectral": 10, "Spectral+": 11}


class BalaServerError(Exception):
    """The server couldn't be reached or refused the request. The message is shown in Discord as-is."""


def _game_deck_name(deck: Deck) -> str:
    return _MOD_DECK_NAMES.get(deck.name, f"{deck.name} Deck")


def _game_stake_number(stake: Stake) -> int:
    if stake.name not in _STAKE_NUMBERS:
        raise MatchupError(f"I don't know the game's number for the **{stake.name}** stake.")
    return _STAKE_NUMBERS[stake.name]


def _player(p: dict | None) -> str:
    if not p:
        return "*(empty)*"
    return discord.utils.escape_markdown(p.get("username") or "?")


def _lobby_line(lobby: dict) -> str:
    host, guest = lobby.get("host"), lobby.get("guest")
    line = f"`{lobby.get('code')}` {_player(host)} vs {_player(guest)}"
    if lobby.get("inGame"):
        line += f" · **in game**, ante {max((p or {}).get('ante', 1) for p in (host, guest))}"
    elif guest and guest.get("ready"):
        line += " · guest ready"
    if lobby.get("awaitingReconnect"):
        line += f" · :warning: {lobby['awaitingReconnect']} reconnecting"
    return line


class Bala(commands.Cog):
    """Control the Balatro server. Managers (Administrator permission) only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_url = os.getenv("BALA_ADMIN_URL", "").rstrip("/")
        self.token = os.getenv("BALA_ADMIN_TOKEN", "")
        self.session: aiohttp.ClientSession | None = None
        if not self.base_url or not self.token:
            log.warning("BALA_ADMIN_URL / BALA_ADMIN_TOKEN not set: !bala commands won't work")

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

    async def cog_unload(self):
        if self.session is not None:
            await self.session.close()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not is_manager(ctx.author):
            raise commands.MissingPermissions(["administrator"])
        return True

    async def _call(self, command: str, body: dict | None = None) -> dict:
        """Run an admin command on the server. GET without a body, POST with one."""
        if not self.base_url or not self.token:
            raise BalaServerError("The Balatro server isn't configured: set BALA_ADMIN_URL and BALA_ADMIN_TOKEN "
                                  "in the bot's .env and restart it.")
        url = f"{self.base_url}/admin/{command}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            if body is None:
                request = self.session.get(url, headers=headers)
            else:
                request = self.session.post(url, headers=headers, json=body)
            async with request as resp:
                if resp.status == 401:
                    raise BalaServerError("The Balatro server rejected the bot's token. Check BALA_ADMIN_TOKEN.")
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    log.warning("Non-JSON reply from %s (HTTP %s)", url, resp.status)
                    raise BalaServerError(f"The Balatro server gave an unexpected reply (HTTP {resp.status}). "
                                          "Check BALA_ADMIN_URL.")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Couldn't reach %s: %r", url, e)
            raise BalaServerError("Couldn't reach the Balatro server. Check that it's running and that "
                                  "BALA_ADMIN_URL is right.")
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else None
            raise BalaServerError(f"The Balatro server refused: {error or 'unknown error'}")
        return data

    # -- commands -------------------------------------------------------------

    @commands.group(name="bala", invoke_without_command=True)
    async def bala(self, ctx: commands.Context):
        """Control the Balatro server."""
        await ctx.send_help(ctx.command)

    @bala.command(name="rollseed")
    async def rollseed(self, ctx: commands.Context):
        """Roll a new seed for every lobby's next game. The seed stays hidden."""
        await self._call("reroll", {})
        # The reply contains the seed: deliberately not shown or logged
        await ctx.send("Rolled a new seed. Every lobby gets it for the next game it starts; "
                       "games already running keep theirs.")

    @bala.command(name="setcombo", usage="<deck> <stake>")
    async def setcombo(self, ctx: commands.Context, deck: str, stake: str):
        """Set the deck and stake for every lobby's next game."""
        d, s = matchups.find_deck(deck), matchups.find_stake(stake)
        await self._call("loadout", {"back": _game_deck_name(d), "stake": _game_stake_number(s)})
        await ctx.send(f"Next games will use {combo_label(ctx.guild, d, s)} in every lobby.")

    @bala.command(name="listlobbies")
    async def listlobbies(self, ctx: commands.Context):
        """List the lobbies on the server and the players in them."""
        lobbies = (await self._call("lobbies")).get("lobbies", [])
        embed = discord.Embed(title=f"Balatro lobbies ({len(lobbies)})")
        if not lobbies:
            embed.description = "No lobbies right now."
        else:
            in_game = sum(1 for lobby in lobbies if lobby.get("inGame"))
            lines = [f"{in_game} in game, {len(lobbies) - in_game} waiting.", ""]
            lines += [_lobby_line(lobby) for lobby in sorted(lobbies, key=lambda x: x.get("code", ""))]
            text = "\n".join(lines)
            if len(text) > 4096:
                text = text[:4000].rsplit("\n", 1)[0] + "\n… (list cut short)"
            embed.description = text
        await ctx.send(embed=embed)

    @bala.command(name="start")
    async def start(self, ctx: commands.Context):
        """Start the game in every lobby that has two players and isn't already playing."""
        data = await self._call("start", {"all": True})
        started, skipped = data.get("started", []), data.get("skipped", [])
        lines = []
        if started:
            lines.append(f"Started {len(started)} lobb{'y' if len(started) == 1 else 'ies'}: "
                         + ", ".join(f"`{code}`" for code in started))
        else:
            lines.append("No lobbies were started.")
        if skipped:
            lines.append(f"Skipped {len(skipped)}:")
            lines += [f"- `{x.get('code')}`: {x.get('reason')}" for x in skipped]
        await ctx.send("\n".join(lines)[:2000])

    # -- errors ---------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, (BalaServerError, MatchupError)):
            await ctx.send(str(error))
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Only administrators can use that command.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Balatro server commands only work inside a server.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Bala(bot))
