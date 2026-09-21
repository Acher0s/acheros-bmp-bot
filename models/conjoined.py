import random
from typing import List, Tuple

from models.deck import DECKS, Deck
from models.stake import STAKES, Stake
from models.tournament import Team, Stage, Round, Set


class ConjoinedTournament:
    def __init__(self):
        self.banned_decks: List[Deck] = []
        self.banned_stakes: List[Stake] = []
        self.teams: List[Team] = []
        self.started: bool = False
        self.stages: List[Stage] = []
        self.cur_stage_idx: int = 0

    def is_full(self) -> bool:
        return len(self.teams) == 16

    def add_team(self, team: Team):
        if self.is_full():
            print("Tournament is already full")
            return

        if team.is_empty():
            print("Team is empty")

    def generate_selection(self, n=9) -> List[Tuple[Deck, Stake]]:
        valid_decks = [deck for deck in DECKS if deck not in self.banned_decks]
        valid_stakes = [stake for stake in STAKES if stake not in self.banned_stakes]

        selection = []

        for i in range(n):
            deck: Deck = random.choice(valid_decks)
            stake: Stake = random.choice(valid_stakes)

            selection.append((deck, stake))

            selection.sort(key=lambda x: x[0])  # Sort by deck first
            selection.sort(key=lambda x: x[1])  # Sort by stake second

        return selection

    def choose_deck_stake(self, selection: List[Tuple[Deck, Stake]], votes: List[int]):
        assert len(selection) == len(votes)
        weighted = [combo * votes[i] for i, combo in enumerate(selection)]

        return random.choice(weighted)


    def stage_1_init(self):
        s = Stage()

        remaining_teams = self.teams.copy()
        r1_matchups = []
        for i in range(8):
            t1 = random.choice(remaining_teams)
            remaining_teams.remove(t1)
            t2 = random.choice(remaining_teams)
            remaining_teams.remove(t2)

            r1_matchups.append(Set(t1, t2, best_of=1))

        r1 = Round(r1_matchups)

        s.add_round(r1)

    def stage_1_next_round(self):
        if self.cur_stage_idx != 0:
            print('Current stage is not stage 1')
            return

        stage: Stage = self.stages[self.cur_stage_idx]
        if not stage.get_current_round().is_finished():
            print('Current round has not yet finished')


        remaining_teams = [team for team in self.teams if stage.get_num_set_losses(team) < 2]


        standings2teams = stage.standings_to_teams()












if __name__ == "__main__":
    t = ConjoinedTournament()

    print([f"{d} / {s}" for d, s in t.generate_selection()])


