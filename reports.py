"""Reporting a match's result with log files. Pure logic, no Discord or web imports.

How a match gets reported:
  1. The set's channel shows a widget for the current *match* (not the set) where
     each team picks who won. Once both teams picked the same winner, a report page
     with a secret link opens for the match.
  2. Each team uploads its Lovely log on that page, into its own slot. A log can
     hold several games; the ones on the match's deck and stake are kept.
  3. The two logs are checked against each other: there must be one game in each
     with the same seed, played by the same two in-game names (each log's player is
     the other's opponent), where one side won and the other lost. The in-game names
     are never linked to Discord users or teams; a log belongs to a team only because
     that team uploaded it into its slot.
  4. The match concludes once both logs agree AND both teams picked the winner the
     logs show. Only then is the result recorded, which is also what allows the next
     match of the set to be added. Managers can always decide or correct a result
     without logs (see "managers" below).

The report page closes after a while (the link stops working), but the uploaded
log files are kept on disk. If the teams (re)agree after it closed, a fresh link
opens and the uploads are kept.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple

from log_parser import GameRecord, LogParseError, parse_log
from models.tournament import GameResult, GameState, Match, TourneySet

PAGE_HOURS = 24               # how long a report link works
PAGE_HOURS_AFTER_RESULT = 2   # ...but it closes this soon after the match concludes
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # a 30-minute session with 2 games is ~260 KB
MAX_UPLOADS_PER_TEAM = 5      # per match; replacing a wrong file counts too

SLOTS = (1, 2)  # team1 / team2 of the set


class ReportError(Exception):
    """A rule violation. The message is shown to the user as-is."""


@dataclass
class Upload:
    file: str                   # path of the saved log, relative to the logs directory
    sha256: str
    uploaded_at: float
    games: List[GameRecord]     # finished games in the log on the match's deck and stake
    total_games: int = 0        # all games found in the log

    def to_dict(self) -> dict:
        return {"file": self.file, "sha256": self.sha256, "uploaded_at": self.uploaded_at,
                "games": [g.to_dict() for g in self.games], "total_games": self.total_games}

    @classmethod
    def from_dict(cls, d: dict) -> "Upload":
        return cls(d["file"], d["sha256"], float(d["uploaded_at"]),
                   [GameRecord.from_dict(g) for g in d["games"]], int(d.get("total_games", 0)))


@dataclass
class MatchReport:
    token: str | None = None            # secret part of the report page link; None until the teams agree
    expires_at: float | None = None
    votes: Dict[int, int] = field(default_factory=dict)   # team slot -> the slot it says won
    voters: Dict[int, str] = field(default_factory=dict)  # team slot -> Discord user id of its last vote
    widget_message_id: int | None = None  # the voting widget in the set's channel
    uploads: Dict[int, Upload] = field(default_factory=dict)
    upload_counts: Dict[int, int] = field(default_factory=lambda: {1: 0, 2: 0})
    final: Dict[int, GameRecord] | None = None  # the two verified games, once concluded

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "expires_at": self.expires_at,
            "votes": {str(k): v for k, v in self.votes.items()},
            "voters": {str(k): v for k, v in self.voters.items()},
            "widget_message_id": self.widget_message_id,
            "uploads": {str(k): u.to_dict() for k, u in self.uploads.items()},
            "upload_counts": {str(k): v for k, v in self.upload_counts.items()},
            "final": {str(k): g.to_dict() for k, g in self.final.items()} if self.final else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MatchReport":
        return cls(
            token=str(d["token"]) if d.get("token") else None,
            expires_at=float(d["expires_at"]) if d.get("expires_at") is not None else None,
            votes={_slot(k): _slot(v) for k, v in d.get("votes", {}).items()},
            voters={_slot(k): str(v) for k, v in d.get("voters", {}).items()},
            widget_message_id=int(d["widget_message_id"]) if d.get("widget_message_id") else None,
            uploads={_slot(k): Upload.from_dict(u) for k, u in d.get("uploads", {}).items()},
            upload_counts={1: 0, 2: 0, **{_slot(k): int(v) for k, v in d.get("upload_counts", {}).items()}},
            final={_slot(k): GameRecord.from_dict(g) for k, g in d["final"].items()} if d.get("final") else None,
        )

    @property
    def agreed_winner(self) -> int | None:
        """The slot both teams picked as winner, if they agree."""
        v1, v2 = self.votes.get(1), self.votes.get(2)
        return v1 if v1 is not None and v1 == v2 else None


def _slot(key) -> int:
    slot = int(key)
    if slot not in SLOTS:
        raise ValueError(f"Invalid team slot {key!r}")
    return slot


# -- finding things -----------------------------------------------------------

def current_match(s: TourneySet) -> Tuple[int, Match] | None:
    """(match number, match) of the set's match that still needs a result, if any."""
    for n, m in reversed(list(enumerate(s.matches, start=1))):
        if m.state != GameState.FINISHED:
            return n, m
    return None


def find_by_token(t, token: str) -> Tuple[TourneySet, int, Match] | None:
    """(set, match number, match) for a report link token."""
    for stage in t.stages:
        for r in stage.rounds:
            for s in r.matchups:
                for n, m in enumerate(s.matches, start=1):
                    if m.report is not None and m.report.token and secrets.compare_digest(m.report.token, token):
                        return s, n, m
    return None


def used_seeds(t, exclude: Match) -> set[str]:
    """Seeds of games already used to conclude other matches (a game can't count twice)."""
    seeds = set()
    for stage in t.stages:
        for r in stage.rounds:
            for s in r.matchups:
                for m in s.matches:
                    if m is not exclude and m.report is not None and m.report.final:
                        seeds.update(g.seed for g in m.report.final.values() if g.seed)
    return seeds


# -- the report page ----------------------------------------------------------

def page_is_open(report: MatchReport | None, now: float) -> bool:
    return (report is not None and report.token is not None and report.expires_at is not None
            and now < report.expires_at)


def ensure_report(m: Match) -> MatchReport:
    """The match's report (created if needed); marks the match as being played."""
    if m.report is None:
        m.report = MatchReport()
    if m.state == GameState.INIT:
        m.state = GameState.STARTED
    return m.report


def vote(m: Match, team_slot: int, winner_slot: int, user_id: str, now: float) -> int | None:
    """A team picks the winner. Returns the agreed winner, if both teams now agree.

    Agreeing opens the report page (a fresh link if the old one expired; uploads are
    kept). Teams can change their pick until the match has a result.
    """
    if m.state == GameState.FINISHED:
        raise ReportError("This match already has a result.")
    r = ensure_report(m)
    r.votes[_slot(team_slot)] = _slot(winner_slot)
    r.voters[team_slot] = user_id
    if r.agreed_winner is not None and not page_is_open(r, now):
        r.token = secrets.token_urlsafe(24)
        r.expires_at = now + PAGE_HOURS * 3600
    return r.agreed_winner


# -- uploads ------------------------------------------------------------------

def matching_games(m: Match, games: Iterable[GameRecord]) -> List[GameRecord]:
    """Finished games on the match's deck and stake."""
    return [g for g in games
            if g.finished
            and (g.deck or "").casefold() == m.deck.name.casefold()
            and g.stake_name == m.stake.name]


def why_no_upload(t, m: Match, slot: int, now: float) -> str | None:
    """None if this team may upload a log now, otherwise the reason it can't."""
    r = m.report
    if not page_is_open(r, now):
        if r is None or r.token is None:
            return "Logs can be uploaded once both teams picked the same winner in Discord."
        return ("This report link has expired. Click your team's pick on the widget in your set's channel "
                "again for a new one.")
    if m.state == GameState.FINISHED:
        return "This match already has a result."
    if slot not in SLOTS:
        return "Unknown team."
    if check(m, used_seeds(t, m)).pair is not None:
        return "Both logs are already verified; they can't be replaced."
    if r.upload_counts.get(slot, 0) >= MAX_UPLOADS_PER_TEAM:
        return f"This team already uploaded {MAX_UPLOADS_PER_TEAM} times. Ask an admin for help."
    return None


def parse_upload(text: str) -> List[GameRecord]:
    """All games in an uploaded log; raises ReportError if it isn't a usable log."""
    try:
        games = parse_log(text)
    except LogParseError as e:
        raise ReportError(str(e)) from e
    if not games:
        raise ReportError("No Balatro Multiplayer games were found in this log.")
    return games


def record_upload(t, m: Match, slot: int, file: str, sha256: str, games: List[GameRecord], now: float) -> Upload:
    """Stores a (parsed) upload in the team's slot, replacing an earlier one."""
    reason = why_no_upload(t, m, slot, now)
    if reason is not None:
        raise ReportError(reason)
    upload = Upload(file, sha256, now, matching_games(m, games), len(games))
    m.report.uploads[slot] = upload
    m.report.upload_counts[slot] = m.report.upload_counts.get(slot, 0) + 1
    return upload


# -- checking and concluding --------------------------------------------------

@dataclass
class ReportStatus:
    uploaded: Dict[int, bool]
    has_game: Dict[int, bool]          # the log has a finished game on the right deck/stake
    pair: Tuple[GameRecord, GameRecord] | None  # (team1's game, team2's game), if the logs agree
    log_winner: int | None             # slot that won according to the logs
    agreed_winner: int | None          # slot both teams picked in Discord

    @property
    def paired(self) -> bool:
        return self.pair is not None

    @property
    def ready(self) -> bool:
        return self.paired and self.agreed_winner == self.log_winner

    @property
    def conflict(self) -> bool:
        return self.paired and self.agreed_winner is not None and self.agreed_winner != self.log_winner


def _same_game(g1: GameRecord, g2: GameRecord) -> bool:
    return (g1.seed is not None and g1.seed == g2.seed
            and g1.player is not None and g1.opponent is not None
            and g1.player == g2.opponent and g1.opponent == g2.player
            and {g1.result, g2.result} == {"win", "loss"})


def check(m: Match, seeds_used: set[str]) -> ReportStatus:
    r = m.report
    uploads = r.uploads if r is not None else {}
    games = {slot: uploads[slot].games if slot in uploads else [] for slot in SLOTS}

    pair = None
    if r is not None and r.final:
        pair = (r.final[1], r.final[2])
    else:
        for g1 in games[1]:
            for g2 in games[2]:
                if _same_game(g1, g2) and g1.seed not in seeds_used:
                    pair = (g1, g2)  # keep going: the last (latest) game wins

    return ReportStatus(
        uploaded={slot: slot in uploads for slot in SLOTS},
        has_game={slot: bool(games[slot]) for slot in SLOTS},
        pair=pair,
        log_winner=(1 if pair[0].result == "win" else 2) if pair else None,
        agreed_winner=r.agreed_winner if r is not None else None,
    )


def try_conclude(t, m: Match, now: float) -> ReportStatus:
    """Records the result if both logs agree with the winner marked in Discord."""
    status = check(m, used_seeds(t, m))
    if m.state != GameState.FINISHED and status.ready:
        m.report.final = {1: status.pair[0], 2: status.pair[1]}
        _set_result(m, status.log_winner, None, "logs", now)
        if m.report.expires_at is not None:
            m.report.expires_at = min(m.report.expires_at, now + PAGE_HOURS_AFTER_RESULT * 3600)
    return status


# -- managers: manual results and corrections ---------------------------------
#
# Managers can always decide a match themselves (no logs needed), and fix a wrong
# result. Results can only change while the set's round is the current round: once
# `next_round` has paired a new round from these results, changing one would make
# those pairings wrong. Every change is kept in Match.history.

def _set_result(m: Match, slot: int | None, by: str | None, action: str, now: float) -> None:
    if slot is None:
        m.result, m.state = None, GameState.STARTED
    else:
        m.report_result(GameResult.TEAM1_WIN if slot == 1 else GameResult.TEAM2_WIN)
    m.history.append({"at": now, "by": by, "action": action, "result": m.result.name if m.result else None})


def why_no_change(t, s: TourneySet) -> str | None:
    """None if managers may still change this set's results, otherwise the reason."""
    rnd = t.get_current_round()
    if rnd is None or s not in rnd.matchups:
        return (f"Set #{s.set_id} is from an earlier round. The next round was already paired from its "
                "results, so they can't be changed anymore.")
    return None


def manual_result(t, s: TourneySet, m: Match, slot: int, by: str, now: float) -> None:
    """A manager decides the match; any log reporting still going on is dropped."""
    reason = why_no_change(t, s)
    if reason is not None:
        raise ReportError(reason)
    if m.state == GameState.FINISHED:
        raise ReportError("This match already has a result. Use `!report correct` to change it.")
    _set_result(m, _slot(slot), by, "manual", now)
    if m.report is not None and m.report.expires_at is not None:
        m.report.expires_at = min(m.report.expires_at, now)  # close the page


def correct_result(t, s: TourneySet, match_no: int, slot: int | None, by: str, now: float) -> Match:
    """Changes a finished match's winner, or (slot None) reopens it to be played/reported again.

    Reopening is only for the set's last match (later matches were added because of
    this result), and starts its reporting from scratch; uploaded files stay on disk.
    """
    reason = why_no_change(t, s)
    if reason is not None:
        raise ReportError(reason)
    if not 1 <= match_no <= len(s.matches):
        raise ReportError(f"Set #{s.set_id} has no match {match_no}.")
    m = s.matches[match_no - 1]
    if m.state != GameState.FINISHED:
        raise ReportError(f"Match {match_no} has no result yet. Use `!report manual` to decide it.")
    if slot is None:
        if match_no != len(s.matches):
            raise ReportError(f"Only the last match of the set can be reopened (match {len(s.matches)}).")
        m.report = None
        _set_result(m, None, by, "reopen", now)
    else:
        _set_result(m, _slot(slot), by, "correct", now)
    return m


# -- round statistics ---------------------------------------------------------

@dataclass
class TeamStats:
    team: object
    games: int = 0
    rerolls: int = 0
    money_spent: int = 0
    highest_score: Decimal | None = None
    highest_by: str | None = None     # in-game name that scored it

    def add(self, g: GameRecord) -> None:
        self.games += 1
        self.rerolls += g.rerolls or 0
        self.money_spent += g.money_spent or 0
        score = Decimal(g.highest_score) if g.highest_score else None
        if score is not None and (self.highest_score is None or score > self.highest_score):
            self.highest_score, self.highest_by = score, g.player


def round_stats(rnd) -> Tuple[List[TeamStats], int, int]:
    """Per-team totals from the verified logs of a round's finished matches.

    Returns (stats of teams with at least one verified game, matches with verified
    logs, finished matches). Matches decided without both logs are left out.
    """
    stats: Dict[int, TeamStats] = {}
    verified = finished = 0
    for s in rnd.matchups:
        for m in s.matches:
            if m.state != GameState.FINISHED:
                continue
            finished += 1
            if m.report is None or not m.report.final:
                continue
            verified += 1
            for slot, team in zip(SLOTS, (s.team1, s.team2)):
                stats.setdefault(id(team), TeamStats(team)).add(m.report.final[slot])
    return list(stats.values()), verified, finished
