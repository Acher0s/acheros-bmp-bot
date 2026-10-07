"""Overlay data as of feed time.  Run from the repo root:  python -m pytest tests"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dummies  # noqa: E402
import matchups  # noqa: E402
import overlays  # noqa: E402
from conjoined import ConjoinedTournament  # noqa: E402
from models.tournament import GameResult, GameState  # noqa: E402

T0 = 1_000_000.0


def _tournament():
    t = ConjoinedTournament()
    dummies.fill_dummy_teams(t, None)
    matchups.initialize(t)
    return t


def _win(match, team1_wins: bool, at: float):
    match.result = GameResult.TEAM1_WIN if team1_wins else GameResult.TEAM2_WIN
    match.state = GameState.FINISHED
    match.history.append({"at": at, "by": "test", "action": "manual", "result": match.result.name})


def test_results_only_count_once_recorded():
    t = _tournament()
    s = t.get_current_round().matchups[0]
    s.add_match(matchups.find_deck("Red"), matchups.find_stake("White"))
    _win(s.matches[0], True, T0 + 100)
    assert overlays.set_score_at(s, T0 + 50) == (0, 0)  # feed hasn't reached the result yet
    assert overlays.set_score_at(s, T0 + 150) == (1, 0)


def test_correction_is_followed_in_time():
    t = _tournament()
    s = t.get_current_round().matchups[0]
    s.add_match(matchups.find_deck("Red"), matchups.find_stake("White"))
    m = s.matches[0]
    _win(m, True, T0 + 100)
    _win(m, False, T0 + 200)  # corrected later
    assert overlays.result_at(m, T0 + 150) == "TEAM1_WIN"
    assert overlays.result_at(m, T0 + 250) == "TEAM2_WIN"


def test_standing_and_state():
    t = _tournament()
    rnd = t.get_current_round()
    s = rnd.matchups[0]
    best_of = s.best_of
    for n in range(best_of // 2 + 1):  # team1 wins the set
        s.add_match(matchups.find_deck("Red"), matchups.find_stake("White"))
        _win(s.matches[n], True, T0 + 100 * (n + 1))
    done_at = T0 + 100 * (best_of // 2 + 1)
    stage = t.stages[0]
    assert overlays.stage_record_at(stage, s.team1, done_at - 1) == (0, 0)
    assert overlays.stage_record_at(stage, s.team1, done_at) == (1, 0)
    assert overlays.stage_record_at(stage, s.team2, done_at) == (0, 1)

    slot_teams = {"s1t1": s.team1.name, "s1t2": s.team2.name, "s2t1": None}
    state = overlays.build_state(t, done_at, slot_teams, {str(s.set_id): T0}, lambda p: p.username.upper())
    a, b = state["slots"]["s1t1"], state["slots"]["s1t2"]
    assert state["round"]["label"] == "Stage 1 · Round 1"
    assert a["team"] == s.team1.name and a["standing"] == "1-0" and b["standing"] == "0-1"
    assert a["p1"] == s.team1.players[0].username.upper()
    assert a["players"] == " & ".join(p.username.upper() for p in s.team1.players)
    assert a["set_score"] == f"{best_of // 2 + 1}-0" and b["set_score"] == f"0-{best_of // 2 + 1}"
    assert state["slots"]["s2t1"] is None


def test_round_at_uses_set_start_times():
    t = _tournament()
    first = t.get_current_round().matchups[0]
    assert overlays.round_at(t, {str(first.set_id): T0}, T0 + 10) == (0, 0)
    assert overlays.round_at(t, {}, T0) == (0, 0)  # unknown: current round
