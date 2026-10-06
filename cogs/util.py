"""!util commands: admin helpers (managers only).

!util initemojis uploads one custom emoji per deck and per stake to the server,
using the PNGs in assets/. It is safe to run again: emojis that already exist
(matched by name) are skipped, so it only fills in what's missing.

Emoji names:  deck_<name>  and  stake_<name>  (lowercase, e.g. deck_red, stake_spectral).
The naming and lookup helpers live in emojis.py (shared with the vote messages).

The bot needs the "Manage Expressions" permission (called "Manage Emojis and
Stickers" in older Discord versions). Run the bot from the project folder, since
the asset paths are relative (./assets/...).

!util createvcs gives every team a private voice channel only its members can see
(details and the permissions it needs in team_vcs.py). Safe to run again: existing
channels are kept and their permissions reset.

!util dummyreport 1|2 (testing, in a set's channel) makes the set's dummy team(s)
pick team 1 or 2 as the winner of the current match, as if they clicked the
widget. Dummies never upload logs.
"""
import logging
from pathlib import Path

import discord
from discord.ext import commands

import dummies
import registration as reg
import team_roles
import team_vcs
from emojis import deck_emoji_name, stake_emoji_name
from models.deck import DECKS
from models.stake import STAKES
from persistence import StorageError
from reports import ReportError
import checks
import discord_text

log = logging.getLogger(__name__)

MAX_EMOJI_BYTES = 256 * 1024  # Discord's limit for an emoji image


class UtilError(Exception):
    """A rule violation. The message is shown to the user in Discord as-is."""


class Util(commands.Cog):
    """Admin utilities. Managers (Administrator permission) only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return checks.require_manager(ctx)

    @commands.group(name="util", invoke_without_command=True)
    async def util(self, ctx: commands.Context):
        """Admin utilities."""
        await ctx.send_help(ctx.command)

    @util.command(name="initemojis")
    async def initemojis(self, ctx: commands.Context):
        """Add an emoji for every deck and stake to this server (skips ones that already exist)."""
        wanted = ([(deck_emoji_name(d), d.png_path) for d in DECKS]
                  + [(stake_emoji_name(s), s.png_path) for s in STAKES])

        existing = await ctx.guild.fetch_emojis()  # fresh from Discord, not the cache
        have = {e.name for e in existing}
        todo = [(name, path) for name, path in wanted if name not in have]
        if not todo:
            await ctx.send("All deck and stake emojis already exist. Nothing to do.")
            return

        free = ctx.guild.emoji_limit - sum(1 for e in existing if not e.animated)
        if len(todo) > free:
            raise UtilError(f"I need {len(todo)} free emoji slots but this server only has {free}. "
                            "Delete some emojis (or boost the server) and try again. Nothing was added.")

        await ctx.send(f"Adding {len(todo)} emoji(s). Discord rate-limits this, so it can take a minute...")

        added: list[discord.Emoji] = []
        failed: list[str] = []
        async with ctx.typing():
            for name, path in todo:
                try:
                    data = Path(path).read_bytes()
                    if len(data) > MAX_EMOJI_BYTES:
                        raise ValueError(f"file is over {MAX_EMOJI_BYTES // 1024} KB")
                    emoji = await ctx.guild.create_custom_emoji(
                        name=name, image=data, reason="Tournament deck/stake emoji")
                except discord.Forbidden:
                    raise  # missing permission: no point trying the rest
                except (OSError, ValueError, discord.HTTPException) as e:
                    log.warning("Couldn't create emoji %s from %s: %r", name, path, e)
                    reason = (str(e) if isinstance(e, ValueError)
                              else "couldn't read the file" if isinstance(e, OSError)
                              else "Discord rejected it")
                    failed.append(f"`{name}` ({reason})")
                else:
                    added.append(emoji)

        lines = []
        if added:
            lines.append(f"Added {len(added)} emoji(s): " + " ".join(str(e) for e in added))
        if failed:
            lines.append(f":warning: {len(failed)} failed: " + ", ".join(failed)
                         + ". Fix the problem and run the command again; it only adds what's missing.")
        await ctx.send("\n".join(lines))

    # -- team voice channels ----------------------------------------------------

    @util.command(name="createvcs")
    async def createvcs(self, ctx: commands.Context):
        """Give every team a private voice channel only its members can see (resets existing ones)."""
        teams = list(self.bot.store.get(ctx.guild.id).teams)
        if not teams:
            raise UtilError("There are no teams yet.")
        no_role = [tm.name for tm in teams if tm.role_id is None or ctx.guild.get_role(tm.role_id) is None]

        created, kept = [], []
        async with ctx.typing():
            for name in [tm.name for tm in teams if tm.name not in no_role]:
                team = reg.find_team(self.bot.store.get(ctx.guild.id), name)
                try:
                    channel, is_new = await team_vcs.sync_team_vc(ctx.guild, team, ctx.guild.get_role(team.role_id))
                except discord.Forbidden:
                    raise UtilError(
                        "I'm missing a permission. I need **Manage Channels** and **Manage Roles**, plus View "
                        "Channel, Connect, Speak, Video, Send Messages and Read Message History (I can only "
                        "hand out permissions I have). Grant them and run the command again; it only does "
                        "what's missing." + (f"\nDone so far: {len(created)} created." if created else ""))
                # Look the team up again: the store may have reloaded during the `await`
                team = reg.find_team(self.bot.store.get(ctx.guild.id), name)
                if team is not None and team.vc_id != channel.id:
                    team.vc_id = channel.id
                    self.bot.store.save(ctx.guild.id)
                (created if is_new else kept).append(channel)

        lines = []
        if created:
            lines.append(f"Created {len(created)} voice channel(s): " + " ".join(c.mention for c in created))
        if kept:
            lines.append(f"{len(kept)} already existed; their permissions are reset to team-only.")
        if no_role:
            lines.append(f":warning: Skipped {len(no_role)} team(s) without a Discord role: "
                         + ", ".join(discord.utils.escape_markdown(n) for n in no_role)
                         + ". Run `!team syncroles`, then this command again.")
        lines.append("Only each team's members can see their channel. Members with Administrator (or Manage "
                     "Channels/Roles) can always see every channel; Discord doesn't allow blocking them.")
        await discord_text.send_lines(ctx, lines)

    # -- dummy teams (testing) --------------------------------------------------

    @util.command(name="filldummy", usage="[N]")
    async def filldummy(self, ctx: commands.Context, n: int | None = None):
        """DEV: register dummy teams (fake players) until there are 16 teams, or add N of them.
        Real teams you registered yourself are kept. Each dummy team gets a Discord role."""
        t = self.bot.store.get(ctx.guild.id)
        created = dummies.fill_dummy_teams(t, n)
        self.bot.store.save(ctx.guild.id)  # check -> change -> save, no `await` in between

        warning = ""
        before = [tm.role_id for tm in created]
        try:
            for team in created:
                await team_roles.sync_team(ctx.guild, team)
        except discord.HTTPException as e:
            log.warning("Couldn't create roles for dummy teams: %r", e)
            warning = "\n:warning: The teams are registered, but I couldn't create their roles (I need **Manage Roles**). Run `!team syncroles` once that's fixed."
        finally:
            if [tm.role_id for tm in created] != before:
                self.bot.store.save(ctx.guild.id)
        await ctx.send(f"Added {len(created)} dummy team(s): {', '.join(tm.name for tm in created)}. "
                       f"{len(t.teams)}/{dummies.MAX_TEAMS} teams registered." + warning)

    @util.command(name="cleardummies")
    async def cleardummies(self, ctx: commands.Context):
        """DEV: remove all dummy teams and their roles (only before the tournament starts)."""
        t = self.bot.store.get(ctx.guild.id)
        removed = dummies.clear_dummy_teams(t)
        self.bot.store.save(ctx.guild.id)

        warning = ""
        try:
            for team in removed:
                await team_roles.delete_role(ctx.guild, team.role_id)
        except discord.HTTPException as e:
            log.warning("Couldn't delete some dummy team roles: %r", e)
            warning = "\n:warning: I couldn't delete all their roles (I need **Manage Roles**). Please delete the leftover `Team: Dummy ...` roles by hand."
        await ctx.send(f"Removed {len(removed)} dummy team(s). {len(t.teams)} team(s) left." + warning)

    @util.command(name="dummyreport", usage="<1|2>")
    async def dummyreport(self, ctx: commands.Context, winner: int):
        """DEV: in a set's channel, its dummy team(s) pick team 1 or 2 as the current match's winner (no logs)."""
        report_cog = self.bot.get_cog("Report")
        if report_cog is None:
            raise UtilError("The report cog isn't loaded.")
        await ctx.send(await report_cog.dummy_vote(ctx.guild, ctx.channel.id, winner, str(ctx.author.id)))

    # -- errors ---------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if checks.is_silent(error):
            return
        error = getattr(error, "original", error)
        if isinstance(error, (UtilError, reg.RegistrationError, ReportError)):
            await ctx.send(str(error))
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        elif isinstance(error, discord.Forbidden):
            log.warning("Missing permission while running %s: %r", ctx.command, error)
            await ctx.send("I'm missing the **Manage Expressions** permission (Manage Emojis and Stickers). "
                           "Grant it and run the command again; it only adds what's missing.")
        elif isinstance(error, (StorageError, OSError)):
            log.error("Problem while running %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong reading data or files. Check the bot's log.")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Util(bot))