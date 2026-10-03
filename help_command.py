"""!help: list every cog and all of its commands, subcommands included. Managers only.

The built-in help only lists top-level groups (e.g. "bala"). This overview shows every
subcommand with its usage and summary. `!help <command>` still shows the details.
Like every other manager command, it's ignored silently for anyone else.
"""
import discord
from discord.ext import commands

import checks

FIELD_LIMIT = 1024
EMBED_LIMIT = 5500  # stay under Discord's 6000-character total per embed


def _line(command: commands.Command, prefix: str) -> str:
    usage = f" {command.signature}" if command.signature else ""
    doc = f" - {command.short_doc}" if command.short_doc else ""
    return f"`{prefix}{command.qualified_name}{usage}`{doc}"


def _chunks(lines: list[str], limit: int) -> list[str]:
    """Join lines into blocks of at most `limit` characters, never splitting a line."""
    blocks, current = [], ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + 1 + len(line) > limit:
            blocks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        blocks.append(current)
    return blocks


class TourneyHelp(commands.DefaultHelpCommand):
    def __init__(self):
        super().__init__(verify_checks=False, command_attrs={"checks": [checks.manager_predicate]})

    async def send_bot_help(self, mapping):
        prefix = self.context.clean_prefix
        embeds = [discord.Embed(title="Commands",
                                description=f"`{prefix}help <command>` shows more about a command.")]

        def add_field(name: str, value: str) -> None:
            if len(embeds[-1]) + len(name) + len(value) > EMBED_LIMIT or len(embeds[-1].fields) >= 25:
                embeds.append(discord.Embed())
            embeds[-1].add_field(name=name, value=value, inline=False)

        for cog, cmds in sorted(mapping.items(), key=lambda item: item[0].qualified_name if item[0] else "~"):
            lines = []
            for command in sorted(cmds, key=lambda c: c.name):
                if command.hidden:
                    continue
                # A group with subcommands is just a namespace: list the subcommands
                # instead (walk_commands goes all the way down).
                subcommands = sorted((c for c in command.walk_commands() if not c.hidden),
                                     key=lambda c: c.qualified_name) \
                    if isinstance(command, commands.Group) else []
                for c in subcommands or [command]:
                    lines.append(_line(c, prefix))
            if not lines:
                continue
            name = cog.qualified_name if cog else "Other"
            if cog and cog.description:
                lines.insert(0, f"*{cog.description}*")
            for i, block in enumerate(_chunks(lines, FIELD_LIMIT)):
                add_field(name if i == 0 else f"{name} (continued)", block)

        for embed in embeds:
            await self.get_destination().send(embed=embed)
