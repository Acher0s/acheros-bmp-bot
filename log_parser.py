"""Reads Balatro Multiplayer games out of a Lovely log file. Pure logic, no Discord imports.

A Lovely log covers a whole game session, so one file can hold several games.
The Multiplayer mod writes these lines (all tagged ":: MULTIPLAYER ::"):

  Client got startGame message: ...          a game starts
  MP_RLOG: MANIFEST {json}                   deck, seed, lobby_config.stake, player, opponent, ...
  Client sent message: {"action":"playHand","score":"1207","handsLeft":2}
                                             the running score of a PvP blind after each hand
                                             (normal blinds only ever send score 0, so PvP
                                             blinds are the only scores a log contains)
  Client sent message: {"action":"spentLastShop","amount":9}
                                             money spent in the shop that just closed
  MP_RLOG: 29 reroll                         one shop reroll
  Client sent message: {"action":"nemesisEndGameStats","reroll_count":11,...}
                                             this player's end-of-game stats
  Client got winGame / loseGame message      the result for the player who wrote the log
  MP_RLOG: END {"result":"win"}              the same result ("win", "loss" or "stop")
  Client got stopGame message: (seed: ...)   back to the lobby: the game is over

Only lines the player's own client sent are used for their stats; "Client got
..." lines about the opponent are ignored. Player names are the in-game names
from the manifest and are never matched to Discord users.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

# Stake numbers as the game counts them: the 8 vanilla stakes, then the
# Multiplayer mod's own stakes, which it places after Gold.
STAKE_NUMBERS = {
    1: "White", 2: "Red", 3: "Green", 4: "Black", 5: "Blue", 6: "Purple", 7: "Orange", 8: "Gold",
    9: "Planet", 10: "Spectral", 11: "Spectral+",
}

_MP_TAG = ":: MULTIPLAYER ::"
_SENT_JSON = re.compile(r"Client sent message: (\{.*\})\s*$")
_GOT = re.compile(r"Client got (\w+) message:(.*)$")
_GOT_PAIR = re.compile(r"\((\w+):\s*([^)]*)\)")
_RLOG = re.compile(r":: MULTIPLAYER :: MP_RLOG: (.*)$")
_NAME_SUFFIX = re.compile(r"~\d+$")


class LogParseError(Exception):
    """The file isn't a Lovely log with Balatro Multiplayer games in it."""


def _clean_name(name) -> str | None:
    """'quppy~9' -> 'quppy'. The lobby adds a ~number to names; the manifest doesn't."""
    if name is None:
        return None
    name = _NAME_SUFFIX.sub("", str(name).strip())
    return name or None


def _deck_name(raw) -> str | None:
    """'Ghost Deck' -> 'Ghost'."""
    if not raw:
        return None
    raw = str(raw).strip()
    return raw[:-len(" Deck")] if raw.endswith(" Deck") else raw


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _score(value) -> Decimal | None:
    """Scores are sent as strings and can get huge ('1.2e+15'), so no floats."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _fmt_score(d: Decimal | None) -> str | None:
    if d is None:
        return None
    return str(d.quantize(Decimal(1))) if d == d.to_integral_value() and d < Decimal("1e21") else str(d)


@dataclass
class GameRecord:
    """One game as seen by the player who wrote the log."""
    player: str | None = None
    opponent: str | None = None
    deck: str | None = None           # 'Ghost', matches models.deck names
    stake: int | None = None          # the game's stake number
    seed: str | None = None
    result: str | None = None         # 'win', 'loss', or None if the game never finished
    rerolls: int = 0
    money_spent: int = 0              # total of all shop spending
    highest_score: str | None = None  # best total reached in one PvP blind
    highest_hand: str | None = None   # best single hand in a PvP blind
    game_id: str | None = None
    lobby_code: str | None = None
    started_at: str | None = None     # from the manifest, with the player's UTC offset

    @property
    def stake_name(self) -> str | None:
        return STAKE_NUMBERS.get(self.stake)

    @property
    def finished(self) -> bool:
        return self.result in ("win", "loss")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameRecord":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class _Builder:
    """Collects one game while its lines are read."""
    rec: GameRecord = field(default_factory=GameRecord)
    reroll_lines: int = 0
    stats_rerolls: int | None = None
    blind_score: Decimal = Decimal(0)
    blind_hands: int = 0
    best_hand: Decimal | None = None
    best_score: Decimal | None = None
    has_manifest: bool = False

    def play_hand(self, score: Decimal, hands_left: int):
        # The score is the running total of the current blind; it drops back to 0
        # (and hands go back up) when a new blind starts.
        if score < self.blind_score or hands_left > self.blind_hands:
            self.blind_score = Decimal(0)
        hand = score - self.blind_score
        if hand > 0:
            self.best_hand = hand if self.best_hand is None else max(self.best_hand, hand)
            self.best_score = score if self.best_score is None else max(self.best_score, score)
        self.blind_score, self.blind_hands = score, hands_left

    def finish(self) -> GameRecord:
        r = self.rec
        r.rerolls = self.stats_rerolls if self.stats_rerolls is not None else self.reroll_lines
        r.highest_hand = _fmt_score(self.best_hand)
        r.highest_score = _fmt_score(self.best_score)
        return r


def parse_log(text: str) -> list[GameRecord]:
    """All games in the log, in order. Games that never got a manifest are skipped."""
    if "Lovely" not in text[:2000]:
        raise LogParseError("This doesn't look like a Lovely log file.")

    games: list[GameRecord] = []
    game: _Builder | None = None
    username = None
    lobby_opponent = None

    def close():
        nonlocal game
        if game is not None and game.has_manifest:
            games.append(game.finish())
        game = None

    for line in text.splitlines():
        if _MP_TAG not in line:
            continue

        m = _RLOG.search(line)
        if m:
            body = m.group(1)
            if body.startswith("MANIFEST "):
                if game is None or game.has_manifest:  # a manifest always opens a game
                    close()
                    game = _Builder()
                try:
                    man = json.loads(body[len("MANIFEST "):])
                except json.JSONDecodeError:
                    continue
                cfg = man.get("lobby_config") or {}
                r = game.rec
                r.deck = _deck_name(man.get("deck") or cfg.get("back"))
                r.stake = _to_int(man.get("stake") if man.get("stake") is not None else cfg.get("stake"))
                r.seed = man.get("seed")
                r.player = _clean_name(man.get("player")) or _clean_name(username)
                r.opponent = _clean_name(man.get("opponent")) or _clean_name(lobby_opponent)
                r.game_id = man.get("game_id")
                r.lobby_code = man.get("lobby_code")
                r.started_at = man.get("start_ts")
                game.has_manifest = True
            elif game is not None:
                if re.match(r"\d+ reroll\b", body):
                    game.reroll_lines += 1
                elif body.startswith("END ") and game.rec.result is None:
                    try:
                        result = json.loads(body[4:]).get("result")
                    except (json.JSONDecodeError, AttributeError):
                        result = None
                    if result in ("win", "loss"):
                        game.rec.result = result
            continue

        m = _SENT_JSON.search(line)
        if m:
            try:
                msg = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            action = msg.get("action")
            if action == "username":
                username = msg.get("username")
            elif game is None:
                continue
            elif action == "playHand":
                score, hands = _score(msg.get("score")), _to_int(msg.get("handsLeft"))
                if score is not None and hands is not None:
                    game.play_hand(score, hands)
            elif action == "spentLastShop":
                game.rec.money_spent += _to_int(msg.get("amount")) or 0
            elif action == "nemesisEndGameStats":
                game.stats_rerolls = _to_int(msg.get("reroll_count"))
            continue

        m = _GOT.search(line)
        if m:
            action = m.group(1)
            pairs = {k: v.strip() for k, v in _GOT_PAIR.findall(m.group(2))}
            if action == "lobbyInfo":
                if pairs.get("isHost") == "true":
                    lobby_opponent = pairs.get("guest", lobby_opponent)
                elif pairs.get("isHost") == "false":
                    lobby_opponent = pairs.get("host", lobby_opponent)
            elif action == "startGame":
                close()
                game = _Builder()
            elif game is not None and action == "winGame":
                game.rec.result = "win"
            elif game is not None and action == "loseGame":
                game.rec.result = "loss"
            elif game is not None and action == "stopGame":
                game.rec.seed = game.rec.seed or pairs.get("seed")
                close()

    close()
    return games
