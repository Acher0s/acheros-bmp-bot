"""Archiving set channels once their set is decided (next_round).  Run from the repo root:  python -m pytest tests"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dummies  # noqa: E402
import match_channels  # noqa: E402
import matchups  # noqa: E402
from conjoined import ConjoinedTournament  # noqa: E402
from models.tournament import GameResult  # noqa: E402
from persistence import TournamentStore  # noqa: E402


class Target:
    """A role or member in a channel's permission overwrites."""
    def __init__(self, target_id):
        self.id = target_id


class FakeCategory:
    def __init__(self, name, channels=()):
        self.name, self.channels = name, list(channels)


class FakeChannel:
    def __init__(self, channel_id, overwrites):
        self.id, self.name, self.topic, self.overwrites = channel_id, f"set-{channel_id}", "[conjoined-set:1]", overwrites
        self.category = None

    async def edit(self, name, topic, category, overwrites, reason):
        self.name, self.topic, self.overwrites = name, topic, overwrites
        if self.category is not None:
            self.category.channels.remove(self)
        self.category = category
        category.channels.append(self)


class FakeGuild:
    def __init__(self, categories):
        self.id, self.categories, self.channels = 1, categories, {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def create_category(self, name, reason):
        category = FakeCategory(name)
        self.categories.append(category)
        return category


def test_decided_sets_lose_their_teams_and_go_to_an_archive_with_room(tmp_path):
    store = TournamentStore(tmp_path)
    t = store._cache[1] = ConjoinedTournament()
    dummies.fill_dummy_teams(t, None)
    matchups.initialize(t)
    sets = t.get_current_round().matchups
    for n, s in enumerate(sets):
        s.team1.role_id, s.team2.role_id = 1000 + 2 * n, 1001 + 2 * n
        s.channel_id = 500 + n
    decided, playing = sets[0], sets[1]
    decided.add_match(matchups.find_deck("Red"), matchups.find_stake("White"))
    decided.matches[0].report_result(GameResult.TEAM1_WIN)
    assert match_channels.sets_to_archive(t) == [decided.set_id]  # undecided sets stay open

    full = FakeCategory(match_channels.ARCHIVE_CATEGORY, [object()] * match_channels.CATEGORY_LIMIT)
    guild = FakeGuild([full])
    everyone, bot = Target(1), Target(2)
    team1, team2 = Target(decided.team1.role_id), Target(decided.team2.role_id)
    channel = guild.channels[decided.channel_id] = FakeChannel(decided.channel_id, {
        everyone: "deny", bot: "allow", team1: "allow", team2: "allow"})

    assert asyncio.run(match_channels.archive_set(store, guild, decided.set_id))
    assert set(channel.overwrites) == {everyone, bot}  # the teams can't see it anymore; managers still can
    assert channel.name.startswith("archived-")
    assert channel.category is not full and channel.category.name == match_channels.ARCHIVE_CATEGORY
    assert TournamentStore(tmp_path).get(1).find_set(decided.set_id).channel_archived
    assert match_channels.sets_to_archive(t) == []
    assert not playing.channel_archived
