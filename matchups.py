"""Rules for running sets and matches. Pure logic, no Discord imports.

Words used here:
  * set   (TourneySet): one matchup between two teams, shown with an ID like #12.
  * match (Match):      one game inside a set, played with a deck and a stake.

Everything that changes the tournament is a plain function on the model, so a
future per-set command (say `!conjoined add_match <set ID> <deck> <stake>`) can
call `add_match` directly, and `add_matches_all` is just that in a loop.
Like registration.py, each function validates first and only then changes things.
"""
from __future__ import annotations

import re
from typing import List, Set, Tuple

from conjoined import ConjoinedTournament, TournamentError
from models.deck import DECKS, Deck
from models.stake import STAKES, Stake
from models.tournament import GameResult, GameState, Match, Round, Team, TourneySet


class MatchupError(Exception):
    """A rule violation. The message is shown to the user in Discord as-is."""


def _esc(text: str) -> str:
    """Escape Discord markdown so names like 'Team_1' don't turn into italics."""
    return re.sub(r"([\\*_`~|>])", r"\\\1", text)


# -- formatting ---------------------------------------------------------------

def format_team(team: Team) -> str:
    """'Name(player1, player2)'"""
    return f"{_esc(team.name)}({', '.join(_esc(p.username) for p in team.players)})"


def format_set(s: TourneySet) -> str:
    """'Team1(p1, p2) vs Team2(p1, p2) #ID'"""
    return f"{format_team(s.team1)} vs {format_team(s.team2)} #{s.set_id}"


def set_status(s: TourneySet) -> str:
    if s.channel_archived:
        channel = "channel archived"
    elif s.channel_id is not None:
        channel = "channel open"
    else:
        channel = "not started"
    winner = s.get_winner()
    outcome = f"won by {_esc(winner.name)}" if winner is not None else "no winner yet"
    return f"Bo{s.best_of}, score {'{}-{}'.format(*s.get_standings())}, {outcome}, {channel}"


def match_status(m: Match) -> str:
    if m.state == GameState.INIT:
        return "not started"
    if m.state == GameState.STARTED:
        return "in progress"
    outcomes = {
        GameResult.TEAM1_WIN: f"{_esc(m.team1.name)} won",
        GameResult.TEAM2_WIN: f"{_esc(m.team2.name)} won",
        GameResult.DRAW: "draw",
        GameResult.CANCELLED: "cancelled",
    }
    return "finished, " + outcomes.get(m.result, "no result recorded")


# -- lookups ------------------------------------------------------------------

def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def find_deck(name: str) -> Deck:
    deck = next((d for d in DECKS if _key(d.name) == _key(name)), None)
    if deck is None:
        raise MatchupError(f"Unknown deck **{_esc(name)}**. Decks: {', '.join(d.name for d in DECKS)}.")
    return deck


def find_stake(name: str) -> Stake:
    stake = next((s for s in STAKES if _key(s.name) == _key(name)), None)
    if stake is None:
        raise MatchupError(f"Unknown stake **{_esc(name)}**. Stakes: {', '.join(s.name for s in STAKES)}.")
    return stake


def require_current_round(t: ConjoinedTournament) -> Round:
    rnd = t.get_current_round()
    if rnd is None:
        raise MatchupError("The tournament hasn't been initialized yet. Use `!conjoined init`.")
    return rnd


def require_set(t: ConjoinedTournament, set_id: int) -> TourneySet:
    s = t.find_set(set_id)
    if s is None:
        raise MatchupError(f"There's no set with ID #{set_id}. Use `!conjoined list_matchups` to see them.")
    return s


# -- changing the tournament --------------------------------------------------

def initialize(t: ConjoinedTournament) -> None:
    """Creates stage 1 with its first round, and closes registration."""
    if t.stages:
        raise MatchupError("The tournament is already initialized.")
    if not t.is_full():
        raise MatchupError(f"Stage 1 needs exactly 16 teams, but {len(t.teams)} are registered.")
    t.stage_1_init()
    t.started = True


def next_round(t: ConjoinedTournament) -> Set[Team]:
    """Starts the next round (or stage) once the current round is finished.

    Returns the teams eliminated by this step.
    """
    require_current_round(t)
    before = set(t.eliminated_teams)
    try:
        t.next_round()
    except TournamentError as e:
        raise MatchupError(str(e)) from e
    return t.eliminated_teams - before


def why_no_match(s: TourneySet) -> str | None:
    """None if a new match may be added to the set, otherwise the reason it can't."""
    if s.get_winner() is not None:
        return "the set is already decided"
    if any(m.state != GameState.FINISHED for m in s.matches):
        return "it still has an unfinished match"
    return None


def add_match(s: TourneySet, deck: Deck, stake: Stake) -> Match:
    """Adds one match to one set (the building block for per-set commands)."""
    reason = why_no_match(s)
    if reason is not None:
        raise MatchupError(f"Can't add a match to set #{s.set_id}: {reason}.")
    return s.add_match(deck, stake)


def add_matches_all(t: ConjoinedTournament, deck: Deck, stake: Stake) -> Tuple[List[TourneySet], List[TourneySet]]:
    """Adds a match with this deck/stake to every set of the current round that needs one.

    Returns (sets that got a match, sets skipped). A set is skipped when it is already
    decided or still has an unfinished match, so running this twice never doubles up.
    """
    if deck in t.banned_decks:
        raise MatchupError(f"The **{deck}** deck is banned.")
    if stake in t.banned_stakes:
        raise MatchupError(f"The **{stake}** stake is banned.")

    added, skipped = [], []
    for s in require_current_round(t).matchups:
        if why_no_match(s) is None:
            s.add_match(deck, stake)
            added.append(s)
        else:
            skipped.append(s)
    return added, skipped


# -- standings ----------------------------------------------------------------

def progress_text(t: ConjoinedTournament) -> str:
    rnd = require_current_round(t)
    done = sum(1 for s in rnd.matchups if s.get_winner() is not None)
    stage = t.stages[t.cur_stage_idx]
    return f"Stage {t.cur_stage_idx + 1}, round {stage.cur_round_idx + 1} ({done}/{len(rnd.matchups)} sets finished)"


def standings_text(t: ConjoinedTournament) -> str:
    """Teams grouped by their set record (wins-losses), best first. Eliminated teams are left out."""
    require_current_round(t)
    groups = t.stages[t.cur_stage_idx].standings_to_teams()
    lines = []
    for (wins, losses), teams in sorted(groups.items(), key=lambda kv: (-kv[0][0], kv[0][1])):
        names = sorted((tm.name for tm in teams if tm not in t.eliminated_teams), key=str.casefold)
        if names:
            lines.append(f"**{wins}-{losses}**: " + ", ".join(_esc(n) for n in names))
    return "\n".join(lines) or "No teams left."


def team_names(teams) -> str:
    return ", ".join(_esc(n) for n in sorted((tm.name for tm in teams), key=str.casefold))


def eliminated_text(t: ConjoinedTournament) -> str:
    names = sorted((tm.name for tm in t.eliminated_teams), key=str.casefold)
    return ", ".join(_esc(n) for n in names) or "None yet"