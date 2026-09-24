from enum import Enum
from typing import List, Tuple, Dict, Set

from models.deck import Deck
from models.stake import Stake


class Player:
    def __init__(self, uid, name):
        self.uid: str = uid
        self.username: str = name

    def __eq__(self, other):
        return self.uid == other.uid


class Team:
    def __init__(self, name):
        self.name = name
        self.players: List[Player] = []

    def add_player(self, player: Player):
        if player not in self.players:
            self.players.append(player)

    def remove_player(self, player: Player):
        if player in self.players:
            self.players.remove(player)

    def is_empty(self):
        return len(self.players) == 0


class GameState(Enum):
    INIT = 0
    STARTED = 1
    FINISHED = 2


class GameResult(Enum):
    TEAM1_WIN = 0
    TEAM2_WIN = 1
    DRAW = 2
    CANCELLED = 3


class Match:
    def __init__(self, team1, team2, deck, stake):
        self.team1: Team = team1
        self.team2: Team = team2
        self.deck: Deck = deck
        self.stake: Stake = stake
        self.state: GameState = GameState.INIT
        self.result: GameResult | None = None

    def report_result(self, res: GameResult):
        self.result = res


class TourneySet:
    def __init__(self, team1, team2, best_of):
        self.best_of: int = best_of
        self.team1: Team = team1
        self.team2: Team = team2
        self.matches: List[Match] = []

    def get_standings(self) -> Tuple[int, int]:
        res = [0, 0]
        for match in self.matches:
            if match.result == GameResult.TEAM1_WIN:
                res[0] += 1
            if match.result == GameResult.TEAM2_WIN:
                res[1] += 1

        return res[0], res[1]

    def get_winner(self) -> Team | None:
        for i, score in enumerate(self.get_standings()):
            if score > self.best_of // 2:
                if i == 0:
                    return self.team1
                return self.team2
            return None

    def get_loser(self):
        if self.get_winner() is None:
            return None
        if self.get_winner() == self.team1:
            return self.team2
        return self.team1


class Round:
    def __init__(self, matchups=None):
        self.matchups: List[TourneySet] = matchups if matchups is not None else []

    def is_finished(self) -> bool:
        for _set in self.matchups:
            if _set.get_winner() is None:
                return False
        return True

    def get_winning_teams(self):
        res = []
        for m in self.matchups:
            if m.get_winner() is not None:
                res.append(m.get_winner())

        return res

    def get_losing_teams(self):
        res = []
        for m in self.matchups:
            if m.get_loser() is not None:
                res.append(m.get_loser())

        return res

    def get_teams(self):
        res = set()
        for m in self.matchups:
            res.add(m.team1)
            res.add(m.team2)

        return res



class Stage:
    def __init__(self):
        self.cur_round_idx: int = 0
        self.rounds: List[Round] = []

    def _validate_round(self, r: Round) -> (True, str):
        seen = set()
        for _set in r.matchups:
            if _set.team1 is None or _set.team2 is None:
                return False, f"team is None"
            if _set.team1 == _set.team2:
                return False, f"{_set.team1} vs {_set.team2} is invalid"
            if _set.team1 in seen:
                return False, f"{_set.team1} is playing more than once in round"
            if _set.team2 in seen:
                return False, f"{_set.team2} is playing more than once in round"

            seen.add(_set.team1)
            seen.add(_set.team2)

        return True, ""

    def add_round(self, r: Round):
        isvalid, err = self._validate_round(r)

        if isvalid:
            self.rounds.append(r)
        else:
            print(f"Round invalid: {err}")

    def get_current_round(self) -> Round:
        return self.rounds[self.cur_round_idx]

    def get_num_set_wins(self, team: Team) -> int:
        res = 0
        for r in self.rounds:
            if team in r.get_winning_teams():
                res += 1
        return res

    def get_num_set_losses(self, team: Team) -> int:
        res = 0
        for r in self.rounds:
            if team in r.get_losing_teams():
                res += 1
        return res

    def standings_to_teams(self) -> Dict[Tuple[int, int], List[Team]]:
        res = {}

        current_round = self.get_current_round()
        for team in current_round.get_teams():
            team_standing = (self.get_num_set_wins(team), self.get_num_set_losses(team))
            if res.get(team_standing) is not None:
                res[team_standing] = [team]
            else:
                res[team_standing].append(team)

        return res

    def played_opponents(self, team: Team):
        res = []
        for i in range(self.cur_round_idx):
            r = self.rounds[i]
            for m in r.matchups:
                if m.team1 == team:
                    res.append(m.team2)
                if m.team2 == team:
                    res.append(m.team1)

        return res

def count_rematches(pairing: List[Tuple[Team, Team]], played_history: Dict[Team, Set[Team]]) -> int:
    """Calculates total repeat matches in a candidate pairing."""
    repeats = 0
    for t1, t2 in pairing:
        if t2 in played_history.get(t1, set()):
            repeats += 1
    return repeats
