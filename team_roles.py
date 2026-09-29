"""Keeps one Discord role per team, so a member's roles say which team they're on.

The role id lives on Team.role_id (saved by persistence.py). Everything here is
idempotent: calling it again after a failure just finishes the job. All Discord
calls for one server go through one lock, so two commands can't create the same
team's role twice.

The bot needs the "Manage Roles" permission.
"""
import asyncio
import logging
from typing import Iterable

import discord

from models.tournament import Player, Team

log = logging.getLogger(__name__)

ROLE_PREFIX = "Team: "

_locks: dict[int, asyncio.Lock] = {}


def _lock(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


def role_name(team: Team) -> str:
    return f"{ROLE_PREFIX}{team.name}"[:100]


async def _member(guild: discord.Guild, uid: str) -> discord.Member | None:
    member = guild.get_member(int(uid))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(uid))
    except discord.NotFound:
        return None  # not in the server (anymore)


async def sync_team(guild: discord.Guild, team: Team, removed: Iterable[Player] = ()) -> None:
    """Make Discord match the team.

    Creates the role if the team has none (or it was deleted), gives it to every
    current player, and takes it from `removed` players. If a new role was
    created, team.role_id is updated: the caller must save the tournament.
    """
    players = list(team.players)
    removed = list(removed)
    async with _lock(guild.id):
        role = guild.get_role(team.role_id) if team.role_id else None
        if role is None:
            role = await guild.create_role(name=role_name(team), reason="Tournament team")
            team.role_id = role.id

        for p in players:
            member = await _member(guild, p.uid)
            if member is not None and role not in member.roles:
                await member.add_roles(role, reason=f"Joined {team.name}")

        for p in removed:
            member = await _member(guild, p.uid)
            if member is not None and role in member.roles:
                await member.remove_roles(role, reason=f"Left {team.name}")


async def delete_role(guild: discord.Guild, role_id: int | None) -> None:
    """Deletes a disbanded team's role (Discord takes it off all members)."""
    if role_id is None:
        return
    async with _lock(guild.id):
        role = guild.get_role(role_id)
        if role is not None:
            await role.delete(reason="Tournament team disbanded")