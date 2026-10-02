"""Team registration rules.

Pure logic with no Discord imports, so it's easy to test and can be reused if we
later switch from !commands to slash commands. Every function validates
everything first and only then changes anything, so a failed request never
leaves a half-applied change behind.
"""
from __future__ import annotations

from typing import Iterable, List

from conjoined import ConjoinedTournament
from models.tournament import Player, Team

MAX_TEAM_SIZE: int | None = 2
MAX_NAME_LENGTH = 32


class RegistrationError(Exception):
    """A rule violation. The message is shown to the user in Discord as-is."""


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def find_team(t: ConjoinedTournament, name: str) -> Team | None:
    key = _key(name)
    return next((team for team in t.teams if _key(team.name) == key), None)


def require_team(t: ConjoinedTournament, name: str) -> Team:
    team = find_team(t, name)
    if team is None:
        raise RegistrationError(f"No team called **{name}**. Use `!team list` to see registered teams.")
    return team


def team_of(t: ConjoinedTournament, uid: str) -> Team | None:
    return next((team for team in t.teams if any(p.uid == uid for p in team.players)), None)


def _require_open(t: ConjoinedTournament) -> None:
    if t.started:
        raise RegistrationError("The tournament has already started, so team changes are closed.")


def _dedupe(players: Iterable[Player]) -> List[Player]:
    seen, out = set(), []
    for p in players:
        if p.uid not in seen:
            seen.add(p.uid)
            out.append(p)
    return out


def _check_size(size: int) -> None:
    if MAX_TEAM_SIZE is not None and size > MAX_TEAM_SIZE:
        raise RegistrationError(f"Teams can have at most {MAX_TEAM_SIZE} players.")


def create_team(t: ConjoinedTournament, name: str, players: Iterable[Player]) -> Team:
    _require_open(t)
    name = " ".join(name.split())
    if not name:
        raise RegistrationError("A team needs a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise RegistrationError(f"Team names can be at most {MAX_NAME_LENGTH} characters.")
    if find_team(t, name) is not None:
        raise RegistrationError(f"A team called **{name}** already exists.")
    if t.is_full():
        raise RegistrationError("The tournament is full.")

    players = _dedupe(players)  # may be empty: players can be added later with !team add
    _check_size(len(players))
    for p in players:
        other = team_of(t, p.uid)
        if other is not None:
            raise RegistrationError(f"<@{p.uid}> is already on **{other.name}**.")

    team = Team(name)
    for p in players:
        team.add_player(p)
    t.teams.append(team)
    return team


def add_players(t: ConjoinedTournament, team: Team, players: Iterable[Player]) -> None:
    _require_open(t)
    players = _dedupe(players)
    for p in players:
        current = team_of(t, p.uid)
        if current is not None:
            raise RegistrationError(f"<@{p.uid}> is already on **{current.name}**.")
    _check_size(len(team.players) + len(players))
    for p in players:
        team.add_player(p)


def remove_players(t: ConjoinedTournament, team: Team, players: Iterable[Player]) -> bool:
    """Removes players. Returns True if the team ended up empty and was disbanded."""
    _require_open(t)
    players = _dedupe(players)
    for p in players:
        if not any(x.uid == p.uid for x in team.players):
            raise RegistrationError(f"<@{p.uid}> isn't on **{team.name}**.")
    for p in players:
        team.remove_player(p)
    if team.is_empty():
        t.teams.remove(team)
        return True
    return False


def disband_team(t: ConjoinedTournament, team: Team) -> None:
    _require_open(t)
    t.teams.remove(team)