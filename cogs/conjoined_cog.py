"""!conjoined commands: running the tournament's sets and matches.

Words used: a *set* is one matchup between two teams (shown with an ID like #12);
its *matches* are the individual games, each with a deck and a stake.

  Managers only (Administrator permission):
    !conjoined init                       create stage 1 and pair up round 1
    !conjoined add_matches_all <deck> <stake>
    !conjoined start_matches_all          open a private channel for every set
    !conjoined next_round                 once every set is decided: pair the next round/stage
    !conjoined list_matchups              dev: sets of the current round + IDs
    !conjoined list_matches <set ID>      dev: matches of one set + status
  Everyone:
    !conjoined standings
    !conjoined roundstats [stage] [round]   compare teams (rerolls, money, score) from their logs

This file only reads commands and writes replies. The rules live in matchups.py
and the channel handling in match_channels.py, so later features (add a match to
one set, archive a set when its result is reported) can call those directly.
"""
import logging

import discord
from discord.ext import commands

import discord_text
import match_channels
import matchups
import reports
from emojis import combo_label
from matchups import MatchupError
from persistence import StorageError
import checks

log = logging.getLogger(__name__)


manager_only = checks.manager_only


def _ids(sets) -> str:
    return " ".join(f"#{s.set_id}" for s in sets)


class Conjoined(commands.Cog):
    """Run the Conjoined tournament."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    def _tournament(self, ctx: commands.Context):
        return self.bot.store.get(ctx.guild.id)

    def _save(self, ctx: commands.Context) -> None:
        self.bot.store.save(ctx.guild.id)

    async def _refresh_widgets(self, ctx: commands.Context, set_ids) -> None:
        """Post the voting widget for sets that just got a match or a channel (see report_cog)."""
        report_cog = self.bot.get_cog("Report")
        if report_cog is not None:
            await report_cog.refresh_widgets(ctx.guild, set_ids)

    # -- commands -------------------------------------------------------------

    @commands.group(name="conjoined", invoke_without_command=True)
    async def conjoined(self, ctx: commands.Context):
        """Run the tournament."""
        # The group stays open so everyone can use standings/roundstats, but its help is managers-only
        if checks.is_manager(ctx.author):
            await ctx.send_help(ctx.command)

    @conjoined.command(name="init")
    @manager_only()
    async def init(self, ctx: commands.Context):
        """Create stage 1 and pair up round 1 (needs 16 teams). Closes registration."""
        t = self._tournament(ctx)
        matchups.initialize(t)
        self._save(ctx)
        count = len(matchups.require_current_round(t).matchups)
        await ctx.send(f"Stage 1 is set up with {count} matchups in round 1, and team registration is now closed. "
                       "See them with `!conjoined list_matchups`.")

    @conjoined.command(name="add_matches_all", usage="<deck> <stake>")
    @manager_only()
    async def add_matches_all(self, ctx: commands.Context, deck: str, stake: str):
        """Add a match with this deck and stake to every set of the current round that needs one."""
        t = self._tournament(ctx)
        d, s = matchups.find_deck(deck), matchups.find_stake(stake)
        added, skipped = matchups.add_matches_all(t, d, s)
        if added:
            self._save(ctx)
        if added:
            msg = f"Added a {combo_label(ctx.guild, d, s)} match to {len(added)} set(s)."
            if skipped:
                msg += (f"\nSkipped {len(skipped)} set(s) that are already decided or still have an "
                        f"unfinished match: {_ids(skipped)}")
        else:
            msg = (f"Nothing added: all {len(skipped)} set(s) are already decided or still have an "
                   f"unfinished match ({_ids(skipped)}).")
        await ctx.send(msg)
        await self._refresh_widgets(ctx, [s.set_id for s in added])

    @conjoined.command(name="start_matches_all")
    @manager_only()
    async def start_matches_all(self, ctx: commands.Context):
        """Open a private channel for every set of the current round, and ping both teams."""
        t = self._tournament(ctx)
        pending = [s for s in matchups.require_current_round(t).matchups if match_channels.needs_start(s)]
        if not pending:
            raise MatchupError("Nothing to start: every set in the current round is already started or decided.")
        missing = match_channels.teams_missing_roles(ctx.guild, pending)
        if missing:
            raise MatchupError("These teams have no Discord role yet: " + ", ".join(missing)
                               + ". Run `!team syncroles` first. Nothing was started.")

        await ctx.send(f"Starting {len(pending)} set(s)...")
        started, failed = [], []
        async with ctx.typing():
            for set_id in [s.set_id for s in pending]:
                try:
                    await match_channels.start_set(self.bot.store, ctx.guild, set_id)
                except discord.Forbidden:
                    raise  # a missing permission will fail every set: stop and say so
                except (discord.HTTPException, MatchupError) as e:
                    log.warning("Couldn't start set #%s: %r", set_id, e)
                    failed.append(set_id)
                else:
                    started.append(set_id)
                    # Signal for other cogs (e.g. the stream cog assigns the set's slots)
                    self.bot.dispatch("set_started", ctx.guild, set_id)

        t = self._tournament(ctx)
        lines = []
        if started:
            lines.append("Started: " + ", ".join(f"#{i} <#{t.find_set(i).channel_id}>" for i in started))
        if failed:
            lines.append(":warning: Couldn't start: " + ", ".join(f"#{i}" for i in failed)
                         + ". Check the bot's log, then run this command again; it only starts what's missing.")
        await ctx.send("\n".join(lines))
        await self._refresh_widgets(ctx, started)

    @conjoined.command(name="next_round")
    @manager_only()
    async def next_round(self, ctx: commands.Context):
        """Start the next round once every set is decided; moves on to the next stage when it's time."""
        t = self._tournament(ctx)
        prev_stage = t.cur_stage_idx
        eliminated = matchups.next_round(t)
        self._save(ctx)

        rnd = matchups.require_current_round(t)
        stage = t.stages[t.cur_stage_idx]
        lines = []
        if t.cur_stage_idx != prev_stage:
            lines.append(f"Stage {prev_stage + 1} is over.")
        placements = t.get_placements()
        if 3 in placements and placements[3] in eliminated:
            lines.append(f"3rd place: **{matchups._esc(placements[3].name)}**")
        elif eliminated:
            lines.append(f"Eliminated: {matchups.team_names(eliminated)}")
        if t.cur_stage_idx == 1 and prev_stage == 0:
            lines.append(f"**{matchups.team_names(t.stage_1_undefeated())}** went undefeated and goes "
                         "straight to the final in stage 3.")
        lines += ["", f"**Stage {t.cur_stage_idx + 1}, round {stage.cur_round_idx + 1}** "
                      f"(best of {rnd.matchups[0].best_of}):"]
        lines += [matchups.format_set(s) for s in rnd.matchups]
        lines += ["", "Next: `!conjoined add_matches_all <deck> <stake>`, then `!conjoined start_matches_all`."]
        for i, block in enumerate(discord_text.chunks(lines, discord_text.DESCRIPTION_LIMIT)):
            await ctx.send(embed=discord.Embed(title="Next round" if i == 0 else None, description=block))

    @conjoined.command(name="list_matchups")
    @manager_only()
    async def list_matchups(self, ctx: commands.Context):
        """DEV: list the sets of the current round with their IDs."""
        t = self._tournament(ctx)
        rnd = matchups.require_current_round(t)
        stage = t.stages[t.cur_stage_idx]
        lines = [matchups.format_set(s) for s in rnd.matchups] or ["No matchups."]
        for i, block in enumerate(discord_text.chunks(lines, discord_text.DESCRIPTION_LIMIT)):
            title = f"Stage {t.cur_stage_idx + 1}, round {stage.cur_round_idx + 1}: matchups" if i == 0 else None
            await ctx.send(embed=discord.Embed(title=title, description=block))

    @conjoined.command(name="list_matches", usage="<set ID>")
    @manager_only()
    async def list_matches(self, ctx: commands.Context, set_id: int):
        """DEV: list all matches of one set and their status."""
        s = matchups.require_set(self._tournament(ctx), set_id)
        lines = [f"{n}. {combo_label(ctx.guild, m.deck, m.stake)}: {matchups.match_status(m)}"
                 for n, m in enumerate(s.matches, start=1)]
        embed = discord.Embed(
            title=f"Set #{s.set_id}",
            description=f"{matchups.format_set(s)}\n{matchups.set_status(s)}\n\n"
                        + ("\n".join(lines) or "No matches yet. Use `!conjoined add_matches_all`."),
        )
        await ctx.send(embed=embed)

    @conjoined.command(name="standings")
    async def standings(self, ctx: commands.Context):
        """Show the current stage and round, the standings, and the eliminated teams."""
        t = self._tournament(ctx)
        embed = discord.Embed(title="Conjoined standings")
        embed.add_field(name="Current stage & round", value=matchups.progress_text(t), inline=False)
        discord_text.add_fields(embed, "Standings (set wins-losses)", matchups.standings_text(t).split("\n"))
        discord_text.add_fields(embed, "Eliminated teams", [matchups.eliminated_text(t)])
        await ctx.send(embed=embed)

    @conjoined.command(name="roundstats", usage="[stage] [round]")
    async def roundstats(self, ctx: commands.Context, stage: int = None, round_no: int = None):
        """Compare the teams of a round (default: the current one) using their uploaded logs."""
        t = self._tournament(ctx)
        matchups.require_current_round(t)
        stage_idx = t.cur_stage_idx if stage is None else stage - 1
        if not 0 <= stage_idx < len(t.stages):
            raise MatchupError(f"There's no stage {stage} yet.")
        st = t.stages[stage_idx]
        round_idx = (st.cur_round_idx if stage is None or stage_idx == t.cur_stage_idx else len(st.rounds) - 1) \
            if round_no is None else round_no - 1
        if not 0 <= round_idx < len(st.rounds):
            raise MatchupError(f"Stage {stage_idx + 1} has no round {round_no}.")

        stats, verified, finished = reports.round_stats(st.rounds[round_idx])
        embed = discord.Embed(title=f"Stage {stage_idx + 1}, round {round_idx + 1}: team stats")
        if not stats:
            embed.description = (f"No logs to compare yet ({finished} finished match(es), none with both teams' "
                                 "logs verified).")
            return await ctx.send(embed=embed)
        embed.description = (f"From the logs of {verified} of {finished} finished match(es). Matches decided "
                             "without both logs aren't counted.")

        def board(key, fmt):
            ranked = sorted((x for x in stats if key(x) is not None), key=key, reverse=True)[:5]
            return "\n".join(f"{i}. **{matchups._esc(x.team.name)}**: {fmt(x)}" for i, x in enumerate(ranked, 1)) or "-"

        games = lambda x: f" ({x.games} game{'s' if x.games != 1 else ''})"
        embed.add_field(name="Most rerolls", inline=False,
                        value=board(lambda x: x.rerolls, lambda x: f"{x.rerolls}{games(x)}"))
        embed.add_field(name="Most money spent", inline=False,
                        value=board(lambda x: x.money_spent, lambda x: f"${x.money_spent}{games(x)}"))
        embed.add_field(name="Highest score (PvP blind)", inline=False,
                        value=board(lambda x: x.highest_score,
                                    lambda x: f"{x.highest_score:,} by {matchups._esc(x.highest_by or '?')}"))
        await ctx.send(embed=embed)

    # -- errors ---------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if checks.is_silent(error):
            return
        error = getattr(error, "original", error)
        if isinstance(error, MatchupError):
            await ctx.send(str(error))
        elif isinstance(error, (StorageError, OSError)):
            log.error("Storage problem while running %s", ctx.command, exc_info=error)
            await ctx.send("Couldn't load or save the tournament data, so nothing was changed. Check the bot's log.")
        elif isinstance(error, discord.Forbidden):
            log.warning("Missing permission while running %s: %r", ctx.command, error)
            await ctx.send("I'm missing a Discord permission (I need Manage Channels, Manage Roles, "
                           "View Channel, Send Messages and Read Message History). Grant it and run the "
                           "command again; it only does what's missing.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Conjoined(bot))