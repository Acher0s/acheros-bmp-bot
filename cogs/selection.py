"""!selection commands: put a deck/stake selection to a vote using emoji reactions.

How voting works:
  * The bot posts the options and adds the reactions 1-9 itself.
  * A player votes by clicking a number. One vote per player: clicking another
    number moves their vote (the bot removes the old reaction).
  * Only players holding the Discord role of a team that is still in the
    tournament can vote. Reactions from anyone else, and reactions that aren't
    one of the options, are removed by the bot right away, so the numbers under
    the message always match the real votes.
  * At !selection stopvote the ballots are re-checked (someone may have left or
    been eliminated meanwhile) and the counts are stored on the tournament.

The vote that is currently running is kept in memory only. Recorded results are
saved to disk, but a vote still open when the bot restarts is lost.

Bot permissions needed in the voting channel: Add Reactions, Manage Messages,
Read Message History.
"""
import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from bot import is_manager
from conjoined import VoteResults
from models.deck import Deck
from models.stake import Stake
from persistence import StorageError

log = logging.getLogger(__name__)

NUMBER_EMOJIS = [f"{i}\ufe0f\u20e3" for i in range(1, 10)]  # 1️⃣ ... 9️⃣
_NORMALIZED = [e.replace("\ufe0f", "") for e in NUMBER_EMOJIS]
MAX_OPTIONS = len(NUMBER_EMOJIS)
DEFAULT_OPTIONS = 9


class SelectionError(Exception):
    """A rule violation. The message is shown to the user in Discord as-is."""



def _option_index(emoji: discord.PartialEmoji, n_options: int) -> int | None:
    """Which option a reaction stands for, or None if it isn't one of the n options."""
    if not emoji.is_unicode_emoji():
        return None
    name = emoji.name.replace("\ufe0f", "")
    if name not in _NORMALIZED:
        return None
    idx = _NORMALIZED.index(name)
    return idx if idx < n_options else None


def _has_voting_role(tournament, roles) -> bool:
    role_ids = tournament.get_voting_role_ids()
    return any(r.id in role_ids for r in roles)


def _options_text(selection: list[tuple[Deck, Stake]]) -> str:
    return "\n".join(
        f"{emoji} **{deck}** deck / **{stake}** stake"
        for emoji, (deck, stake) in zip(NUMBER_EMOJIS, selection)
    )


def _results_text(results: VoteResults) -> str:
    total = results.count_votes()
    lines = []
    for emoji, (deck, stake), votes in zip(NUMBER_EMOJIS, results.selection, results.votes):
        pct = f" ({votes / total:.0%})" if total else ""
        lines.append(f"{emoji} **{deck}** deck / **{stake}** stake: {votes}{pct}")
    return "\n".join(lines)


@dataclass
class ActiveVote:
    selection: list[tuple[Deck, Stake]]
    message: discord.Message | None = None  # set once the vote message is posted
    ballots: dict[int, int] = field(default_factory=dict)  # user id -> option index
    warned: set[int] = field(default_factory=set)  # users already told they can't vote


class Selection(commands.Cog):
    """Selection votes. Managers (Administrator permission) only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: dict[int, ActiveVote] = {}  # guild id -> vote in progress

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not is_manager(ctx.author):
            raise commands.MissingPermissions(["administrator"])
        return True

    # -- commands -------------------------------------------------------------

    @commands.group(name="selection", invoke_without_command=True)
    async def selection(self, ctx: commands.Context):
        """Put deck/stake selections to a vote."""
        await ctx.send_help(ctx.command)

    @selection.command(name="startvote", usage="[N]")
    async def startvote(self, ctx: commands.Context, n: int = DEFAULT_OPTIONS):
        """Start a vote on N random deck/stake combos (1-9, default 9)."""
        if not 1 <= n <= MAX_OPTIONS:
            raise SelectionError(f"N must be between 1 and {MAX_OPTIONS}.")
        if ctx.guild.id in self.active:
            raise SelectionError("A vote is already running. Use `!selection stopvote` or `!selection discardvote` first.")

        perms = ctx.channel.permissions_for(ctx.guild.me)
        missing = [name for name, ok in (
            ("Add Reactions", perms.add_reactions),
            ("Manage Messages", perms.manage_messages),
            ("Read Message History", perms.read_message_history),
        ) if not ok]
        if missing:
            raise SelectionError("I need these permissions in this channel: " + ", ".join(missing) + ".")

        t = self.bot.store.get(ctx.guild.id)
        if not t.get_voting_role_ids():
            raise SelectionError("No active team has a role yet, so nobody could vote. Run `!team syncroles` first.")

        vote = ActiveVote(selection=t.generate_selection(n))
        self.active[ctx.guild.id] = vote  # reserve the slot before any `await`

        embed = discord.Embed(title="Vote: pick a deck and stake", description=_options_text(vote.selection))
        embed.set_footer(text="React with a number to vote (one vote per player, you can change it). "
                              "Only members of teams still in the tournament can vote; other reactions are removed.")
        try:
            vote.message = await ctx.send(embed=embed)
            for emoji in NUMBER_EMOJIS[:n]:
                await vote.message.add_reaction(emoji)
        except Exception:
            self.active.pop(ctx.guild.id, None)
            if vote.message is not None:
                try:
                    await vote.message.delete()
                except discord.HTTPException:
                    pass
            raise

    @selection.command(name="stopvote")
    async def stopvote(self, ctx: commands.Context):
        """Stop the vote and record the result."""
        vote = self.active.pop(ctx.guild.id, None)  # from here on, new reactions are ignored
        if vote is None:
            raise SelectionError("There's no vote running.")

        try:
            counts = [0] * len(vote.selection)
            ignored = 0
            for uid, idx in list(vote.ballots.items()):
                if await self._still_eligible(ctx.guild, uid):
                    counts[idx] += 1
                else:
                    ignored += 1

            # Rule: change -> save with no `await` in between.
            t = self.bot.store.get(ctx.guild.id)
            results = VoteResults(vote.selection)
            results.votes = counts
            t.vote_results.append(results)
            self.bot.store.save(ctx.guild.id)
        except Exception:
            self.active.setdefault(ctx.guild.id, vote)  # nothing was recorded: let them retry
            raise

        await self._close_message(vote, "Vote closed")
        embed = discord.Embed(title=f"Vote closed and recorded: {sum(counts)} valid vote(s)",
                              description=_results_text(results))
        if ignored:
            embed.set_footer(text=f"{ignored} vote(s) ignored because the voter is no longer on an active team.")
        await ctx.send(embed=embed)

    @selection.command(name="discardvote")
    async def discardvote(self, ctx: commands.Context):
        """Stop the vote without recording anything."""
        vote = self.active.pop(ctx.guild.id, None)
        if vote is None:
            raise SelectionError("There's no vote running.")
        await self._close_message(vote, "Vote discarded")
        await ctx.send("Vote discarded. Nothing was recorded.")

    # -- helpers --------------------------------------------------------------

    async def _still_eligible(self, guild: discord.Guild, uid: int) -> bool:
        t = self.bot.store.get(guild.id)
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                return False  # left the server
        return _has_voting_role(t, member.roles)

    async def _close_message(self, vote: ActiveVote, title: str) -> None:
        """Mark the vote message as finished and clear its reactions (best effort)."""
        if vote.message is None:
            return
        try:
            await vote.message.edit(embed=discord.Embed(title=title, description=_options_text(vote.selection)))
            await vote.message.clear_reactions()
        except discord.HTTPException as e:
            log.warning("Couldn't tidy up the vote message: %r", e)

    async def _remove_reaction(self, vote: ActiveVote, emoji, user_id: int) -> None:
        try:
            await vote.message.remove_reaction(emoji, discord.Object(user_id))
        except discord.HTTPException as e:
            log.warning("Couldn't remove a reaction from %s: %r", user_id, e)

    def _vote_for(self, payload: discord.RawReactionActionEvent) -> ActiveVote | None:
        """The running vote this reaction belongs to, if any."""
        if payload.guild_id is None:
            return None
        vote = self.active.get(payload.guild_id)
        if vote is None or vote.message is None or vote.message.id != payload.message_id:
            return None
        return vote

    # -- reactions ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        vote = self._vote_for(payload)
        if vote is None:
            return

        idx = _option_index(payload.emoji, len(vote.selection))
        if idx is None:  # not one of the options
            await self._remove_reaction(vote, payload.emoji, payload.user_id)
            return

        t = self.bot.store.get(payload.guild_id)
        member = payload.member
        if member is None or not _has_voting_role(t, member.roles):
            await self._remove_reaction(vote, payload.emoji, payload.user_id)
            if payload.user_id not in vote.warned:  # tell them once, not on every click
                vote.warned.add(payload.user_id)
                await vote.message.channel.send(
                    f"<@{payload.user_id}> only members of teams still in the tournament can vote.",
                    delete_after=10,
                )
            return

        # Eligible. One vote per player: record the new choice first, then remove the
        # old reaction (its removal event is ignored because the ballot no longer matches).
        old = vote.ballots.get(payload.user_id)
        vote.ballots[payload.user_id] = idx
        if old is not None and old != idx:
            await self._remove_reaction(vote, NUMBER_EMOJIS[old], payload.user_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        vote = self._vote_for(payload)
        if vote is None:
            return
        idx = _option_index(payload.emoji, len(vote.selection))
        if idx is not None and vote.ballots.get(payload.user_id) == idx:
            del vote.ballots[payload.user_id]  # they took their own vote back

    # -- errors ---------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, SelectionError):
            await ctx.send(str(error))
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Only administrators can use selection commands.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Selection commands only work inside a server.")
        elif isinstance(error, (StorageError, OSError)):
            log.error("Storage problem while running %s", ctx.command, exc_info=error)
            await ctx.send("Couldn't load or save the tournament data, so nothing was recorded. Check the bot's log.")
        elif isinstance(error, discord.Forbidden):
            log.warning("Missing permission while running %s: %r", ctx.command, error)
            await ctx.send("I'm missing a Discord permission in this channel (Add Reactions / Manage Messages / Read Message History).")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Selection(bot))