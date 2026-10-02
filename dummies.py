"""Dummy teams with fake players, for testing without 16 real Discord users. No Discord imports.

Dummy players get ids in a reserved range far above any real Discord user id, so
they can never clash with a real user and the rest of the bot can tell them apart
(is_dummy_uid). Anything that talks to Discord about a player (giving roles,
pinging) must skip dummies.

Everything goes through registration.py, so the normal rules apply: no changes
once the tournament has started, no duplicate team names, at most 16 teams.
"""
from __future__ import annotations

from typing import List

import registration as reg
from conjoined import ConjoinedTournament
from models.tournament import Player, Team

DUMMY_UID_BASE = 9_000_000_000_000_000_000  # a valid 64-bit id, but Discord ids won't get here until ~2083
MAX_TEAMS = 16  # same limit as ConjoinedTournament.is_full()


def is_dummy_uid(uid) -> bool:
    try:
        return int(uid) >= DUMMY_UID_BASE
    except (TypeError, ValueError):
        return False


def is_dummy_team(team: Team) -> bool:
    """True if the team has players and every one of them is a dummy."""
    return bool(team.players) and all(is_dummy_uid(p.uid) for p in team.players)


def is_real_team(team: Team) -> bool:
    """True if at least one player is a real Discord user (an empty team isn't real either)."""
    return any(not is_dummy_uid(p.uid) for p in team.players)


def fill_dummy_teams(t: ConjoinedTournament, count: int | None = None) -> List[Team]:
    """Registers dummy teams: `count` of them, or as many as it takes to reach 16.

    Returns the new teams. Nothing is changed if the request can't be met.
    """
    free = MAX_TEAMS - len(t.teams)
    if free <= 0:
        raise reg.RegistrationError("The tournament is already full.")
    if count is None:
        count = free
    if count < 1:
        raise reg.RegistrationError("N must be at least 1.")
    if count > free:
        raise reg.RegistrationError(f"There are only {free} free spot(s), so I can't add {count} teams.")

    size = reg.MAX_TEAM_SIZE or 2
    next_uid = max((int(p.uid) for tm in t.teams for p in tm.players if is_dummy_uid(p.uid)),
                   default=DUMMY_UID_BASE - 1) + 1

    created: List[Team] = []
    n = 0
    for _ in range(count):
        while True:  # first free "Dummy <n>"
            n += 1
            name = f"Dummy {n}"
            if reg.find_team(t, name) is None:
                break
        players = []
        for k in range(size):
            players.append(Player(str(next_uid), f"dummy{n}{chr(ord('a') + k)}"))
            next_uid += 1
        created.append(reg.create_team(t, name, players))  # raises before changing anything if registration is closed
    return created


def clear_dummy_teams(t: ConjoinedTournament) -> List[Team]:
    """Disbands every dummy team (real teams are never touched). Returns the removed teams."""
    dummies = [tm for tm in t.teams if is_dummy_team(tm)]
    if not dummies:
        raise reg.RegistrationError("There are no dummy teams.")
    for tm in dummies:
        reg.disband_team(t, tm)  # first call raises if registration is closed, before anything is removed
    return dummies