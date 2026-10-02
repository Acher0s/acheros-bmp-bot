import random
from typing import Callable, Dict, List, Tuple, Set

from models.deck import DECKS, Deck
from models.stake import STAKES, Stake
from models.tournament import Team, Stage, Round, TourneySet, match_bracket_group, GameResult, Match

MAX_SELECTION_RETRIES = 10
STAGE_1_ROUNDS = 4


class TournamentError(Exception):
    """A tournament rule stops this action. The message says why and can be shown to users."""


class ConjoinedTournament:
    def __init__(self, teams=None):
        self.banned_decks: List[Deck] = []
        self.banned_stakes: List[Stake] = []
        self.teams: List[Team] = [] if teams is None else teams
        self.eliminated_teams: set[Team] = set()
        self.started: bool = False
        self.stages: List[Stage] = []
        self.cur_stage_idx: int = 0
        self.vote_results: List[VoteResults] = []
        self.next_set_id: int = 1

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

    def get_active_teams(self) -> List[Team]:
        """Teams still in the tournament."""
        return [team for team in self.teams if team not in self.eliminated_teams]

    def get_voting_role_ids(self) -> Set[int]:
        """Discord role ids that grant voting rights (one per active team)."""
        return {team.role_id for team in self.get_active_teams() if team.role_id is not None}

    def new_set(self, team1: Team, team2: Team, best_of: int) -> TourneySet:
        """Creates a set with the next free ID."""
        s = TourneySet(team1, team2, best_of=best_of, set_id=self.next_set_id)
        self.next_set_id += 1
        return s

    def get_current_round(self) -> Round | None:
        """None until the tournament is initialized."""
        if self.cur_stage_idx >= len(self.stages):
            return None
        stage = self.stages[self.cur_stage_idx]
        if stage.cur_round_idx >= len(stage.rounds):
            return None
        return stage.get_current_round()

    def find_set(self, set_id: int) -> TourneySet | None:
        for stage in self.stages:
            for r in stage.rounds:
                for s in r.matchups:
                    if s.set_id == set_id:
                        return s
        return None

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


    def real_team_check(self) -> Callable[[Team], bool] | None:
        """While testing with dummy teams: tells real teams apart, so pairing keeps them
        together. None when there are no dummy teams (normal, fully random pairing)."""
        from dummies import is_dummy_uid, is_real_team  # imported here: dummies imports this module
        if not any(is_dummy_uid(p.uid) for team in self.teams for p in team.players):
            return None
        return is_real_team

    def stage_1_init(self):
        s = Stage()

        # All 16 teams paired randomly
        pairings = match_bracket_group(self.teams, {}, self.real_team_check())
        r1 = Round([self.new_set(t1, t2, best_of=1) for t1, t2 in pairings])

        s.add_round(r1)

        self.stages.append(s)

    def next_round(self):
        """Starts the next round, moving on to the next stage once the current one is over.

        Stage 1: Bo1 swiss, STAGE_1_ROUNDS rounds, 2 set losses and you're out.
        Stage 2: the 3-1 teams play Bo3 semi-finals, then the winners play a Bo3 final (loser: 3rd).
        Stage 3: Bo5 final between the stage 2 winner and the undefeated team of stage 1.
        """
        rnd = self.get_current_round()
        if rnd is None:
            raise TournamentError("The tournament hasn't been initialized yet.")
        if not rnd.is_finished():
            raise TournamentError("The current round hasn't finished yet.")

        stage = self.stages[self.cur_stage_idx]
        if self.cur_stage_idx == 0:
            if stage.cur_round_idx + 1 < STAGE_1_ROUNDS:
                self._stage_1_next_round()
            else:
                self._stage_2_init()
        elif self.cur_stage_idx == 1:
            if stage.cur_round_idx == 0:
                self._stage_2_final()
            else:
                self._stage_3_init()
        else:
            raise TournamentError("The tournament is over.")

    def _stage_1_next_round(self):
        stage: Stage = self.stages[0]

        # Teams with 2 set losses are out; everyone else is paired within their (wins, losses) bracket.
        eliminated = set()
        brackets = {}
        for (wins, losses), bracket_teams in stage.standings_to_teams().items():
            if losses >= 2:
                eliminated.update(bracket_teams)
            else:
                brackets[(wins, losses)] = bracket_teams

        # Check everything before changing anything: an odd bracket can't be fully paired,
        # and match_bracket_group would silently leave its teams out.
        odd = [standing for standing, bracket_teams in brackets.items() if len(bracket_teams) % 2]
        if odd:
            raise TournamentError(f"Can't pair records with an odd number of teams: {odd}")

        played_history = stage.get_played_history()

        next_round_matchups = []

        for standing, bracket_teams in brackets.items():
            # Pair teams within the same score bracket while minimizing rematches
            pairings = match_bracket_group(bracket_teams, played_history, self.real_team_check())

            for t1, t2 in pairings:
                next_round_matchups.append(self.new_set(t1, t2, best_of=1))

        self.eliminated_teams |= eliminated
        stage.add_round(Round(next_round_matchups))
        stage.cur_round_idx = len(stage.rounds) - 1

    def stage_1_undefeated(self) -> List[Team]:
        return self.stages[0].standings_to_teams().get((STAGE_1_ROUNDS, 0), [])

    def _stage_2_init(self):
        standings = self.stages[0].standings_to_teams()
        qualifiers = standings.get((STAGE_1_ROUNDS - 1, 1), [])
        if len(qualifiers) != 4 or len(self.stage_1_undefeated()) != 1:
            raise TournamentError(f"Stage 1 should end with 1 team at {STAGE_1_ROUNDS}-0 and 4 at "
                                  f"{STAGE_1_ROUNDS - 1}-1, but the records don't match that.")

        # Pair randomly, avoiding repeat matchups from stage 1 where possible.
        pairings = match_bracket_group(qualifiers, self.stages[0].get_played_history(), self.real_team_check())
        s = Stage()
        s.add_round(Round([self.new_set(t1, t2, best_of=3) for t1, t2 in pairings]))

        self.eliminated_teams |= {team for (wins, losses), teams in standings.items() if losses >= 2 for team in teams}
        self.stages.append(s)
        self.cur_stage_idx = 1

    def _stage_2_final(self):
        stage = self.stages[1]
        semis = stage.get_current_round()
        t1, t2 = semis.get_winning_teams()
        stage.add_round(Round([self.new_set(t1, t2, best_of=3)]))
        self.eliminated_teams |= set(semis.get_losing_teams())
        stage.cur_round_idx = len(stage.rounds) - 1

    def _stage_3_init(self):
        stage_2_final = self.stages[1].get_current_round().matchups[0]
        s = Stage()
        s.add_round(Round([self.new_set(stage_2_final.get_winner(), self.stage_1_undefeated()[0], best_of=5)]))
        self.eliminated_teams.add(stage_2_final.get_loser())  # 3rd place
        self.stages.append(s)
        self.cur_stage_idx = 2

    def get_placements(self) -> Dict[int, Team]:
        """The places decided so far: 3rd once stage 2 is done, 1st and 2nd once the final is."""
        res = {}
        if len(self.stages) > 1 and len(self.stages[1].rounds) > 1:
            third = self.stages[1].rounds[1].matchups[0].get_loser()
            if third is not None:
                res[3] = third
        if len(self.stages) > 2:
            final = self.stages[2].rounds[0].matchups[0]
            if final.get_winner() is not None:
                res[1], res[2] = final.get_winner(), final.get_loser()
        return res

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