"""Substituting a player (!team sub), also after the tournament started.  Run from the repo root:  python -m pytest tests"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import dummies  # noqa: E402
import matchups  # noqa: E402
import registration as reg  # noqa: E402
from conjoined import ConjoinedTournament  # noqa: E402
from models.tournament import GameResult, Player  # noqa: E402
from persistence import TournamentStore  # noqa: E402


def _started():
    t = ConjoinedTournament()
    dummies.fill_dummy_teams(t, None)
    matchups.initialize(t)
    return t


def test_sub_in_for_one_round_and_back_keeps_the_team(tmp_path):
    t = _started()
    s = t.get_current_round().matchups[0]
    team = s.team1
    original, teammate = team.players
    sub = Player("123", "subby")
    with pytest.raises(reg.RegistrationError, match="already started"):
        reg.add_players(t, team, [sub])  # the normal commands stay closed

    reg.substitute_player(t, team, original, sub)
    assert [p.uid for p in team.players] == ["123", teammate.uid]  # takes the same place (p1)

    s.add_match(matchups.find_deck("Red"), matchups.find_stake("White"))
    s.matches[0].report_result(GameResult.TEAM1_WIN)
    store = TournamentStore(tmp_path)
    store._cache[1] = t
    store.save(1)
    t = TournamentStore(tmp_path).get(1)  # survives a restart, record and all
    team = reg.find_team(t, team.name)
    assert t.find_set(s.set_id).get_winner() is team

    reg.substitute_player(t, team, Player("123", "subby"), original)  # back for round 2
    assert [p.uid for p in team.players] == [original.uid, teammate.uid]


def test_sub_refuses_mistakes():
    t = _started()
    team, other = t.teams[0], t.teams[1]
    out = team.players[0]
    with pytest.raises(reg.RegistrationError, match="isn't on"):
        reg.substitute_player(t, team, Player("999", "nobody"), Player("123", "subby"))
    with pytest.raises(reg.RegistrationError, match="already on"):
        reg.substitute_player(t, team, out, other.players[0])  # can't take a player from another team
    with pytest.raises(reg.RegistrationError, match="already on"):
        reg.substitute_player(t, team, out, team.players[1])  # nor the teammate
    with pytest.raises(reg.RegistrationError, match="same player"):
        reg.substitute_player(t, team, out, out)
    assert team.players[0] is out  # nothing changed
