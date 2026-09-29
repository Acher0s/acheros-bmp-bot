import logging

import discord
from discord.ext import commands

import registration as reg
from models.tournament import Player, Team
from persistence import StorageError

log = logging.getLogger(__name__)


def to_player(user: discord.User | discord.Member) -> Player:
    return Player(str(user.id), user.name)


def is_organizer(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild


def format_team(team: Team) -> str:
    name = discord.utils.escape_markdown(team.name)
    members = " ".join(f"<@{p.uid}>" for p in team.players)
    return f"**{name}** ({members})"


class Teams(commands.Cog):
    """Team registration. Every change is saved to disk before the bot replies."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # -- helpers --------------------------------------------------------------
    # Rule: check -> change -> save with no `await` in between, so two commands
    # can never interleave and the file always matches memory.

    def _tournament(self, ctx: commands.Context):
        return self.bot.store.get(ctx.guild.id)

    def _save(self, ctx: commands.Context) -> None:
        self.bot.store.save(ctx.guild.id)

    def _require_manager(self, ctx: commands.Context) -> None:
        if is_organizer(ctx.author):
            return
        raise reg.RegistrationError(
            f"Only organizers can do that."
        )

    @staticmethod
    def _reject_bots(users) -> None:
        if any(u.bot for u in users):
            raise reg.RegistrationError("Bots can't be on a team.")

    # -- commands -------------------------------------------------------------

    @commands.group(name="team", invoke_without_command=True)
    async def team(self, ctx: commands.Context):
        """Register and manage teams."""
        await ctx.send_help(ctx.command)

    @team.command(name="create", usage='"<team name>" [@player ...]')
    async def team_create(self, ctx: commands.Context, name: str, *members: discord.Member):
        """Create a team. With no @mentions, you're the only member.
        If you list players, that's the whole roster (you must be in it unless you're an organizer)."""
        roster = list(members) or [ctx.author]
        self._reject_bots(roster)
        if ctx.author not in roster and not is_organizer(ctx.author):
            raise reg.RegistrationError("You can only create a team that includes yourself.")

        t = self._tournament(ctx)
        team = reg.create_team(t, name, [to_player(m) for m in roster])
        self._save(ctx)
        await ctx.send(f"Created {format_team(team)}. {len(t.teams)} team(s) registered.")

    @team.command(name="add", usage='"<team name>" @player [@player ...]')
    async def team_add(self, ctx: commands.Context, team_name: str, *members: discord.Member):
        """Add players to a team (team members and organizers only)."""
        if not members:
            raise reg.RegistrationError("Mention at least one player to add.")
        self._reject_bots(members)

        t = self._tournament(ctx)
        team = reg.require_team(t, team_name)
        self._require_manager(ctx, team)
        reg.add_players(t, team, [to_player(m) for m in members])
        self._save(ctx)
        await ctx.send(f"Updated {format_team(team)}.")

    @team.command(name="remove", usage='"<team name>" @player [@player ...]')
    async def team_remove(self, ctx: commands.Context, team_name: str, *users: discord.User):
        """Remove players from a team (team members and organizers only).
        A team with no players left is disbanded."""
        if not users:
            raise reg.RegistrationError("Mention at least one player to remove.")

        t = self._tournament(ctx)
        team = reg.require_team(t, team_name)
        self._require_manager(ctx)
        disbanded = reg.remove_players(t, team, [to_player(u) for u in users])
        self._save(ctx)
        if disbanded:
            await ctx.send(f"**{discord.utils.escape_markdown(team.name)}** has no players left, so it was disbanded.")
        else:
            await ctx.send(f"Updated {format_team(team)}.")

    @team.command(name="disband")
    async def team_disband(self, ctx: commands.Context, *, team_name: str):
        """Disband a team (team members and organizers only)."""
        t = self._tournament(ctx)
        team = reg.require_team(t, team_name)
        self._require_manager(ctx)
        reg.disband_team(t, team)
        self._save(ctx)
        await ctx.send(f"Disbanded **{discord.utils.escape_markdown(team.name)}**.")

    @team.command(name="list")
    async def team_list(self, ctx: commands.Context):
        """Show all registered teams."""
        t = self._tournament(ctx)
        if not t.teams:
            await ctx.send("No teams registered yet.")
            return
        teams = sorted(t.teams, key=lambda x: x.name.casefold())
        lines = [f"{i}. {format_team(team)}" for i, team in enumerate(teams, start=1)]
        embed = discord.Embed(title=f"Registered teams ({len(teams)})", description="\n".join(lines))
        await ctx.send(embed=embed)

    # -- errors ---------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, reg.RegistrationError):
            await ctx.send(str(error))
        elif isinstance(error, (StorageError, OSError)):
            log.error("Storage problem while running %s", ctx.command, exc_info=error)
            await ctx.send("Couldn't load or save the tournament data, so nothing was changed. Check the bot's log.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Team commands only work inside a server.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(
                f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`\n"
                "@mention players, and put team names that contain spaces in quotes."
            )
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))