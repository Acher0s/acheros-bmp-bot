"""Saves and loads the tournament so the bot can be restarted at any time.

Approach: the live objects (ConjoinedTournament -> Team -> Player, and Stage ->
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
from reports import MatchReport
from models.tournament import GameResult, GameState, Match, Player, Round, Stage, Team, TourneySet

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class StorageError(Exception):
    """Saved data is missing pieces, malformed, or from an unknown version."""


def _set_to_dict(s: TourneySet) -> dict:
    # A Match's teams are always its set's teams, so they aren't stored per match.
    return {
        "set_id": s.set_id,
        "best_of": s.best_of,
        "team1": s.team1.name,
        "team2": s.team2.name,
        "channel_id": s.channel_id,
        "channel_archived": s.channel_archived,
        "matches": [
            {
                "deck": m.deck.name,
                "stake": m.stake.name,
                "state": m.state.name,
                "result": m.result.name if m.result is not None else None,
                "report": m.report.to_dict() if m.report is not None else None,
                "history": m.history,
            }
            for m in s.matches
        ],
    }


def _stage_to_dict(stage: Stage) -> dict:
    return {
        "cur_round_idx": stage.cur_round_idx,
        "rounds": [{"sets": [_set_to_dict(s) for s in r.matchups]} for r in stage.rounds],
    }


def tournament_to_dict(t: ConjoinedTournament) -> dict:
    names = [team.name for team in t.teams]
    if len(set(names)) != len(names):
        # Sets refer to teams by name, so duplicates would re-link to the wrong team.
        raise StorageError("Two teams share a name; refusing to save an ambiguous tournament")
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
        "eliminated_teams": [team.name for team in t.teams if team in t.eliminated_teams],
        "next_set_id": t.next_set_id,
        "stages": [_stage_to_dict(stage) for stage in t.stages],
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

        # Everything below is missing in files saved before the tournament could start.
        teams_by_name = {team.name: team for team in teams}
        t.eliminated_teams = {teams_by_name[name] for name in data.get("eliminated_teams", [])}

        seen_ids = set()
        for sd in data.get("stages", []):
            stage = Stage()
            stage.cur_round_idx = int(sd["cur_round_idx"])
            for rd in sd["rounds"]:
                sets = []
                for setd in rd["sets"]:
                    s = TourneySet(teams_by_name[setd["team1"]], teams_by_name[setd["team2"]],
                                   best_of=int(setd["best_of"]), set_id=int(setd["set_id"]))
                    if s.set_id in seen_ids:
                        raise StorageError(f"Set ID #{s.set_id} appears more than once")
                    seen_ids.add(s.set_id)
                    s.channel_id = setd.get("channel_id")
                    s.channel_archived = bool(setd.get("channel_archived", False))
                    for md in setd["matches"]:
                        m = Match(s.team1, s.team2, decks[md["deck"]], stakes[md["stake"]])
                        m.state = GameState[md["state"]]
                        m.result = GameResult[md["result"]] if md["result"] is not None else None
                        m.report = MatchReport.from_dict(md["report"]) if md.get("report") else None
                        m.history = [dict(h) for h in md.get("history", [])]
                        s.matches.append(m)  # not add_match: that refuses once the set is decided
                    sets.append(s)
                # Append directly: add_round's validation only prints and would drop the round.
                stage.rounds.append(Round(sets))
            if stage.rounds and not 0 <= stage.cur_round_idx < len(stage.rounds):
                raise StorageError(f"Saved round index {stage.cur_round_idx} is out of range")
            t.stages.append(stage)

        # Never hand out an ID that is already in use, even if the counter was lost.
        t.next_set_id = max([int(data.get("next_set_id", 1)), *(i + 1 for i in seen_ids)])
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

    def guild_ids(self) -> list[int]:
        """Every server with a saved (or loaded) tournament."""
        ids = set(self._cache)
        for p in self.directory.glob("*.json"):
            if p.stem.isdigit():
                ids.add(int(p.stem))
        return sorted(ids)

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