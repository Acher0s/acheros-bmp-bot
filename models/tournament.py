import random
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
        self.role_id: int | None = None  # Discord role shared by all members of the team

    def __str__(self):
        return f"{self.name}({", ".join([str(p) for p in self.players])})"

    def __repr__(self):
        return self.__str__()

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
        self.state = GameState.FINISHED


class TourneySet:
    def __init__(self, team1, team2, best_of):
        self.best_of: int = best_of
        self.team1: Team = team1
        self.team2: Team = team2
        self.matches: List[Match] = []

    def __str__(self, tabs=0):
        t1_score, t2_score = self.get_standings()
        return "\t" * tabs + f"{self.team1} VS {self.team2} {t1_score} - {t2_score} (Bo{self.best_of})" + (f"\tWINNER: {self.get_winner()}" if self.get_winner() is not None else "")

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

    def __str__(self, tabs=0):
        return ("\t" * tabs + f"Round(\n" +
                ",\n".join([m.__str__(tabs=tabs+1) for m in self.matchups]) +
                "\n" + "\t" * tabs + ")")

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

    def __str__(self, tabs=0):
        return ("\t" * tabs + "Stage(" +
                f"\n{"\n".join([f"{r.__str__(tabs=tabs + 1)}" for r in self.rounds])}" +
                "\n" + "\t" * tabs + ")"
                )

    def __repr__(self):
        return str(self)

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

        for team in self.rounds[0].get_teams():
            team_standing = (self.get_num_set_wins(team), self.get_num_set_losses(team))
            if res.get(team_standing) is None:
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

    def get_played_history(self) -> Dict[Team, Set[Team]]:
        """Returns a lookup dictionary of sets of past opponents for each team."""
        history = {}
        for r in self.rounds:
            for m in r.matchups:
                if m.team1 not in history:
                    history[m.team1] = set()
                if m.team2 not in history:
                    history[m.team2] = set()

                history[m.team1].add(m.team2)
                history[m.team2].add(m.team1)
        return history

def count_rematches(pairing: List[Tuple[Team, Team]], played_history: Dict[Team, Set[Team]]) -> int:
    """Calculates total repeat matches in a candidate pairing."""
    repeats = 0
    for t1, t2 in pairing:
        if t2 in played_history.get(t1, set()):
            repeats += 1
    return repeats


def match_bracket_group(teams: List[Team], played_history: Dict[Team, Set[Team]]) -> List[Tuple[Team, Team]]:
    """
    Pairs a pool of teams, prioritizing zero rematches.
    Uses randomized backtracking for speed.
    """
    shuffled_teams = teams.copy()
    random.shuffle(shuffled_teams)

    best_pairing = []
    min_rematches = float('inf')

    def backtrack(remaining: List[Team], current_pairing: List[Tuple[Team, Team]], current_cost: int):
        nonlocal best_pairing, min_rematches

        # Prune if this branch is already worse than our best solution
        if current_cost >= min_rematches:
            return

        if not remaining:
            if current_cost < min_rematches:
                min_rematches = current_cost
                best_pairing = list(current_pairing)
            return

        first = remaining[0]
        rest = remaining[1:]

        # Randomize choice of opponents to keep tournament random
        candidates = list(rest)
        random.shuffle(candidates)

        for opponent in candidates:
            is_repeat = 1 if opponent in played_history.get(first, set()) else 0

            next_remaining = [t for t in rest if t != opponent]
            backtrack(next_remaining, current_pairing + [(first, opponent)], current_cost + is_repeat)

            # Optimization: If we found a zero-rematch pairing, stop immediately
            if min_rematches == 0:
                return

    backtrack(shuffled_teams, [], 0)
    return best_pairing