"""What the OBS overlays show, computed for a moment in time ("feed time").

The casters' feeds run behind live, so the overlays describe what's on the feed, not what's
happening now: the team on a slot at feed time, and standings and set scores counting only results
recorded before feed time. Otherwise the overlay would give away results before viewers see them.
Results have timestamps in Match.history (see reports._set_result).
"""
from conjoined import ConjoinedTournament
from models.tournament import Team, TourneySet

TEAM1_WIN, TEAM2_WIN = "TEAM1_WIN", "TEAM2_WIN"


def result_at(match, t: float) -> str | None:
    """The match's result (a GameResult name) as it stood at time t."""
    if not match.history:  # no recorded changes: the current result has always been there
        return match.result.name if match.result is not None else None
    result = None
    for change in match.history:  # appended in order
        if change["at"] > t:
            break
        result = change["result"]
    return result


def set_score_at(s: TourneySet, t: float) -> tuple[int, int]:
    results = [result_at(m, t) for m in s.matches]
    return results.count(TEAM1_WIN), results.count(TEAM2_WIN)


def set_winner_at(s: TourneySet, t: float) -> Team | None:
    w1, w2 = set_score_at(s, t)
    if w1 > s.best_of // 2:
        return s.team1
    if w2 > s.best_of // 2:
        return s.team2
    return None


def stage_record_at(stage, team: Team, t: float) -> tuple[int, int]:
    """(set wins, set losses) of a team in one stage, counting results recorded up to t."""
    wins = losses = 0
    for rnd in stage.rounds:
        for s in rnd.matchups:
            if team not in (s.team1, s.team2):
                continue
            winner = set_winner_at(s, t)
            if winner is None:
                continue
            if winner == team:
                wins += 1
            else:
                losses += 1
    return wins, losses


def find_set(t: ConjoinedTournament, set_id: int):
    """(stage index, round index, set) or None."""
    for si, stage in enumerate(t.stages):
        for ri, rnd in enumerate(stage.rounds):
            for s in rnd.matchups:
                if s.set_id == set_id:
                    return si, ri, s
    return None


def round_at(t: ConjoinedTournament, set_starts: dict, at: float) -> tuple[int, int] | None:
    """(stage index, round index) of the latest set started up to `at`; the current round if no set
    start is known; None before the tournament is set up."""
    best = None
    for set_id, started in set_starts.items():
        if started <= at and (best is None or started > best[0]):
            found = find_set(t, int(set_id))
            if found:
                best = (started, found[0], found[1])
    if best:
        return best[1], best[2]
    if t.get_current_round() is None:
        return None
    return t.cur_stage_idx, t.stages[t.cur_stage_idx].cur_round_idx


def build_state(t: ConjoinedTournament, at: float, slot_teams: dict[str, str | None], set_starts: dict,
                display_name) -> dict:
    """Everything the overlays need, as of time `at`.

    slot_teams: slot -> team name on that slot at `at` (None: nobody, slate)
    display_name: Player -> name to show (e.g. their server display name)
    """
    teams = {team.name: team for team in t.teams}
    where = round_at(t, set_starts, at)
    state = {"round": None, "slots": {}}
    stage = rnd = None
    if where is not None:
        si, ri = where
        stage, rnd = t.stages[si], t.stages[si].rounds[ri]
        best_of = rnd.matchups[0].best_of if rnd.matchups else None
        state["round"] = {"stage": si + 1, "round": ri + 1, "best_of": best_of,
                          "label": f"Stage {si + 1} · Round {ri + 1}"}
    for slot, name in slot_teams.items():
        team = teams.get(name) if name else None
        if team is None:
            state["slots"][slot] = None
            continue
        players = [display_name(p) for p in team.players]
        entry = {"team": team.name, "p1": players[0] if players else "", "p2": players[1] if len(players) > 1 else ""}
        if stage is not None:
            wins, losses = stage_record_at(stage, team, at)
            entry.update(wins=wins, losses=losses, standing=f"{wins}-{losses}")
        s = next((x for x in rnd.matchups if team in (x.team1, x.team2)), None) if rnd else None
        if s is not None:
            w1, w2 = set_score_at(s, at)
            own, other = (w1, w2) if s.team1 == team else (w2, w1)
            entry.update(set_id=s.set_id, set_score=f"{own}-{other}", best_of=s.best_of)
        state["slots"][slot] = entry
    return state
