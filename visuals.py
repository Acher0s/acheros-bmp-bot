"""Draws the picture for !selection pullrandom.

Top: one long bar split into a segment per vote option. A segment's width matches
its share of the votes (options with 0 votes get no segment), neighbours are
separated by a small break, and each segment has the average color of its stake
icon. An arrow above the bar marks where the pull landed.

Below the bar: one fixed-size card per option (deck icon on the stake color, option
number underneath, same number as in the vote message). Each card is connected to
its segment, so every deck is clearly visible no matter how few votes it got.

Needs Pillow (pip install pillow).
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageStat

from conjoined import VoteResults

WIDTH, HEIGHT = 1000, 330
PAD = 30
BAR_TOP, BAR_HEIGHT = 80, 44
GAP = 6                                # break between neighbouring bar segments (px)
CARD_TOP, CARD_W, CARD_H = 172, 94, 110  # card row (max card width; shrinks only if many options)

BG = (43, 45, 49)  # Discord dark grey
TEXT = (242, 243, 245)
LINE = (100, 104, 112)  # card outlines and connectors
NEUTRAL = (149, 165, 166)  # color used if a stake image can't be read


def _font(size: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow 10.1+
    except TypeError:
        return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, cx: float, y: float, text: str, font, fill) -> None:
    width = draw.textlength(text, font=font)
    draw.text((cx - width / 2, y), text, font=font, fill=fill)


def _average_color(path: Path) -> tuple[int, int, int]:
    """Average color of the center 10x10 pixels of an image (visible pixels only)."""
    try:
        img = Image.open(path).convert("RGBA")
    except OSError:
        return NEUTRAL
    left, top = max(0, (img.width - 10) // 2), max(0, (img.height - 10) // 2)
    center = img.crop((left, top, min(img.width, left + 10), min(img.height, top + 10)))
    visible = center.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
    if visible.getbbox() is None:  # completely transparent
        return NEUTRAL
    r, g, b = ImageStat.Stat(center.convert("RGB"), visible).mean
    return round(r), round(g), round(b)


def _fit(path: Path, max_w: int, max_h: int) -> Image.Image | None:
    """Loads an icon scaled to fit the box (keeping its shape). None if it can't be read."""
    try:
        img = Image.open(path).convert("RGBA")
    except OSError:
        return None
    scale = min(max_w / img.width, max_h / img.height)
    method = Image.Resampling.NEAREST if scale > 1 else Image.Resampling.LANCZOS  # crisp pixel art
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), method)


def _mix(color: tuple[int, int, int], other: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """`amount` of `color` blended over `other`."""
    return tuple(round(c * amount + o * (1 - amount)) for c, o in zip(color, other))


def render_pull_image(results: VoteResults, winner: int, fraction: float) -> io.BytesIO:
    """PNG of the vote bar with an arrow inside option `winner`.

    `fraction` (0..1) is how far along that option's segment the arrow points.
    The winner itself is decided by VoteResults.random_weighted_selection(); a
    random spot inside its segment is exactly as likely as any other, so this
    only affects where the arrow is drawn.
    """
    total = results.count_votes()
    if total == 0 or results.votes[winner] == 0:
        raise ValueError("Can't draw a pull from a vote with no votes on the winner")

    bar_x0, bar_w = PAD, WIDTH - 2 * PAD
    bar_bottom = BAR_TOP + BAR_HEIGHT

    # Each voted option's share of the bar, shrunk by half a gap per side -> a break between neighbours.
    segments = []  # (option index, left, right)
    cum = 0
    for i, votes in enumerate(results.votes):
        if votes == 0:
            continue
        slot0 = bar_x0 + round(bar_w * cum / total)
        cum += votes
        slot1 = bar_x0 + round(bar_w * cum / total)
        left = slot0 + GAP // 2
        segments.append((i, left, max(left + 2, slot1 - GAP // 2)))

    img = Image.new("RGBA", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    f_num = _font(22)

    slot_w = bar_w / len(segments)  # the cards share the width equally
    card_w = int(min(CARD_W, slot_w - 8))
    winner_span = (bar_x0, bar_x0)

    for n, (i, left, right) in enumerate(segments):
        deck, stake = results.selection[i]
        color = _average_color(stake.png_path)
        if i == winner:
            winner_span = (left, right)

        draw.rectangle([left, BAR_TOP, right - 1, bar_bottom - 1], fill=color)

        cx = bar_x0 + slot_w * (n + 0.5)
        c0, c1 = round(cx - card_w / 2), round(cx + card_w / 2)
        draw.polygon([(left, bar_bottom), (right - 1, bar_bottom), (c1, CARD_TOP), (c0, CARD_TOP)],
                     fill=_mix(color, BG, 0.3), outline=LINE)  # connector from segment to card

        winning = i == winner
        draw.rounded_rectangle([c0, CARD_TOP, c1, CARD_TOP + CARD_H], radius=8, fill=color,
                               outline=TEXT if winning else LINE, width=4 if winning else 2)
        icon = _fit(deck.png_path, card_w - 12, CARD_H - 12)
        if icon is not None:
            img.paste(icon, (round(cx - icon.width / 2), CARD_TOP + (CARD_H - icon.height) // 2), icon)
        _centered(draw, cx, CARD_TOP + CARD_H + 8, str(i + 1), f_num, TEXT)

    # Arrow (pointing down) above the bar, plus a marker line across the winning segment.
    left, right = winner_span
    ax = left + 1 + fraction * max(0, right - left - 2)
    tip = BAR_TOP - 3
    draw.line([(ax, BAR_TOP), (ax, bar_bottom)], fill=BG, width=7)
    draw.line([(ax, BAR_TOP), (ax, bar_bottom)], fill=TEXT, width=3)
    draw.polygon([(ax - 14, tip - 26), (ax + 14, tip - 26), (ax, tip)], fill=TEXT)

    deck, stake = results.selection[winner]
    label = f"{winner + 1}. {deck} / {stake}"
    label_w = draw.textlength(label, font=f_num)
    lx = min(max(ax - label_w / 2, PAD), WIDTH - PAD - label_w)
    draw.text((lx, tip - 26 - 32), label, font=f_num, fill=TEXT)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf