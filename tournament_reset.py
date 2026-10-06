"""Wiping the tournament so a new one can start: `!conjoined reset`.

Deletes everything the bot made in Discord for the tournament:
  * every set channel (open and archived) and the two set categories,
  * every team voice channel and their category,
  * every team role (Discord takes it off all members).
Then replaces the saved tournament with an empty one: no teams, no stages, no
bans, registration open. The old data is copied to <guild>.reset-<time>.json
first. Uploaded logs stay on disk (data/logs).

Only things the bot can recognise as its own are deleted: channels by the ids
saved on sets/teams or the set marker in their topic, roles by the ids saved on
teams or the "Team: " name prefix. A category is only deleted once it's empty,
so anything else someone put in it is kept.

Restart safety: Discord is cleaned up first and the data wiped last, so if
something fails halfway, running the command again finishes the job.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field

import discord

import match_channels
import team_roles
import team_vcs
from conjoined import ConjoinedTournament
from persistence import TournamentStore


@dataclass
class Cleanup:
    channels: list = field(default_factory=list)  # set text channels and team voice channels
    roles: list = field(default_factory=list)
    categories: list = field(default_factory=list)

    def summary(self) -> str:
        texts = sum(1 for c in self.channels if isinstance(c, discord.TextChannel))
        return (f"{texts} set channel(s), {len(self.channels) - texts} team voice channel(s), "
                f"{len(self.roles)} team role(s) and {len(self.categories)} category(ies)")


def _is_set_channel(channel) -> bool:
    topic = getattr(channel, "topic", None) or ""
    marker = match_channels.TOPIC_MARKER.split("{")[0]  # "[conjoined-set:"
    return topic.startswith(marker) or topic.startswith(f"[archived] {marker}")


def collect(guild: discord.Guild, t: ConjoinedTournament) -> Cleanup:
    """What a reset would delete in this server."""
    ids = {s.channel_id for stage in t.stages for r in stage.rounds for s in r.matchups if s.channel_id}
    ids |= {team.vc_id for team in t.teams if team.vc_id}
    categories = [c for c in guild.categories
                  if c.name in (match_channels.ACTIVE_CATEGORY, match_channels.ARCHIVE_CATEGORY, team_vcs.CATEGORY)]
    vc_names = {team_vcs.channel_name(team) for team in t.teams}

    channels = []
    for c in guild.channels:
        if isinstance(c, discord.CategoryChannel):
            continue
        ours = (c.id in ids or (isinstance(c, discord.TextChannel) and _is_set_channel(c))
                or (isinstance(c, discord.VoiceChannel) and c.category is not None
                    and c.category.name == team_vcs.CATEGORY and c.name in vc_names))
        if ours:
            channels.append(c)

    role_ids = {team.role_id for team in t.teams if team.role_id}
    roles = [r for r in guild.roles
             if not r.is_default() and not r.managed
             and (r.id in role_ids or (r.name.startswith(team_roles.ROLE_PREFIX) and r.is_assignable()))]

    gone = {c.id for c in channels}
    empty_after = [cat for cat in categories if all(c.id in gone for c in cat.channels)]
    return Cleanup(channels, roles, empty_after)


async def wipe_discord(guild: discord.Guild, cleanup: Cleanup) -> None:
    """Deletes it all. Already-deleted things are skipped; a missing permission raises Forbidden."""
    reason = "Conjoined tournament reset"
    for item in [*cleanup.channels, *cleanup.categories, *cleanup.roles]:
        try:
            await item.delete(reason=reason)
        except discord.NotFound:
            pass


def wipe_data(store: TournamentStore, guild_id: int) -> str | None:
    """Keeps a copy of the saved tournament, then replaces it with an empty one.
    Returns the copy's file name (None if there was nothing saved)."""
    path = store.directory / f"{guild_id}.json"
    copy = None
    if path.exists():
        copy = path.with_name(f"{guild_id}.reset-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(path, copy)
    store.replace(guild_id, ConjoinedTournament())
    return copy.name if copy else None
