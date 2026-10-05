"""Deck/stake bans: !selection ban commands, and pullrandom banning what it pulls.  Run from the repo root:  python -m pytest tests"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import matchups  # noqa: E402
from cogs.selection import Selection, SelectionError  # noqa: E402
from conjoined import ConjoinedTournament, VoteResults  # noqa: E402
from models.deck import DECKS  # noqa: E402
from models.stake import STAKES  # noqa: E402
from persistence import TournamentStore  # noqa: E402


class Ctx:
    def __init__(self):
        self.guild = SimpleNamespace(id=1, emojis=[])
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content if content is not None else kwargs.get("embed").description)


@pytest.fixture
def env(tmp_path):
    store = TournamentStore(tmp_path)
    store._cache[1] = ConjoinedTournament()
    return Selection(SimpleNamespace(store=store)), Ctx(), store, tmp_path


def run(command, cog, ctx, *args, **kwargs):
    return asyncio.run(command.callback(cog, ctx, *args, **kwargs))


def saved(tmp_path) -> ConjoinedTournament:
    return TournamentStore(tmp_path).get(1)


def test_ban_and_unban_commands(env):
    cog, ctx, store, tmp_path = env
    run(Selection.bandeck, cog, ctx, deck="red")
    run(Selection.banstake, cog, ctx, stake="  GOLD ")
    assert [d.name for d in saved(tmp_path).banned_decks] == ["Red"]
    assert [s.name for s in saved(tmp_path).banned_stakes] == ["Gold"]
    assert "Banned decks: **Red** deck" in ctx.sent[-1]
    with pytest.raises(SelectionError, match="already banned"):
        run(Selection.bandeck, cog, ctx, deck="Red")
    with pytest.raises(SelectionError, match="Unknown deck"):
        run(Selection.bandeck, cog, ctx, deck="Purple")
    with pytest.raises(matchups.MatchupError, match="Red.*banned"):
        matchups.add_matches_all(store.get(1), DECKS[0], STAKES[0])  # also refused when adding matches
    run(Selection.unbandeck, cog, ctx, deck="Red")
    with pytest.raises(SelectionError, match="not banned"):
        run(Selection.unbandeck, cog, ctx, deck="Red")
    run(Selection.unbanall, cog, ctx)
    assert not saved(tmp_path).banned_decks and not saved(tmp_path).banned_stakes
    with pytest.raises(SelectionError, match="Nothing is banned"):
        run(Selection.unbanall, cog, ctx)


def test_pullrandom_bans_the_pulled_deck_and_stake_and_skips_banned_options(env):
    cog, ctx, store, tmp_path = env
    t = store.get(1)
    # option 1 shares option 2's deck, option 3 shares option 2's stake, option 4 is unrelated
    a, b, c = DECKS[0], DECKS[1], DECKS[2]
    x, y = STAKES[0], STAKES[1]
    results = VoteResults([(a, x), (a, y), (b, y), (c, x)])
    results.votes = [0, 5, 0, 0]
    t.vote_results.append(results)

    run(Selection.pullrandom, cog, ctx)  # only option 2 has votes
    assert saved(tmp_path).banned_decks == [a] and saved(tmp_path).banned_stakes == [y]
    assert "are banned from now on" in ctx.sent[-1]

    with pytest.raises(SelectionError, match="banned deck or stake"):
        run(Selection.pullrandom, cog, ctx)  # the only voted option is banned now
    results.votes = [3, 5, 4, 2]  # 1 (deck a), 2 and 3 (stake y) are banned: only option 4 is left
    run(Selection.pullrandom, cog, ctx)
    assert saved(tmp_path).banned_decks == [a, c] and saved(tmp_path).banned_stakes == [y, x]


def test_votes_offer_distinct_unbanned_combos_only():
    t = ConjoinedTournament()
    t.banned_decks = DECKS[2:]  # two decks left
    t.banned_stakes = STAKES[3:]  # three stakes left
    for _ in range(50):
        selection = t.generate_selection(9)
        assert len(selection) == len(set(selection)) == 6
        assert all(d in DECKS[:2] and s in STAKES[:3] for d, s in selection)
    t.banned_stakes = list(STAKES)
    assert t.generate_selection(9) == []
