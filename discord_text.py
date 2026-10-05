"""Fitting text into Discord's limits without cutting it off mid-list.

Team names can be up to 64 characters (twice that once markdown is escaped), so lists of 16 teams
can outgrow a message (2000), an embed description (4096) or an embed field (1024).
"""

MESSAGE_LIMIT = 2000
DESCRIPTION_LIMIT = 4096
FIELD_LIMIT = 1024


def _split_line(line: str, limit: int) -> list[str]:
    """A line longer than `limit`, cut after a comma (or else a space) where possible."""
    parts = []
    while len(line) > limit:
        cut = line.rfind(", ", 0, limit - 1) + 1 or line.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    parts.append(line)
    return parts


def chunks(lines: list[str], limit: int) -> list[str]:
    """Join lines into blocks of at most `limit` characters. A line is only split when it's too long
    on its own."""
    blocks, current = [], ""
    for line in (part for line in lines for part in _split_line(line, limit)):
        if current and len(current) + 1 + len(line) > limit:
            blocks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        blocks.append(current)
    return blocks


def add_fields(embed, name: str, lines: list[str], inline: bool = False, empty: str = "-") -> None:
    """One embed field with these lines, continued in more fields when they don't fit in one."""
    for i, block in enumerate(chunks(lines, FIELD_LIMIT) or [empty]):
        embed.add_field(name=name if i == 0 else f"{name} (cont.)", value=block, inline=inline)


async def send_lines(destination, lines: list[str]) -> None:
    """Send lines as one or more messages, splitting only between lines."""
    for block in chunks(lines, MESSAGE_LIMIT):
        await destination.send(block)
