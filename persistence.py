"""Saves and loads the tournament so the bot can be restarted at any time.

Approach: the live objects (ConjoinedTournament -> Team -> Player, later Stage ->
Round -> TourneySet -> Match) stay the single source of truth in memory. After
every change we write the *whole* state to one JSON file (one file per Discord
server). Objects that refer to each other (a Match points at two Teams, a Deck,
a Stake) are stored by name, and re-linked to the real objects on load.

Safety rules:
  * Writes are atomic: temp file -> fsync -> os.replace, so a crash mid-write can
    never leave a half-written file.
  * The previous snapshot is kept as <guild>.json.bak.
  * If the file can't be read, we fall back to the .bak. If that fails too we raise
    instead of starting a fresh tournament (which would overwrite your data).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from conjoined import ConjoinedTournament, VoteResults
from models.deck import DECKS
from models.stake import STAKES
from models.tournament import Player, Team

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class StorageError(Exception):
    """Saved data is missing pieces, malformed, or from an unknown version."""


def tournament_to_dict(t: ConjoinedTournament) -> dict:
    if t.stages:
        # Guard so we can never silently drop data: stage/round/match saving is
        # added in the "start the tournament" step.
        raise NotImplementedError("Saving stages is not implemented yet")
    return {
        "version": SCHEMA_VERSION,
        "started": t.started,
        "cur_stage_idx": t.cur_stage_idx,
        "banned_decks": [d.name for d in t.banned_decks],
        "banned_stakes": [s.name for s in t.banned_stakes],
        "teams": [
            {
                "name": team.name,
                "role_id": team.role_id,
                "players": [{"uid": p.uid, "username": p.username} for p in team.players],
            }
            for team in t.teams
        ],
        "vote_results": [
            {
                "selection": [{"deck": d.name, "stake": s.name} for d, s in v.selection],
                "votes": list(v.votes),
            }
            for v in t.vote_results
        ],
    }


def tournament_from_dict(data: dict) -> ConjoinedTournament:
    if data.get("version") != SCHEMA_VERSION:
        raise StorageError(f"Unsupported data version: {data.get('version')!r}")

    decks = {d.name: d for d in DECKS}
    stakes = {s.name: s for s in STAKES}

    try:
        teams = []
        for td in data["teams"]:
            team = Team(td["name"])
            team.role_id = td.get("role_id")  # missing in files saved before roles existed
            for pd in td["players"]:
                team.add_player(Player(pd["uid"], pd["username"]))
            teams.append(team)

        t = ConjoinedTournament(teams=teams)
        t.started = data["started"]
        t.cur_stage_idx = data["cur_stage_idx"]
        t.banned_decks = [decks[name] for name in data["banned_decks"]]
        t.banned_stakes = [stakes[name] for name in data["banned_stakes"]]

        for vd in data.get("vote_results", []):  # missing in older files
            selection = [(decks[c["deck"]], stakes[c["stake"]]) for c in vd["selection"]]
            votes = [int(v) for v in vd["votes"]]
            if len(votes) != len(selection):
                raise StorageError("A saved vote has a different number of votes than options")
            results = VoteResults(selection)
            results.votes = votes
            t.vote_results.append(results)
    except (KeyError, TypeError, ValueError) as e:
        raise StorageError(f"Malformed tournament data: {e!r}") from e
    return t


class TournamentStore:
    """Holds one ConjoinedTournament per Discord server, backed by JSON files."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cache: dict[int, ConjoinedTournament] = {}

    def _path(self, guild_id: int) -> Path:
        return self.directory / f"{guild_id}.json"

    def get(self, guild_id: int) -> ConjoinedTournament:
        if guild_id not in self._cache:
            self._cache[guild_id] = self._load(guild_id)
        return self._cache[guild_id]

    def save(self, guild_id: int) -> None:
        """Call right after changing the tournament, before replying to the user."""
        path = self._path(guild_id)
        tmp = path.with_name(path.name + ".tmp")
        try:
            data = tournament_to_dict(self._cache[guild_id])
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            os.replace(tmp, path)
        except Exception:
            # Memory may now differ from disk; forget it so the next access
            # reloads the last good snapshot instead of drifting.
            self._cache.pop(guild_id, None)
            raise

    def _load(self, guild_id: int) -> ConjoinedTournament:
        path = self._path(guild_id)
        backup = path.with_name(path.name + ".bak")

        if not path.exists() and not backup.exists():
            return ConjoinedTournament()  # first run for this server

        problems = []
        for candidate in (path, backup):
            if not candidate.exists():
                continue
            try:
                with open(candidate, encoding="utf-8") as f:
                    t = tournament_from_dict(json.load(f))
            except (OSError, json.JSONDecodeError, StorageError) as e:
                problems.append(f"{candidate.name}: {e}")
                continue
            if candidate == backup:
                log.warning("Loaded %s from backup because the main file was unreadable (%s)", guild_id, problems)
            return t

        raise StorageError("Could not load saved tournament (left untouched): " + "; ".join(problems))