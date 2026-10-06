"""Log parser: the poly hack card count from the deck a player sends at the end of a game.  Run from the repo root:  python -m pytest tests"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log_parser  # noqa: E402

PREFIX = "INFO - [G] 2026-10-06 13:27:47 :: TRACE :: MULTIPLAYER :: "


def _log(*lines: str) -> str:
    return "INFO - [♥] Lovely 0.9\n" + "\n".join(PREFIX + line for line in lines)


MANIFEST = 'MP_RLOG: MANIFEST {"player":"Birb","opponent":"Saksh","seed":"YF6NSXQ3","deck":"Red Deck","stake":1}'
OWN_DECK = ('Client sent message: {"cards":";S-2-c_base-polychrome-none;H-5-m_steel-polychrome-Red;'
            'D-6-c_base-polychrome-none;C-4-c_base-foil-none;S-K-m_lucky-polychrome-Red;C-3-m_wild-polychrome-none",'
            '"action":"receiveNemesisDeck"}')
OPPONENT_DECK = ("Client got receiveNemesisDeck message:  (cards: ;S-2-c_base-polychrome-none;S-3-c_base-polychrome-none)"
                 "  (action: receiveNemesisDeck) ")


def test_counts_polychrome_2s_to_5s_in_the_players_own_deck():
    [game] = log_parser.parse_log(_log(
        "Client got startGame message:  (deck: c_multiplayer_1)  (action: startGame) ",
        MANIFEST,
        "Client got winGame message:  (action: winGame) ",
        OWN_DECK,
        OPPONENT_DECK,  # the opponent's deck never counts
        "Client got stopGame message:  (action: stopGame)  (seed: YF6NSXQ3) ",
    ))
    assert game.poly_hack_cards == 3  # S-2, H-5 and C-3; not the 6, the foil 4 or the king


def test_no_deck_in_the_log_is_unknown_not_zero():
    [game] = log_parser.parse_log(_log(MANIFEST))
    assert game.poly_hack_cards is None
    assert log_parser.GameRecord.from_dict({"player": "old record"}).poly_hack_cards is None
