"""!bala commands: control the Balatro multiplayer server during the tournament.

Talks to the server's admin HTTP API (see the server repo's README, "Tournament Mode").
By default lobbies are locked (hosts can't start games, only `!bala start` can), every
game uses the rolled seed, and the combo set with setcombo is forced. Each of those
three can be switched off on its own.

  Managers only (Administrator permission):
    !bala status                   show the three switches and the current combo (never the seed)
    !bala manual_start on|off      on: hosts can start games. off: lobbies locked
    !bala force_seed on|off        on: every game uses the rolled seed. off: random seed per game
    !bala force_combo on|off       on: the setcombo deck/stake is forced. off: hosts choose
    !bala rollseed                 roll a new hidden seed for every lobby's next game
    !bala setcombo <deck> <stake>  set the deck and stake for every lobby's next game
    !bala listlobbies              list the lobbies and the players in them
    !bala start                    start the game in every lobby that has two players

Settings (.env):
  BALA_ADMIN_URL     where the server's admin API is reachable, e.g. https://bala-admin.example.com
  BALA_ADMIN_TOKEN   the server's ADMIN_TOKEN
"""
import asyncio
import logging
import os
import time

import aiohttp
import discord
from discord.ext import commands

import matchups
import checks
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


def _combo_text(guild: discord.Guild, back: str | None, stake: int | None) -> str:
    """The server's loadout in the bot's own deck/stake terms, with emojis when both are known."""
    deck_name = next((name for name, game in _MOD_DECK_NAMES.items() if game == back), None) \
        or (back[:-len(" Deck")] if back and back.endswith(" Deck") else back)
    stake_name = next((name for name, n in _STAKE_NUMBERS.items() if n == stake), None)
    try:
        return combo_label(guild, matchups.find_deck(deck_name), matchups.find_stake(stake_name))
    except (MatchupError, TypeError, AttributeError):
        return f"deck **{deck_name or 'host choice'}** / stake **{stake_name or stake or 'host choice'}**"


def _switches_text(guild: discord.Guild, t: dict) -> str:
    has_combo = t.get("back") is not None or t.get("stake") is not None
    if not t.get("forceCombo"):
        combo = "off (hosts pick their deck and stake)"
    elif has_combo:
        combo = f"on: {_combo_text(guild, t.get('back'), t.get('stake'))}"
    else:
        combo = "on, but no combo set yet (use `!bala setcombo`)"
    return "\n".join([
        "**Manual start:** " + ("on (hosts can start games)" if t.get("manualStart")
                                else "off (lobbies locked, use `!bala start`)"),
        "**Force seed:** " + ("on (every game uses the rolled seed)" if t.get("forceSeed")
                              else "off (random seed every game)"),
        f"**Force combo:** {combo}",
    ])


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
        return checks.require_manager(ctx)

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

    @bala.command(name="status")
    async def status(self, ctx: commands.Context):
        """Show the three switches and the current combo. The seed stays hidden."""
        t = (await self._call("status")).get("tourney", {})
        await ctx.send(embed=discord.Embed(title="Balatro server settings", description=_switches_text(ctx.guild, t)))

    async def _set_switch(self, ctx: commands.Context, key: str, enabled: bool):
        t = (await self._call("settings", {key: enabled})).get("tourney", {})
        await ctx.send(embed=discord.Embed(title="Balatro server settings", description=_switches_text(ctx.guild, t)))

    @bala.command(name="manual_start", usage="<on|off>")
    async def manual_start(self, ctx: commands.Context, enabled: bool):
        """on: hosts can start games themselves. off: lobbies are locked, only !bala start starts them."""
        await self._set_switch(ctx, "manual_start", enabled)

    @bala.command(name="force_seed", usage="<on|off>")
    async def force_seed(self, ctx: commands.Context, enabled: bool):
        """on: every game uses the rolled seed until the next rollseed. off: a random seed every game."""
        await self._set_switch(ctx, "force_seed", enabled)

    @bala.command(name="force_combo", usage="<on|off>")
    async def force_combo(self, ctx: commands.Context, enabled: bool):
        """on: every game uses the setcombo deck and stake. off: hosts pick their own."""
        await self._set_switch(ctx, "force_combo", enabled)

    @bala.command(name="rollseed")
    async def rollseed(self, ctx: commands.Context):
        """Roll a new seed for every lobby's next game. The seed stays hidden."""
        data = await self._call("reroll", {})
        # The reply contains the seed: deliberately not shown or logged
        msg = ("Rolled a new seed. Every lobby gets it for the next game it starts, and keeps it for every "
               "game after that until the next roll. Games already running keep theirs.")
        if not data.get("tourney", {}).get("forceSeed"):
            msg += "\n:warning: **Force seed is off**, so games use random seeds. `!bala force_seed on` to use this one."
        await ctx.send(msg)

    @bala.command(name="setcombo", usage="<deck> <stake>")
    async def setcombo(self, ctx: commands.Context, deck: str, stake: str):
        """Set the deck and stake for every lobby's next game."""
        d, s = matchups.find_deck(deck), matchups.find_stake(stake)
        data = await self._call("loadout", {"back": _game_deck_name(d), "stake": _game_stake_number(s)})
        msg = f"Next games will use {combo_label(ctx.guild, d, s)} in every lobby."
        if not data.get("tourney", {}).get("forceCombo"):
            msg = (f"Saved {combo_label(ctx.guild, d, s)}.\n:warning: **Force combo is off**, so hosts still pick "
                   "their own. `!bala force_combo on` to use this one.")
        await ctx.send(msg)

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
        if started:
            # Signal for other cogs: the current match of every set in progress starts now
            self.bot.dispatch("bala_started", ctx.guild, time.time())
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
        if checks.is_silent(error):
            return
        error = getattr(error, "original", error)
        if isinstance(error, (BalaServerError, MatchupError)):
            await ctx.send(str(error))
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            # also covers on/off typos (BadBoolArgument is a BadArgument)
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Bala(bot))
