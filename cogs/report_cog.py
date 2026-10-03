"""Reporting match results: the widget in each set channel, and the managers' !report.

The widget (players)
  Every started set's channel has a widget for its current match, with a
  "<team> won" button per team. Each team picks the winner (any member of the team
  can click; the last click counts). When both teams picked the same winner, the
  widget turns into the log upload step with a link to the match's report page.
  The result is recorded once both logs are in and show that winner.
  The widget is posted by the bot as soon as a match needs a result, and every
  WIDGET_MOVE_MINUTES it is re-posted at the bottom of the channel if other
  messages came after it.
  While testing, a manager who isn't on either team votes for the set's dummy
  team(s), which can't click themselves.

Managers only:
  !report manual                        (in the set's channel) decide the current match
                                        yourself: no logs needed, closes its report page
  !report correct <match>               (in the set's channel) change a finished match's
  !report correct <set ID> <match>      winner, or reopen the set's last match. Shows the
                                        match's result history first.
  Results can only be changed while the set's round is the current round, and every
  change is announced in the set's channel and kept in the match's history.

The rules live in reports.py, the upload page in report_web.py; this file only
talks to Discord. It also runs the web server for the page (see the REPORT_*
settings below) for as long as the cog is loaded.

Settings (.env, all optional):
  REPORT_PUBLIC_URL   https://conjoined.balala.pro   the address players open
  REPORT_BIND_HOST    127.0.0.1                      where the reverse proxy connects to
  REPORT_PORT         8080
"""
import asyncio
import logging
import os
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

import checks
import reports
from checks import is_manager
from dummies import is_dummy_team
from emojis import combo_label
from models.tournament import GameState, TourneySet
from persistence import StorageError
from report_web import ReportWeb
from reports import ReportError, ReportStatus

log = logging.getLogger(__name__)

PUBLIC_URL = os.getenv("REPORT_PUBLIC_URL", "https://conjoined.balala.pro").rstrip("/")
BIND_HOST = os.getenv("REPORT_BIND_HOST", "127.0.0.1")
PORT = int(os.getenv("REPORT_PORT", "8080"))
WIDGET_MOVE_MINUTES = 2


def _set_for_channel(t, channel_id: int) -> TourneySet | None:
    for stage in t.stages:
        for r in stage.rounds:
            for s in r.matchups:
                if s.channel_id == channel_id:
                    return s
    return None


def _team(s: TourneySet, slot: int):
    return s.team1 if slot == 1 else s.team2


def _voter_slots(member, s: TourneySet) -> list[int]:
    """The team slot(s) this member votes for: their own team. A manager on neither
    team votes for the set's dummy teams (testing), since those can't click."""
    role_ids = {r.id for r in getattr(member, "roles", [])}
    own = [slot for slot in reports.SLOTS
           if _team(s, slot).role_id is not None and _team(s, slot).role_id in role_ids]
    if own:
        return own if len(own) == 1 else []  # on both teams: ambiguous, so no vote
    if isinstance(member, discord.Member) and is_manager(member):
        return [slot for slot in reports.SLOTS if is_dummy_team(_team(s, slot))]
    return []


def _page_url(report: reports.MatchReport) -> str:
    return f"{PUBLIC_URL}/r/{report.token}"


def _mark(ok: bool, pending: bool = False) -> str:
    return ":white_check_mark:" if ok else (":white_large_square:" if pending else ":x:")


class VoteButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"conjoined-report:(?P<set_id>\d+):(?P<match_no>\d+):(?P<slot>[12])"):
    """'<team> won' button on the widget. The custom id carries everything, so it survives restarts."""

    def __init__(self, set_id: int, match_no: int, slot: int, label: str = "Won"):
        super().__init__(discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.primary if slot == 1 else discord.ButtonStyle.secondary,
            custom_id=f"conjoined-report:{set_id}:{match_no}:{slot}",
        ))
        self.set_id, self.match_no, self.slot = set_id, match_no, slot

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["set_id"]), int(match["match_no"]), int(match["slot"]), item.label or "Won")

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Report")
        await cog.vote(interaction, self.set_id, self.match_no, self.slot)


class ManagerButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"conjoined-admin:(?P<kind>manual|correct):(?P<set_id>\d+):(?P<match_no>\d+):(?P<choice>[012])"):
    """Buttons of !report manual / !report correct. choice: 1 or 2 = that team won, 0 = reopen."""

    def __init__(self, kind: str, set_id: int, match_no: int, choice: int, label: str = "?"):
        super().__init__(discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.danger if choice == 0 else discord.ButtonStyle.primary,
            custom_id=f"conjoined-admin:{kind}:{set_id}:{match_no}:{choice}",
        ))
        self.kind, self.set_id, self.match_no, self.choice = kind, set_id, match_no, choice

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["kind"], int(match["set_id"]), int(match["match_no"]), int(match["choice"]), item.label or "?")

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Report")
        await cog.manager_choice(interaction, self.kind, self.set_id, self.match_no, self.choice)


def _history_text(m) -> str:
    names = {"logs": "confirmed by logs", "manual": "decided by a manager", "correct": "corrected by a manager",
             "reopen": "reopened by a manager"}
    results = {"TEAM1_WIN": "team 1 won", "TEAM2_WIN": "team 2 won", None: "no result"}
    lines = []
    for h in m.history[-5:]:
        who = f" (<@{h['by']}>)" if h.get("by") else ""
        lines.append(f"<t:{int(h['at'])}:f> {names.get(h['action'], h['action'])}{who}: "
                     f"{results.get(h.get('result'), h.get('result'))}")
    return "\n".join(lines) or "No changes recorded."


def widget_message(guild, s: TourneySet, n: int, m, status: ReportStatus, now: float):
    """(embed, view) of the voting widget for match n of set s."""
    r = m.report
    lines = [combo_label(guild, m.deck, m.stake), "",
             "**Who won?** When the match is done, each team picks the winner below."]
    for slot in reports.SLOTS:
        pick = r.votes.get(slot)
        lines.append(f"{_mark(pick is not None, pending=True)} **{_team(s, slot).name}** "
                     + (f"picked **{_team(s, pick).name}**" if pick is not None else "hasn't picked yet"))

    agreed = r.agreed_winner
    view = discord.ui.View(timeout=None)
    for slot in reports.SLOTS:
        view.add_item(VoteButton(s.set_id, n, slot, f"{_team(s, slot).name} won"))

    if agreed is None:
        if len(r.votes) == 2:
            lines += ["", ":warning: The teams picked different winners. Talk it out, or ask a manager."]
    else:
        lines += ["", f"Both teams agree: **{_team(s, agreed).name}** won.",
                  "**Next:** each team uploads its Lovely log (in the Balatro folder under `Mods/lovely/log`) on "
                  "the report page. The result is recorded once both logs show this game."]
        for slot in reports.SLOTS:
            up, has = status.uploaded[slot], status.has_game[slot]
            detail = ("log uploaded" if has else "log uploaded, but it has no finished game on this deck/stake") \
                if up else "no log yet"
            lines.append(f"{_mark(up and has, pending=not up)} **{_team(s, slot).name}**: {detail}")
        if status.uploaded[1] and status.uploaded[2] and status.has_game[1] and status.has_game[2] and not status.paired:
            lines.append(":warning: The two logs don't show the same game (same seed, same two players, one win "
                         "and one loss). One of them may be the wrong file.")
        if status.conflict:
            lines.append(f":warning: The logs show **{_team(s, status.log_winner).name}** won. Change your pick, "
                         "or ask a manager if the logs are wrong.")
        if reports.page_is_open(r, now):
            lines.append(f"The link expires <t:{int(r.expires_at)}:R>.")
            view.add_item(discord.ui.Button(label="Upload logs", url=_page_url(r)))

    embed = discord.Embed(title=f"Set #{s.set_id}, match {n}", description="\n".join(lines))
    return embed, view


class Report(commands.Cog):
    """Report match results."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.web = ReportWeb(bot.store, Path(bot.store.directory) / "logs", self.after_upload)
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        self.bot.add_dynamic_items(VoteButton, ManagerButton)
        await self.web.start(BIND_HOST, PORT)
        self.keep_widgets_at_bottom.start()

    async def cog_unload(self):
        self.keep_widgets_at_bottom.cancel()
        self.bot.remove_dynamic_items(VoteButton, ManagerButton)
        await self.web.stop()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    # -- the widget -------------------------------------------------------------

    async def ensure_widget(self, guild: discord.Guild, set_id: int) -> None:
        """Makes sure the set's channel shows the widget of its current match, at the bottom.
        Posts it if missing, re-posts it (and deletes the old one) if messages came after it."""
        async with self._lock(guild.id):
            t = self.bot.store.get(guild.id)
            s = t.find_set(set_id)
            if s is None or s.channel_id is None or s.channel_archived:
                return
            channel = guild.get_channel(s.channel_id)
            found = reports.current_match(s)
            if channel is None or found is None:
                return
            n, m = found
            r = reports.ensure_report(m)
            old = r.widget_message_id
            if old is not None and channel.last_message_id == old:
                return  # already at the bottom
            self.bot.store.save(guild.id)  # ensure_report may have changed the match

            embed, view = widget_message(guild, s, n, m, reports.check(m, reports.used_seeds(t, m)), time.time())
            msg = await channel.send(embed=embed, view=view)

            t = self.bot.store.get(guild.id)  # look it up again after the await
            s = t.find_set(set_id)
            m = s.matches[n - 1] if s is not None and n <= len(s.matches) else None
            if m is None or m.report is None:
                return
            m.report.widget_message_id = msg.id
            self.bot.store.save(guild.id)
            if old is not None:
                await self._delete_message(channel, old)

    async def update_widget(self, guild: discord.Guild, set_id: int, match_no: int) -> None:
        """Redraws the widget where it is (after an upload)."""
        t = self.bot.store.get(guild.id)
        s = t.find_set(set_id)
        if s is None or s.channel_id is None or not 1 <= match_no <= len(s.matches):
            return
        m = s.matches[match_no - 1]
        channel = guild.get_channel(s.channel_id)
        if channel is None or m.report is None or m.report.widget_message_id is None:
            return await self.ensure_widget(guild, set_id)
        embed, view = widget_message(guild, s, match_no, m, reports.check(m, reports.used_seeds(t, m)), time.time())
        try:
            await channel.get_partial_message(m.report.widget_message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            await self.ensure_widget(guild, set_id)

    @staticmethod
    async def _delete_message(channel, message_id: int) -> None:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("Couldn't delete old widget %s: %r", message_id, e)

    async def refresh_widgets(self, guild: discord.Guild, set_ids) -> None:
        """Posts/moves the widgets of these sets now (e.g. right after matches were added)."""
        for set_id in set_ids:
            try:
                await self.ensure_widget(guild, set_id)
            except discord.HTTPException as e:
                log.warning("Couldn't post the widget of set #%s: %r", set_id, e)

    @tasks.loop(minutes=WIDGET_MOVE_MINUTES)
    async def keep_widgets_at_bottom(self):
        for guild_id in self.bot.store.guild_ids():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                rnd = self.bot.store.get(guild_id).get_current_round()
            except StorageError:
                continue
            if rnd is not None:
                await self.refresh_widgets(guild, [s.set_id for s in rnd.matchups])

    @keep_widgets_at_bottom.before_loop
    async def _before_widget_loop(self):
        await self.bot.wait_until_ready()

    # -- voting -----------------------------------------------------------------

    async def vote(self, interaction: discord.Interaction, set_id: int, match_no: int, winner_slot: int):
        async def reply_private(text: str):
            await interaction.response.send_message(text, ephemeral=True)

        if interaction.guild is None:
            return await reply_private("This only works inside a server.")
        try:
            t = self.bot.store.get(interaction.guild.id)
        except StorageError:
            log.exception("Couldn't load the tournament for a vote")
            return await reply_private("Couldn't load the tournament data. Tell a manager.")
        s = t.find_set(set_id)
        if s is None or not 1 <= match_no <= len(s.matches):
            return await reply_private("This match doesn't exist anymore.")
        m = s.matches[match_no - 1]
        if m.state == GameState.FINISHED:
            return await reply_private("This match already has a result.")
        slots = _voter_slots(interaction.user, s)
        if not slots:  # not on either team: not allowed to vote, so nothing happens
            return await checks.ignore_click(interaction)

        now = time.time()
        for slot in slots:
            reports.vote(m, slot, winner_slot, str(interaction.user.id), now)
        status, concluded = self._conclude(t, m)
        self.bot.store.save(interaction.guild.id)

        if concluded:
            await interaction.response.defer()
            return await self._finish(interaction.guild, s, match_no, m, status)
        embed, view = widget_message(interaction.guild, s, match_no, m, status, now)
        if interaction.message is not None and interaction.message.id == m.report.widget_message_id:
            await interaction.response.edit_message(embed=embed, view=view)
        else:  # clicked on an old copy of the widget
            await reply_private(f"Got it: you picked **{_team(s, winner_slot).name}**.")
            await self.update_widget(interaction.guild, set_id, match_no)

    # -- after an upload on the web page --------------------------------------

    async def after_upload(self, guild_id: int, set_id: int, match_no: int, slot: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        t = self.bot.store.get(guild_id)
        s = t.find_set(set_id)
        if s is None or not 1 <= match_no <= len(s.matches):
            return
        m = s.matches[match_no - 1]
        status, concluded = self._conclude(t, m)
        if concluded:
            self.bot.store.save(guild_id)
            await self._finish(guild, s, match_no, m, status)
        else:
            await self.update_widget(guild, set_id, match_no)

    # -- managers -------------------------------------------------------------

    @commands.group(name="report", invoke_without_command=True)
    @checks.manager_only()
    async def report(self, ctx: commands.Context):
        """MANAGERS: decide or correct match results (players use the widget in their set's channel)."""
        await ctx.send("`!report manual` (in a set's channel): decide its current match yourself.\n"
                       "`!report correct [set ID] <match number>`: change a finished match's result, or reopen it.")

    @report.command(name="manual")
    @checks.manager_only()
    async def manual(self, ctx: commands.Context):
        """MANAGERS: decide the current match of this set yourself, without logs."""
        t = self.bot.store.get(ctx.guild.id)
        s = _set_for_channel(t, ctx.channel.id)
        if s is None:
            raise ReportError("Use this inside the set's channel.")
        reason = reports.why_no_change(t, s)
        if reason is not None:
            raise ReportError(reason)
        found = reports.current_match(s)
        if found is None:
            raise ReportError("There's no match without a result in this set. To change a result, use "
                              "`!report correct <match>`.")
        n, m = found
        view = discord.ui.View(timeout=None)
        for slot in reports.SLOTS:
            view.add_item(ManagerButton("manual", s.set_id, n, slot, f"{_team(s, slot).name} won"))
        note = " The teams' picks and log reporting will be skipped." if m.report is not None else ""
        await ctx.send(f"**Manager report** for set #{s.set_id}, match {n} ({combo_label(ctx.guild, m.deck, m.stake)}): "
                       f"which team won?{note}", view=view)

    @report.command(name="correct", usage="[set ID] <match number>")
    @checks.manager_only()
    async def correct(self, ctx: commands.Context, first: int, second: int = None):
        """MANAGERS: change a finished match's winner, or reopen the set's last match."""
        t = self.bot.store.get(ctx.guild.id)
        if second is None:
            s, n = _set_for_channel(t, ctx.channel.id), first
            if s is None:
                raise ReportError("Use this inside the set's channel, or give the set ID: "
                                  "`!report correct <set ID> <match number>`.")
        else:
            s, n = t.find_set(first), second
            if s is None:
                raise ReportError(f"There's no set with ID #{first}.")
        reason = reports.why_no_change(t, s)
        if reason is not None:
            raise ReportError(reason)
        if not 1 <= n <= len(s.matches):
            raise ReportError(f"Set #{s.set_id} has no match {n}.")
        m = s.matches[n - 1]
        if m.state != GameState.FINISHED:
            raise ReportError(f"Match {n} has no result yet. Use `!report manual` in the set's channel to decide it.")

        view = discord.ui.View(timeout=None)
        for slot in reports.SLOTS:
            view.add_item(ManagerButton("correct", s.set_id, n, slot, f"{_team(s, slot).name} won"))
        if n == len(s.matches):
            view.add_item(ManagerButton("correct", s.set_id, n, 0, "Reopen match"))
        current = {"TEAM1_WIN": s.team1.name, "TEAM2_WIN": s.team2.name}.get(m.result.name if m.result else None)
        embed = discord.Embed(
            title=f"Correct set #{s.set_id}, match {n}",
            description=(f"{s.team1.name} (team 1) vs {s.team2.name} (team 2), {m.deck} / {m.stake}\n"
                         f"Current result: **{current + ' won' if current else m.result.name}**\n\n"
                         f"**History**\n{_history_text(m)}\n\n"
                         "Pick the right winner. *Reopen* clears the result so the match can be played and "
                         "reported again (its uploaded logs stay saved, but it needs new ones)."),
        )
        await ctx.send(embed=embed, view=view)

    async def manager_choice(self, interaction: discord.Interaction, kind: str, set_id: int, match_no: int, choice: int):
        async def reply_private(text: str):
            await interaction.response.send_message(text, ephemeral=True)

        if interaction.guild is None or not is_manager(interaction.user):
            return await checks.ignore_click(interaction)
        t = self.bot.store.get(interaction.guild.id)
        s = t.find_set(set_id)
        if s is None or not 1 <= match_no <= len(s.matches):
            return await reply_private("This match doesn't exist anymore.")
        by, now = str(interaction.user.id), time.time()
        old_widget = s.matches[match_no - 1].report.widget_message_id if s.matches[match_no - 1].report else None
        was_decided = s.get_winner() is not None
        try:
            if kind == "manual":
                m = s.matches[match_no - 1]
                reports.manual_result(t, s, m, choice, by, now)
            else:
                m = reports.correct_result(t, s, match_no, choice or None, by, now)
        except ReportError as e:
            return await reply_private(str(e))
        self.bot.store.save(interaction.guild.id)

        # Signals for other cogs (e.g. the stream cog archives the match, frees or re-assigns slots)
        if kind == "manual":
            self.bot.dispatch("match_finished", interaction.guild, s.set_id, match_no)
        is_decided = s.get_winner() is not None
        if is_decided and not was_decided:
            self.bot.dispatch("set_decided", interaction.guild, s.set_id)
        elif was_decided and not is_decided:
            self.bot.dispatch("set_reopened", interaction.guild, s.set_id)

        # Buttons are single use: remove them, then announce to both teams.
        await interaction.response.edit_message(view=None)
        who = interaction.user.mention
        if choice == 0:
            text = (f":arrows_counterclockwise: {who} reopened match {match_no}; it has no result anymore. "
                    "Play it (again) and pick the winner on the widget.")
        else:
            verb = "decided" if kind == "manual" else "corrected"
            text = (f":white_check_mark: {who} {verb} match {match_no}: **{_team(s, choice).name}** won. "
                    f"Set score: {'{}-{}'.format(*s.get_standings())}.")
            if s.get_winner() is not None:
                text += f"\n:trophy: **{s.get_winner().name}** wins set #{s.set_id}!"
        channel = interaction.guild.get_channel(s.channel_id) if s.channel_id else None
        if channel is not None and old_widget is not None and kind == "manual":
            await self._delete_message(channel, old_widget)  # its voting is over
        await (channel or interaction.channel).send(text)
        if channel is not None and channel != interaction.channel:
            await interaction.followup.send(f"Done, and announced in {channel.mention}.", ephemeral=True)
        await self.refresh_widgets(interaction.guild, [s.set_id])  # e.g. the reopened match's widget

    # -- shared ---------------------------------------------------------------

    @staticmethod
    def _conclude(t, m) -> tuple[ReportStatus, bool]:
        was_finished = m.state == GameState.FINISHED
        status = reports.try_conclude(t, m, time.time())
        return status, not was_finished and m.state == GameState.FINISHED

    async def _finish(self, guild: discord.Guild, s: TourneySet, match_no: int, m, status: ReportStatus):
        """A match just got its result from the logs: replace its widget with the result."""
        channel = guild.get_channel(s.channel_id) if s.channel_id else None
        if channel is None:
            return
        if m.report is not None and m.report.widget_message_id is not None:
            await self._delete_message(channel, m.report.widget_message_id)
        winner = _team(s, status.log_winner)
        lines = [f"**{winner.name}** won match {match_no} ({m.deck} / {m.stake}). "
                 f"Set score: {'{}-{}'.format(*s.get_standings())}.", ""]
        for slot, g in zip(reports.SLOTS, status.pair):
            lines.append(f"**{_team(s, slot).name}** ({g.player}): {g.rerolls} rerolls, ${g.money_spent} spent, "
                         f"highest score {g.highest_score or '-'}")
        lines.append(f"Seed: `{status.pair[0].seed}`")
        if s.get_winner() is not None:
            lines += ["", f":trophy: **{s.get_winner().name}** wins set #{s.set_id}!"]
        else:
            lines += ["", "The next match can now be added."]
        await channel.send(embed=discord.Embed(title=f"Set #{s.set_id}, match {match_no}: result",
                                               description="\n".join(lines)))
        # Signals for other cogs (e.g. the stream cog archives the match, frees the set's slots)
        self.bot.dispatch("match_finished", guild, s.set_id, match_no)
        if s.get_winner() is not None:
            self.bot.dispatch("set_decided", guild, s.set_id)
        await self.refresh_widgets(guild, [s.set_id])  # the next match, if it was already added

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if checks.is_silent(error):
            return
        error = getattr(error, "original", error)
        if isinstance(error, ReportError):
            await ctx.send(str(error))
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        elif isinstance(error, (StorageError, OSError)):
            log.error("Storage problem while running %s", ctx.command, exc_info=error)
            await ctx.send("Couldn't load or save the tournament data, so nothing was changed. Check the bot's log.")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Report(bot))
