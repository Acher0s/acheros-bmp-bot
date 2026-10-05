"""Team names up to 64 characters with spaces and special characters.  Run from the repo root:  python -m pytest tests"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import discord_text  # noqa: E402
import match_channels  # noqa: E402
import matchups  # noqa: E402
import registration as reg  # noqa: E402
from cogs import report_cog, stream_cog  # noqa: E402
from conjoined import ConjoinedTournament  # noqa: E402
from models.tournament import Player, TourneySet  # noqa: E402
from persistence import TournamentStore  # noqa: E402

# 64 characters each: spaces, markdown characters, quotes, accents, other alphabets, emoji
NAMES = [
    "The *Very* Long_Team ~Name~ `with` | pipes > and \\ backslashes!!",
    '"Quoted" Team & Co. <script>alert(1)</script> =SUM(A1) @everyone',
    "Équipe Ünïcödé ñandú — Команда Ромашка 日本チーム 🃏🔥 Jimbo's Jokers",
    "=HYPERLINK(\"http://example.com\") + -1 @here #1 set 12 none team03",
]
NAMES += [f"Team {n:02d} with a rather long name that fills up all sixty-four characters" for n in range(5, 17)]
NAMES = [n[:64] for n in NAMES]


def _registered() -> ConjoinedTournament:
    t = ConjoinedTournament()
    for i, name in enumerate(NAMES):
        reg.create_team(t, name, [Player(str(100 + 2 * i), f"p{i}a"), Player(str(101 + 2 * i), f"p{i}b")])
    return t


def test_names_up_to_64_characters_are_accepted_as_typed():
    assert all(len(n) <= 64 for n in NAMES) and max(len(n) for n in NAMES) == 64
    t = _registered()
    assert [tm.name for tm in t.teams] == NAMES
    assert reg.find_team(t, "  the *very*   LONG_team ~name~ `with` | pipes > and \\ backslashes!! ") is t.teams[0]
    with pytest.raises(reg.RegistrationError):
        reg.create_team(ConjoinedTournament(), "x" * 65, [])


def test_names_survive_saving_and_loading(tmp_path):
    store = TournamentStore(tmp_path)
    store._cache[1] = _registered()
    store.save(1)
    assert [tm.name for tm in TournamentStore(tmp_path).get(1).teams] == NAMES


def test_set_channel_names_fit_and_show_both_teams():
    for a in NAMES:
        for b in NAMES:
            if a == b:
                continue
            s = TourneySet(SimpleNamespace(name=a), SimpleNamespace(name=b), 3, set_id=123)
            name = match_channels.channel_name(s)
            assert len(name) <= 100 and name.startswith("set-123-") and "-vs-" in name
            assert name == name.lower() and " " not in name
    s = TourneySet(SimpleNamespace(name=NAMES[2]), SimpleNamespace(name=NAMES[0]), 3, set_id=7)
    assert match_channels.channel_name(s).startswith("set-7-équipe-ünïcödé-ñandú-команда")


def test_long_lists_are_split_not_cut():
    t = _registered()
    matchups.initialize(t)
    lines = matchups.standings_text(t).split("\n")
    embed = SimpleNamespace(fields=[], add_field=lambda **kw: embed.fields.append(kw))
    discord_text.add_fields(embed, "Standings", lines)
    assert len(embed.fields) > 1 and all(len(f["value"]) <= discord_text.FIELD_LIMIT for f in embed.fields)
    shown = "\n".join(f["value"] for f in embed.fields)
    assert all(matchups._esc(n) in shown for n in NAMES)
    sets = [matchups.format_set(s) for s in matchups.require_current_round(t).matchups]
    blocks = discord_text.chunks(sets, discord_text.DESCRIPTION_LIMIT)
    assert "\n".join(blocks) == "\n".join(sets) and all(len(b) <= discord_text.DESCRIPTION_LIMIT for b in blocks)


def test_markdown_in_names_is_escaped():
    assert report_cog._name(SimpleNamespace(name="A_*B*_")) == "A\\_\\*B\\*\\_"


def test_csv_cells_never_run_as_formulas():
    assert [stream_cog._text_cell(n) for n in ("=SUM(A1)", "+1", "-x", "@here", "Team", None)] == \
           ["'=SUM(A1)", "'+1", "'-x", "'@here", "Team", ""]


def test_stream_commands_find_teams_by_any_name():
    paths = {name: f"team{i:02d}" for i, name in enumerate(NAMES, start=1)}
    paths["team03"] = "team16"  # a team literally called like another team's path
    cog = SimpleNamespace(_team_paths=lambda guild_id: paths)
    resolve = lambda value: stream_cog.Stream._resolve_team(cog, 1, value)
    for name, path in paths.items():
        assert resolve(name) == path
        assert resolve(f'"{name.upper()}"') == path
        assert resolve(f"  {name}  ".replace(" ", "  ")) == path
    assert resolve("team03") == "team16"  # the team's name wins over the path
    assert resolve("TEAM05") == "team05"
    with pytest.raises(stream_cog.StreamError):
        resolve("no such team")
