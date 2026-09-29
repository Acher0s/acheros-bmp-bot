import random
from typing import List, Tuple, Set

from models.deck import DECKS, Deck
from models.stake import STAKES, Stake
from models.tournament import Team, Stage, Round, TourneySet, match_bracket_group, GameResult, Match

MAX_SELECTION_RETRIES = 5

class ConjoinedTournament:
    def __init__(self, teams=None):
        self.banned_decks: List[Deck] = []
        self.banned_stakes: List[Stake] = []
        self.teams: List[Team] = [] if teams is None else teams
        self.started: bool = False
        self.stages: List[Stage] = []
        self.cur_stage_idx: int = 0

    def __str__(self, tabs=0):
        return "\t" * tabs + "ConjoinedTournament(\n\t" + ("\n" + "\t" * (tabs + 1)).join([
            f"banned_decks={self.banned_decks}",
            f"banned_stakes={self.banned_stakes}",
            f"teams={str(self.teams)}",
            f"started={self.started}",
            f"stages=[\n" + ",\n".join([s.__str__(tabs=tabs + 2) for s in self.stages]) + "\n" + "\t" * (tabs + 1) + "]"
        ]) + "\n" + "\t" * tabs + ")"

    def is_full(self) -> bool:
        return len(self.teams) == 16

    def add_team(self, team: Team):
        if self.is_full():
            print("Tournament is already full")
            return

        if team.is_empty():
            print("Team is empty")

        self.teams.append(team)

    def generate_selection(self, n=9) -> List[Tuple[Deck, Stake]]:
        valid_decks = [deck for deck in DECKS if deck not in self.banned_decks]
        valid_stakes = [stake for stake in STAKES if stake not in self.banned_stakes]

        selection = []

        i = 0
        retries = 0
        while i < n:
            deck: Deck = random.choice(valid_decks)
            stake: Stake = random.choice(valid_stakes)

            combo = (deck, stake)
            if combo in selection and retries < MAX_SELECTION_RETRIES:
                print(combo, selection)
                retries += 1
            else:
                selection.append(combo )

                selection.sort(key=lambda x: x[0])  # Sort by deck first
                selection.sort(key=lambda x: x[1])  # Sort by stake second

                i+=1

        return selection


    def stage_1_init(self):
        s = Stage()

        remaining_teams = self.teams.copy()
        r1_matchups = []
        for i in range(8):
            t1 = random.choice(remaining_teams)
            remaining_teams.remove(t1)
            t2 = random.choice(remaining_teams)
            remaining_teams.remove(t2)

            r1_matchups.append(TourneySet(t1, t2, best_of=1))

        r1 = Round(r1_matchups)

        s.add_round(r1)

        self.stages.append(s)

    def stage_1_next_round(self):
        if self.cur_stage_idx != 0:
            print('Current stage is not stage 1')
            return

        stage: Stage = self.stages[self.cur_stage_idx]
        if not stage.get_current_round().is_finished():
            print('Current round has not yet finished')


        remaining_teams = [team for team in self.teams if stage.get_num_set_losses(team) < 2]

        standings2teams = stage.standings_to_teams()

        # remove teams with 2 losses
        standings2teams[(0,2)] = []
        standings2teams[(1,2)] = []
        standings2teams[(2,2)] = []
        standings2teams[(3,2)] = []

        played_history = stage.get_played_history()

        next_round_matchups = []

        for standing, bracket_teams in standings2teams.items():
            # Pair teams within the same score bracket while minimizing rematches
            pairings = match_bracket_group(bracket_teams, played_history)

            for t1, t2 in pairings:
                next_round_matchups.append(TourneySet(t1, t2, best_of=1))

        new_round = Round(next_round_matchups)
        stage.add_round(new_round)
        stage.cur_round_idx += 1

class VoteResults:
    def __init__(self, selection):
        self.selection: List[Tuple[Deck, Stake]] = selection
        self.votes = [0] * len(selection)

    def count_votes(self) -> int:
        return sum(self.votes)

    def fill_dummy_votes(self, n = 32):
        for i in range(n - self.count_votes()):
            self.votes[random.randrange(0, len(self.selection))] += 1

    def random_weighted_selection(self):
        weighted_arr = []
        for combo, votes in zip(self.selection, self.votes):
            weighted_arr += [combo] * votes

        return random.choice(weighted_arr)

    def __str__(self, tabs=0) -> str:
        res = ""
        for (deck, stake), votes in zip(self.selection, self.votes):
            res += "\t" * tabs
            res += f"{deck} / {stake}: {votes}/{self.count_votes()}" + (f" ({votes/self.count_votes() * 100:0.2f}%)" if self.count_votes() > 0 else "")
            res += "\n"

        return res


def dummy_resolve_cur_round(t: ConjoinedTournament):
    cur_stage = t.stages[t.cur_stage_idx]
    cur_round = cur_stage.get_current_round()

    for s in cur_round.matchups:
        for m in s.matches:
            if m.result is None:
                m.report_result(random.choice([GameResult.TEAM1_WIN, GameResult.TEAM2_WIN]))

        while s.get_winner() is None:
            random_match = Match(team1=s.team1, team2=s.team2, deck=random.choice(DECKS), stake=random.choice(STAKES))
            random_match.report_result(random.choice([GameResult.TEAM1_WIN, GameResult.TEAM2_WIN]))
            s.matches.append(random_match)






if __name__ == "__main__":
    teams = [Team(f"Team{i}") for i in range(16)]
    t = ConjoinedTournament(teams=teams)

    sel = t.generate_selection()

    voteres = VoteResults(sel)

    voteres.fill_dummy_votes()

    print(voteres)