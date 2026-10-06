"""The Discord side of running a set: one private text channel per set.

  start_set(...)   creates the channel (only the two teams' roles can see it),
                   pings both teams and posts the intro message.
  archive_set(...) locks the channel away from the players and moves it to an
                   archive category. Call this when a set's result is reported.

Restart safety: the only state is Set.channel_id / Set.channel_archived, which is
saved to disk right after every Discord change. Both functions are idempotent:
  * a set that already has a channel is skipped, an archived one stays archived;
  * if the bot died after creating a channel but before saving, the channel is
    found again through the marker at the start of its topic and reused instead
    of creating a duplicate.
The lock below is per server and only prevents two commands from racing.

Bot permissions: Manage Channels, Manage Roles, View Channel, Send Messages,
Read Message History.
"""
from __future__ import annotations

import asyncio
import re

import discord

import matchups
from dummies import is_dummy_uid
from emojis import combo_label
from matchups import MatchupError
from models.tournament import GameState, Team, TourneySet
from persistence import TournamentStore

ACTIVE_CATEGORY = "Conjoined Matches"
ARCHIVE_CATEGORY = "Conjoined Archive"
TOPIC_MARKER = "[conjoined-set:{}]"  # start of every open set channel's topic
ARCHIVED_PREFIX = "archived-"

INSTRUCTIONS = """Create/join a lobby like you would normally. The organiser will start the match for everyone at the same time. 
Have at least one member of your team keep an eye on this channel:

A team may declare they will be matching a skip using the designated communication channel with the opposing team. This forces the opposing team to either take a skip or enter the blind, and the former team to match. Purposeful stalling of skips is not allowed. Not matching the skip after declaring your team will match is not allowed.

you can refer to all the rules here too: docs.google.com/document/d/1jqNzznUPQkLeeYf8pE2PGSv5_3jDYmU-2VyxMVnuWsk/

When you're done with a match, report the result with the button below and upload the logfile through the link provided.
"""


_locks: dict[int, asyncio.Lock] = {}


def _lock(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


# -- helpers ------------------------------------------------------------------

def needs_start(s: TourneySet) -> bool:
    """True if the set has no channel yet and isn't already decided."""
    return s.channel_id is None and s.get_winner() is None


def teams_missing_roles(guild: discord.Guild, sets: list[TourneySet]) -> list[str]:
    """Names of teams (in these sets) without an existing Discord role."""
    missing: list[str] = []
    for s in sets:
        for team in (s.team1, s.team2):
            if (team.role_id is None or guild.get_role(team.role_id) is None) and team.name not in missing:
                missing.append(team.name)
    return missing


def _roles(guild: discord.Guild, s: TourneySet) -> tuple[discord.Role, discord.Role]:
    missing = teams_missing_roles(guild, [s])
    if missing:
        raise MatchupError("These teams have no Discord role yet: " + ", ".join(missing)
                           + ". Run `!team syncroles` first.")
    return guild.get_role(s.team1.role_id), guild.get_role(s.team2.role_id)


def channel_name(s: TourneySet) -> str:
    """'set-12-falcons-vs-otters'. Discord allows 100 characters, so each team gets half of what's
    left; letters of any alphabet are kept, everything else (spaces, symbols, emoji) becomes '-'."""
    budget = (100 - len(f"set-{s.set_id}--vs-")) // 2
    slug = lambda text: re.sub(r"[\W_]+", "-", text.lower()).strip("-")[:budget].strip("-")
    parts = ["set", str(s.set_id), slug(s.team1.name), "vs", slug(s.team2.name)]
    return "-".join(p for p in parts if p)[:100]


def _ping(team: Team, role: discord.Role) -> str:
    """Ping the team's role, or its real players one by one if the role can't be mentioned.
    A team made only of dummy players can't be pinged, so it is just named."""
    if role.mentionable:
        return role.mention
    pings = [f"<@{p.uid}>" for p in team.players if not is_dummy_uid(p.uid)]
    return " ".join(pings) or f"**{matchups._esc(team.name)}**"


def intro_text(guild: discord.Guild, s: TourneySet, role1: discord.Role, role2: discord.Role) -> str:
    matches = [f"Match {n}: {combo_label(guild, m.deck, m.stake)}" for n, m in enumerate(s.matches, start=1)]
    return "\n".join([
        f"{_ping(s.team1, role1)} {_ping(s.team2, role2)}",
        "",
        f"**Set #{s.set_id}**: {matchups.format_team(s.team1)} vs {matchups.format_team(s.team2)} (best of {s.best_of})",
        *(matches or ["_No match has been assigned yet._"]),
        "",
        INSTRUCTIONS,
    ])


def _get_set(store: TournamentStore, guild_id: int, set_id: int) -> TourneySet:
    """Always look the set up fresh: after a failed save the store reloads from disk,
    so a set object held across an `await` might no longer be the live one."""
    return matchups.require_set(store.get(guild_id), set_id)


async def _category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    return discord.utils.get(guild.categories, name=name) or await guild.create_category(
        name, reason="Conjoined tournament")


def _existing_channel(guild: discord.Guild, s: TourneySet):
    marker = TOPIC_MARKER.format(s.set_id)
    return next((c for c in guild.text_channels if c.topic and c.topic.startswith(marker)), None)


async def _create_channel(guild: discord.Guild, s: TourneySet, role1: discord.Role, role2: discord.Role):
    allow = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: allow,  # so the bot can't lock itself out
        role1: allow,
        role2: allow,
    }
    return await guild.create_text_channel(
        channel_name(s),
        category=await _category(guild, ACTIVE_CATEGORY),
        overwrites=overwrites,
        topic=f"{TOPIC_MARKER.format(s.set_id)} {s.team1.name} vs {s.team2.name}",
        reason=f"Conjoined set #{s.set_id}",
    )


# -- the two things you can do to a set ---------------------------------------

async def start_set(store: TournamentStore, guild: discord.Guild, set_id: int) -> bool:
    """Opens the set's private channel and introduces the match.

    Returns True if the set was started, False if there was nothing to do
    (already started, or already decided).
    """
    async with _lock(guild.id):
        s = _get_set(store, guild.id, set_id)
        if not needs_start(s):
            return False
        role1, role2 = _roles(guild, s)

        channel = _existing_channel(guild, s) or await _create_channel(guild, s, role1, role2)
        await channel.send(intro_text(guild, s, role1, role2))

        # Change -> save with no `await` in between.
        s = _get_set(store, guild.id, set_id)
        s.channel_id = channel.id
        for m in s.matches:
            if m.state == GameState.INIT:
                m.state = GameState.STARTED
        store.save(guild.id)
        return True


async def archive_set(store: TournamentStore, guild: discord.Guild, set_id: int) -> bool:
    """Locks the set's channel away from the players and moves it to the archive category.

    Returns True if it was archived, False if there was nothing to do. Managers keep
    access. Call this once a set's result has been reported.
    """
    async with _lock(guild.id):
        s = _get_set(store, guild.id, set_id)
        if s.channel_id is None or s.channel_archived:
            return False

        channel = guild.get_channel(s.channel_id)
        if channel is not None:
            team_role_ids = {tm.role_id for tm in (s.team1, s.team2) if tm.role_id is not None}
            # Drop the teams' permissions; @everyone can't view, so the players are locked out.
            overwrites = {target: ow for target, ow in channel.overwrites.items()
                          if getattr(target, "id", None) not in team_role_ids}
            name = channel.name if channel.name.startswith(ARCHIVED_PREFIX) else ARCHIVED_PREFIX + channel.name
            topic = channel.topic or ""
            if not topic.startswith("[archived]"):
                topic = f"[archived] {topic}"
            await channel.edit(
                name=name[:100],
                topic=topic[:1024],
                category=await _category(guild, ARCHIVE_CATEGORY),
                overwrites=overwrites,
                reason=f"Conjoined set #{set_id} finished",
            )
        # (If the channel was deleted by hand there is nothing to lock; just record it.)

        s = _get_set(store, guild.id, set_id)
        s.channel_archived = True
        store.save(guild.id)
        return True