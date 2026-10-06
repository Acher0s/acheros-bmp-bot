"""!stream commands: the tournament's stream setup, through the media control service.

Players never use these. Credentials are never posted in a channel: they're only shown in
private (ephemeral) replies to the manager who clicks a button.

  Managers only (Administrator permission):
    !stream casterrole @Role         which role counts as caster; creates/repairs #stream-casters
    !stream export                   button: sync logins and get the CSVs privately
    !stream reset <@player|login>    button: new password, shown privately
    !stream overview                 every team: who streamed successfully, who's live, slots, delay
    !stream inspect <team>           details of the team's current stream
    !stream assign <slot> <team|none>  manual slot assignment (normally automatic)
    !stream kick <team>              disconnect whoever is publishing on the team's path
    !stream twitch <team> <channel|off>  Twitch passthrough: the team's feed shows their Twitch stream
    !stream delay [minutes] [confirm]  show / preview / apply the delay (5-120 min)
    !stream archive                  archive health
    !stream vods <team | set ID>     archived files
  Managers and casters:
    !stream lineup                   which team is on each feed now (feed time) and what's next

Automatic (signals from the other cogs):
  set started    -> slots s<position>t1/t2 assigned, status panel posted in the set's channel
  !bala start    -> start time recorded for the current match of every set in progress
  match result   -> match manifest sent (one archive file per team per match)
  set decided    -> slots freed; a whole-set archive if no match had a start time
  set reopened   -> slots assigned again
  reset          -> team paths, panels and start times forgotten; slots and Twitch passthrough cleared
  every 10 s     -> panels refreshed (edited only when something changed)

Settings (.env):
  STREAM_CONTROL_URL     the media control service, e.g. http://192.168.100.159:9000
  STREAM_CONTROL_TOKEN   its CONTROL_API_TOKEN
Casters are the members holding the caster role, which needs the bot's Server Members Intent.
"""
import asyncio
import csv
import io
import json
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks

import checks
import discord_text
import reports

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
PANEL_SECONDS = 10
REFUSAL_NOTICE_SECONDS = 180  # how long the panel mentions a refused teammate
ARCHIVE_PAD_SECONDS = 60  # added before and after each match in the archive
MIN_DELAY, MAX_DELAY = 5, 120
TEAM_COUNT, SET_COUNT = 16, 8
CASTERS_CHANNEL_NAME = "stream-casters"
SLOT_RE = re.compile(r"^s([1-8])t([12])$")


class StreamError(Exception):
    """Shown to the manager as-is."""


def _ts(seconds: float, style: str = "t") -> str:
    return f"<t:{int(seconds)}:{style}>"


def _esc(text) -> str:
    return discord.utils.escape_markdown(str(text))


def _key(name: str) -> str:
    """How team names are compared: any case, runs of spaces count as one (as in registration.py)."""
    return " ".join(name.split()).casefold()


def _text_cell(value) -> str:
    """A name for a CSV cell. Spreadsheets run a cell starting with = + - @ as a formula, so such a
    name gets a leading apostrophe, which they hide."""
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


SETTING_NAMES = {"codec": "codec", "bframes": "B-frames", "keyframes": "keyframes", "bitrate": "bitrate",
                 "resolution": "resolution", "fps": "frame rate", "audio": "sound"}


def _settings_problems(settings: dict | None) -> list[str]:
    """'B-frames (yes)', 'keyframes (at least 8.3 s)', ... for the checks a team's stream fails."""
    if not settings:
        return []
    return [f"{SETTING_NAMES.get(c['key'], c['key'])} ({c['value']})" for c in settings.get("checks", []) if not c["ok"]]


def _twitch_text(twitch: dict) -> str:
    """'twitch.tv/name (live)' for a team's Twitch passthrough setting."""
    live = {True: "live", False: "offline"}.get(twitch.get("live"), "not checked yet")
    return f"twitch.tv/{twitch['channel']} ({live})"


def _csv_file(name: str, header: list[str], rows: list[list]) -> discord.File:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    # With a BOM, Excel reads the file as UTF-8 (accents and emoji in names) instead of the local code page
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=name)


def _find_set(t, set_id: int):
    """(stage index, round index, position in round starting at 1, set) or None."""
    for si, stage in enumerate(t.stages):
        for ri, rnd in enumerate(stage.rounds):
            for pos, s in enumerate(rnd.matchups, start=1):
                if s.set_id == set_id:
                    return si, ri, pos, s
    return None


class StreamState:
    """The cog's own per-server state, kept out of the tournament data (data/stream_<guild>.json)."""

    def __init__(self, path: Path):
        self.path = path
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                log.error("Couldn't read %s, starting fresh", path)
        self.caster_role_id: int | None = data.get("caster_role_id")
        self.casters_channel_id: int | None = data.get("casters_channel_id")
        self.team_paths: dict[str, str] = data.get("team_paths", {})  # team name -> teamNN
        self.panels: dict[str, int] = data.get("panels", {})  # set id -> panel message id
        self.set_starts: dict[str, float] = data.get("set_starts", {})
        self.match_starts: dict[str, float] = data.get("match_starts", {})  # "set:match" -> start
        self.archived_matches: dict[str, list[int]] = data.get("archived_matches", {})  # set id -> match numbers

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "caster_role_id": self.caster_role_id, "casters_channel_id": self.casters_channel_id,
            "team_paths": self.team_paths, "panels": self.panels, "set_starts": self.set_starts,
            "match_starts": self.match_starts, "archived_matches": self.archived_matches,
        }, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class ExportButton(discord.ui.DynamicItem[discord.ui.Button], template=r"stream-export"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Export logins", style=discord.ButtonStyle.primary,
                                           custom_id="stream-export"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Stream")
        if cog is None:
            return await checks.ignore_click(interaction)
        await cog.export_click(interaction)


class ResetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"stream-reset:(?P<login>[a-z0-9-]{1,40})"):
    def __init__(self, login: str):
        self.login = login
        super().__init__(discord.ui.Button(label=f"Reset {login}", style=discord.ButtonStyle.danger,
                                           custom_id=f"stream-reset:{login}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["login"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Stream")
        if cog is None:
            return await checks.ignore_click(interaction)
        await cog.reset_click(interaction, self.login)


class Stream(commands.Cog):
    """Tournament streams. Managers only (casters may use !stream lineup)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_url = os.getenv("STREAM_CONTROL_URL", "").rstrip("/")
        self.token = os.getenv("STREAM_CONTROL_TOKEN", "")
        self.session: aiohttp.ClientSession | None = None
        self._states: dict[int, StreamState] = {}
        self._panel_cache: dict[int, str] = {}  # message id -> last description shown
        if not self.configured:
            log.warning("STREAM_CONTROL_URL / STREAM_CONTROL_TOKEN not set: !stream won't work")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        self.bot.add_dynamic_items(ExportButton, ResetButton)
        self.refresh_panels.start()

    async def cog_unload(self):
        self.refresh_panels.cancel()
        self.bot.remove_dynamic_items(ExportButton, ResetButton)
        if self.session is not None:
            await self.session.close()

    def _state(self, guild_id: int) -> StreamState:
        if guild_id not in self._states:
            self._states[guild_id] = StreamState(Path(self.bot.store.directory) / f"stream_{guild_id}.json")
        return self._states[guild_id]

    def _is_caster(self, member) -> bool:
        role_id = self._state(member.guild.id).caster_role_id if getattr(member, "guild", None) else None
        return role_id is not None and any(r.id == role_id for r in getattr(member, "roles", []))

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.command is not None and ctx.command.qualified_name == "stream lineup" and self._is_caster(ctx.author):
            return True
        return checks.require_manager(ctx)

    # -- control service ---------------------------------------------------------

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.configured:
            raise StreamError("The stream service isn't configured: set STREAM_CONTROL_URL and STREAM_CONTROL_TOKEN "
                              "in the bot's .env and restart it.")
        try:
            async with self.session.request(method, self.base_url + path, json=body,
                                            headers={"Authorization": f"Bearer {self.token}"}) as resp:
                if resp.status == 401:
                    raise StreamError("The stream service rejected the bot's token. Check STREAM_CONTROL_TOKEN.")
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    raise StreamError(f"The stream service gave an unexpected reply (HTTP {resp.status}).")
                if resp.status >= 400:
                    raise StreamError(f"The stream service refused: {(data or {}).get('error', resp.status)}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Couldn't reach the stream service (%s %s): %r", method, path, e)
            raise StreamError("Couldn't reach the stream service. Check that it's running and that "
                              "STREAM_CONTROL_URL is right.")

    # -- teams and paths -----------------------------------------------------------

    def _team_paths(self, guild_id: int) -> dict[str, str]:
        """team name -> teamNN for every registered team, giving new teams the next free path.
        Paths of teams that aren't registered anymore (removed, renamed, dummies) are freed first."""
        state = self._state(guild_id)
        teams = self.bot.store.get(guild_id).teams
        names = {team.name for team in teams}
        stale = [name for name in state.team_paths if name not in names]
        for name in stale:
            del state.team_paths[name]
        taken = set(state.team_paths.values())
        changed = bool(stale)
        for team in teams:
            if team.name in state.team_paths:
                continue
            free = next((f"team{n:02d}" for n in range(1, TEAM_COUNT + 1) if f"team{n:02d}" not in taken), None)
            if free is None:
                raise StreamError(f"There are only {TEAM_COUNT} team paths; can't give one to {team.name}.")
            state.team_paths[team.name] = free
            taken.add(free)
            changed = True
        if changed:
            state.save()
        return state.team_paths

    def _resolve_team(self, guild_id: int, value: str) -> str:
        """A team name (any case; quotes around it are optional) or team path -> team path. Names come
        first, so a team that happens to be called e.g. 'team03' is still found by its name."""
        paths = self._team_paths(guild_id)
        stripped = value.strip()
        unquoted = stripped[1:-1] if len(stripped) > 1 and stripped[0] in '"\u201c' and stripped[-1] in '"\u201d' \
            else stripped
        for candidate in dict.fromkeys((value, unquoted)):
            for name, path in paths.items():
                if _key(name) == _key(candidate):
                    return path
        lowered = value.strip().lower()
        if re.fullmatch(r"team\d{2}", lowered) and lowered in paths.values():
            return lowered
        raise StreamError(f"Unknown team **{_esc(value)}**.")

    def _name_of(self, guild_id: int, path: str | None) -> str:
        if path is None:
            return "-"
        for name, p in self._state(guild_id).team_paths.items():
            if p == path:
                return name
        return path

    def _roster(self, guild: discord.Guild) -> dict:
        t = self.bot.store.get(guild.id)
        paths = self._team_paths(guild.id)
        teams = [{"path": paths[team.name], "name": team.name,
                  "players": [{"discord_id": p.uid, "name": p.username} for p in team.players]} for team in t.teams]
        casters = []
        state = self._state(guild.id)
        role = guild.get_role(state.caster_role_id) if state.caster_role_id else None
        if role is not None:
            casters = [{"discord_id": str(m.id), "name": m.name} for m in role.members if not m.bot]
        return {"teams": teams, "casters": casters}

    # -- commands ------------------------------------------------------------------

    @commands.group(name="stream", invoke_without_command=True)
    async def stream(self, ctx: commands.Context):
        """Tournament streams."""
        if checks.is_manager(ctx.author):
            await ctx.send_help(ctx.command)

    @stream.command(name="casterrole", usage="@Role")
    async def casterrole(self, ctx: commands.Context, role: discord.Role):
        """Set which role counts as caster, and create/repair #stream-casters for it."""
        state = self._state(ctx.guild.id)
        state.caster_role_id = role.id
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                      embed_links=True),
        }
        channel = ctx.guild.get_channel(state.casters_channel_id) if state.casters_channel_id else None
        if isinstance(channel, discord.TextChannel):
            await channel.edit(overwrites=overwrites, reason="Stream casters channel")
        else:
            channel = await ctx.guild.create_text_channel(CASTERS_CHANNEL_NAME, overwrites=overwrites,
                                                          reason="Stream casters channel")
            state.casters_channel_id = channel.id
        state.save()
        members = [m for m in role.members if not m.bot]
        note = "" if members else (" Nobody with that role is visible to me yet: if casters do have it, turn on the "
                                   "bot's **Server Members Intent** in the Discord developer portal.")
        await ctx.send(f"Casters are now members with {role.mention} ({len(members)} right now). "
                       f"They can use `!stream lineup` in {channel.mention}.{note}")

    @stream.command(name="export")
    async def export(self, ctx: commands.Context):
        """Post a button that syncs the logins and shows the CSVs privately to the manager who clicks it."""
        view = discord.ui.View(timeout=None)
        view.add_item(ExportButton())
        await ctx.send("**Stream logins.** Clicking the button creates any missing logins, revokes those of players "
                       "who left, and shows the CSVs **only to you** (download them right away; private replies "
                       "disappear).", view=view)

    async def export_click(self, interaction: discord.Interaction):
        if interaction.guild is None or not checks.is_manager(interaction.user):
            return await checks.ignore_click(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self._call("POST", "/export", self._roster(interaction.guild))
        except StreamError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        players = sorted(data.get("players", []), key=lambda p: (p.get("team_path") or "", p["login"]))
        casters = data.get("casters", [])
        files = [_csv_file("stream-players.csv",
                           ["team", "team_path", "player", "discord_id", "login", "password", "browser_link",
                            "obs_srt_url", "streamed_successfully"],
                           [[_text_cell(p.get("team_name")), p.get("team_path"), _text_cell(p["name"]), p["discord_id"],
                             p["login"], p["password"], p.get("browser_url"), p.get("srt_url"),
                             "yes" if p.get("verified") else "no"]
                            for p in players])]
        if casters:
            files.append(_csv_file("stream-casters.csv",
                                   ["caster", "discord_id", "login", "password", "slot", "browser_link", "obs_srt_url"],
                                   [[_text_cell(c["name"]), c["discord_id"], c["login"], c["password"], f["slot"],
                                     f["browser_url"], f["srt_url"]] for c in casters for f in c.get("feeds", [])]))
        created, revoked = data.get("created", []), data.get("revoked", [])
        summary = (f"{len(players)} player login(s), {len(casters)} caster login(s). "
                   f"Created: {', '.join(created) or 'none'}. Revoked: {', '.join(revoked) or 'none'}.")
        if not casters:
            summary += "\nNo casters: set the caster role with `!stream casterrole @Role` first."
        await interaction.followup.send(summary, files=files, ephemeral=True)

    @stream.command(name="reset", usage="<@player or login>")
    async def reset(self, ctx: commands.Context, *, target: str):
        """Give a player or caster a new password; the new link is only shown to the manager who confirms."""
        target = target.strip()
        status = await self._call("GET", "/status")
        mention = re.fullmatch(r"<@!?(\d+)>", target)
        if mention:
            matches = [l for l in status.get("logins", []) if l["discord_id"] == mention.group(1)]
        else:
            matches = [l for l in status.get("logins", []) if l["login"] == target.lower()]
        if not matches:
            raise StreamError("No active login for that player or name. Run the export first?")
        view = discord.ui.View(timeout=None)
        for login in matches:
            view.add_item(ResetButton(login["login"]))
        names = ", ".join(f"**{l['login']}** ({_esc(l['name'])})" for l in matches)
        await ctx.send(f"Reset the password of {names}? The old one stops working immediately; the new link is "
                       "shown only to whoever clicks.", view=view)

    async def reset_click(self, interaction: discord.Interaction, login: str):
        if interaction.guild is None or not checks.is_manager(interaction.user):
            return await checks.ignore_click(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            entry = await self._call("POST", f"/logins/{login}/reset")
        except StreamError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        lines = [f"New password for **{entry['login']}** ({_esc(entry['name'])}): `{entry['password']}`"]
        if entry.get("kind") == "player":
            lines += [f"Browser: {entry.get('browser_url')}", f"OBS (SRT): `{entry.get('srt_url')}`"]
        else:
            lines.append("Their 16 feed links changed too: run the export again for the full list.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        try:
            await interaction.message.edit(content=f"Password of **{login}** was reset by {interaction.user.mention}.",
                                           view=None)
        except discord.HTTPException:
            pass

    @stream.command(name="overview")
    async def overview(self, ctx: commands.Context):
        """Every team: who has streamed successfully, who's live now, the slot mapping and the delay."""
        status = await self._call("GET", "/status")
        by_team: dict[str, list] = {}
        for login in status.get("logins", []):
            if login["kind"] == "player":
                by_team.setdefault(login["team_path"], []).append(login)
        names = {path: name for name, path in self._team_paths(ctx.guild.id).items()}
        embed = discord.Embed(title="Streams overview")
        if not status.get("mediamtx_ok"):
            embed.description = ":warning: The media server isn't answering; live status may be out of date."
        # A team needs at least one tested player (whoever streams its games), not every player
        teams_tested = teams_total = 0
        for team in status.get("teams", []):
            path = team["path"]
            players = by_team.get(path, [])
            if not players and path not in names:
                continue
            lines = []
            tested = any(p["verified"] for p in players)
            if players:
                teams_total += 1
                teams_tested += tested
            for p in players:
                mark = ":white_check_mark:" if p["verified"] else ":x:"
                live = " :red_circle: **live**" if team.get("live") and team.get("login") == p["login"] else ""
                lines.append(f"{mark} {_esc(p['name'])}{live}")
            if not players:
                lines.append("*no logins yet: run `!stream export`*")
            if team.get("twitch"):
                lines.append(f":tv: via {_twitch_text(team['twitch'])}")
            if problems := _settings_problems(team.get("settings")):
                lines.append(f":warning: settings: {', '.join(problems)}")
            label = _esc(names.get(path, team.get('name') or path))
            embed.add_field(name=f"{'' if tested else ':warning: '}{label} ({path})",
                            value="\n".join(lines)[:1024], inline=True)
        slots = status.get("slots", {})
        assigned = [f"`{slot}` {_esc(self._name_of(ctx.guild.id, team))}" for slot, team in slots.items() if team]
        # Discord caps an embed at 6000 characters in all: with long team names, the slots go in a second one
        fits = len(embed) + sum(len(line) + 1 for line in assigned) + 200 < 6000
        slots_embed = embed if fits else discord.Embed(title="Streams overview: slots")
        discord_text.add_fields(slots_embed, "Slots now", assigned, empty="none assigned")
        embed.set_footer(text=f"{teams_tested}/{teams_total} teams tested (one player each is enough) · "
                              f"delay {status.get('delay_minutes', 0):g} min")
        await ctx.send(embed=embed)
        if not fits:
            await ctx.send(embed=slots_embed)

    @stream.command(name="inspect", usage="<team>")
    async def inspect(self, ctx: commands.Context, *, team: str):
        """Details of a team's current stream: player, resolution, frame rate, bitrate, codecs, audio."""
        path = self._resolve_team(ctx.guild.id, team)
        info = await self._call("GET", f"/teams/{path}")
        embed = discord.Embed(title=f"{_esc(self._name_of(ctx.guild.id, path))} ({path})")
        if info.get("twitch"):
            embed.add_field(name="Twitch passthrough", inline=False,
                            value=f"The feed shows {_twitch_text(info['twitch'])}. "
                                  + ("The direct stream below is recorded." if info.get("live") else
                                     "Not recorded: nobody streams to the tournament directly."))
        if info.get("settings"):
            problems = _settings_problems(info["settings"])
            embed.add_field(name="Stream settings", inline=False,
                            value=":white_check_mark: all good" if not problems else
                            ":x: " + ", ".join(problems) + ". The player sees how to fix it on the go-live page "
                            "(Test your setup).")
        if not info.get("live"):
            embed.description = "Not streaming directly right now."
            return await ctx.send(embed=embed)
        seg = info.get("segment") or {}
        video = seg.get("video") or info.get("video") or {}
        audio = seg.get("audio") or info.get("audio") or {}
        embed.add_field(name="Player", value=f"{_esc(info.get('player_name') or info.get('login'))} "
                                             f"(since {_ts(info['since'], 'R')})", inline=False)
        res = f"{video.get('width')}x{video.get('height')}" if video.get("width") else "?"
        embed.add_field(name="Video", value=f"{video.get('codec', '?')} {res} {video.get('fps') or '?'} fps")
        embed.add_field(name="Bitrate", value=f"{info.get('bitrate_kbps') or seg.get('bitrate_kbps') or '?'} kbps")
        audio_text = f"{audio.get('codec')} {audio.get('sample_rate') or ''} Hz {audio.get('channels') or ''} ch" \
            if audio else ":warning: no audio"
        embed.add_field(name="Audio", value=audio_text)
        if (video.get("codec") or "").lower() not in ("h264", ""):
            embed.add_field(name=":warning: Unsupported video codec", inline=False,
                            value="The delayed feeds need H.264: ask the player to switch their encoder to H.264.")
        await ctx.send(embed=embed)

    @stream.command(name="lineup")
    async def lineup(self, ctx: commands.Context):
        """Which team is on each feed right now (feeds run behind) and what's coming next."""
        data = await self._call("GET", "/lineup")
        delay = data.get("delay_minutes", 0)
        by_pos: dict[int, list] = {}
        for slot in data.get("slots", []):
            m = SLOT_RE.match(slot["slot"])
            if m:
                by_pos.setdefault(int(m.group(1)), []).append(slot)
        lines = []
        for pos in sorted(by_pos):
            slots = by_pos[pos]
            if not any(s["team"] or s["upcoming"] for s in slots):
                continue
            if lines:
                lines.append("")
            lines.append(f"**Set position {pos}** (on your feeds now, {delay:g} min behind)")
            for s in slots:
                lines.append(f"`{s['slot']}` {_esc(self._name_of(ctx.guild.id, s['team']) if s['team'] else '(slate)')}")
            upcoming = {}
            for s in slots:
                for u in s["upcoming"]:
                    upcoming.setdefault(round(u["reaches_feed_at"]), {})[s["slot"]] = u["team"]
            for at, teams in sorted(upcoming.items()):
                pair = " vs ".join(_esc(self._name_of(ctx.guild.id, teams.get(f"s{pos}t{n}")))
                                   for n in (1, 2) if f"s{pos}t{n}" in teams)
                lines.append(f"Next on s{pos}: {pair}, reaches your feeds at {_ts(at)}")
        await discord_text.send_lines(ctx, lines or ["Nothing is assigned to any feed yet."])

    @stream.command(name="assign", usage="<slot> <team|none>")
    async def assign(self, ctx: commands.Context, slot: str, *, team: str):
        """Manually assign a team to a slot (e.g. s1t1 Team Falcon), or free it with 'none' or '-'."""
        slot = slot.lower()
        if not SLOT_RE.match(slot):
            raise StreamError(f"Slots are s1t1 .. s{SET_COUNT}t2.")
        try:
            path = self._resolve_team(ctx.guild.id, team)
        except StreamError:
            if team.strip().lower() not in ("none", "-"):
                raise
            path = None
        await self._call("PUT", "/slots", {slot: path})
        await ctx.send(f"`{slot}` is now {'free' if path is None else '**' + _esc(self._name_of(ctx.guild.id, path)) + '**'}."
                       " It reaches the casters' feeds one delay later.")

    @stream.command(name="kick", usage="<team>")
    async def kick(self, ctx: commands.Context, *, team: str):
        """Disconnect whoever is publishing on a team's path (e.g. a teammate who forgot to stop)."""
        path = self._resolve_team(ctx.guild.id, team)
        data = await self._call("POST", f"/teams/{path}/kick")
        await ctx.send(f"Disconnected **{data.get('login')}** from {_esc(self._name_of(ctx.guild.id, path))}. "
                       "If their software reconnects automatically, they need to stop it themselves.")

    @stream.command(name="twitch", usage="<team> <channel|off>")
    async def twitch(self, ctx: commands.Context, *, args: str):
        """Twitch passthrough: the team's feed shows their own Twitch stream (which must be delayed by exactly
        the tournament delay) instead of their recording; 'off' to stop. Players can also do this on the page."""
        team, _, channel = args.strip().rpartition(" ")
        if not team or not channel:
            raise StreamError("Usage: `!stream twitch <team> <channel | off>`, e.g. `!stream twitch Team Falcon falconplays`.")
        path = self._resolve_team(ctx.guild.id, team)
        off = channel.lower() in ("off", "none", "-")
        data = await self._call("PUT", f"/teams/{path}/twitch", {"channel": None if off else channel})
        name = _esc(self._name_of(ctx.guild.id, path))
        if data.get("channel"):
            await ctx.send(f"**{name}**'s feed now shows **twitch.tv/{data['channel']}** whenever they're on. Their "
                           "Twitch stream must be delayed by exactly the tournament delay (`!stream delay`), and it "
                           "isn't recorded: for the archive they should also stream directly (page or OBS).")
        else:
            await ctx.send(f"**{name}**'s feed shows their own stream again.")

    @stream.command(name="delay", usage="[minutes] [confirm]")
    async def delay(self, ctx: commands.Context, minutes: float = None, confirm: str = None):
        """Show the delay; with minutes, preview a change; with 'confirm', apply it to every feed."""
        if minutes is None:
            data = await self._call("GET", "/delay")
            since = f" (since {_ts(data['changed_at'], 'f')})" if data.get("changed_at") else ""
            return await ctx.send(f"The feeds run **{data['minutes']:g} minutes** behind{since}.")
        if not MIN_DELAY <= minutes <= MAX_DELAY:
            raise StreamError(f"The delay must be between {MIN_DELAY} and {MAX_DELAY} minutes.")
        if confirm is not None and confirm.lower() != "confirm":
            raise StreamError(f"Add `confirm` to apply it: `!stream delay {minutes:g} confirm`.")
        if confirm is None:
            effect = await self._call("POST", "/delay/preview", {"minutes": minutes})
            await ctx.send(self._effect_text(ctx.guild.id, effect) +
                           f"\nApply it with `!stream delay {minutes:g} confirm` (best before the event or in a break).")
            return
        data = await self._call("PUT", "/delay", {"minutes": minutes})
        log.info("%s set the stream delay to %g minutes", ctx.author, minutes)
        await ctx.send(f"Delay set to **{minutes:g} minutes**. " + self._effect_text(ctx.guild.id, data["effect"]))

    def _effect_text(self, guild_id: int, effect: dict) -> str:
        kind = effect.get("effect")
        if kind == "none":
            return "That's the current delay; nothing changes."
        if kind == "hold":
            return f"The feeds will hold on the slate for {effect['hold_minutes']:g} min, then carry on where they were."
        teams = sorted({self._name_of(guild_id, s["team"]) for s in effect.get("skipped", [])})
        what = f", which contains {', '.join(_esc(t) for t in teams)} playing" if teams else ""
        return f"The feeds will skip {_ts(effect['skip_from'])}–{_ts(effect['skip_to'])}{what}."

    @stream.command(name="archive")
    async def archive(self, ctx: commands.Context):
        """Archive health: copy to node 1, SSD ring buffer, lost footage, last stitched match, HDD space."""
        data = await self._call("GET", "/archive")
        gb = lambda b: f"{b / 1e9:.1f} GB" if isinstance(b, (int, float)) else "?"
        local = data.get("local", {})
        embed = discord.Embed(title="Stream archive")
        embed.add_field(name="SSD (stream server)", value=f"{gb(local.get('free_bytes'))} free of {gb(local.get('total_bytes'))}")
        if not data.get("enabled"):
            embed.add_field(name="Copy to node 1", value=":warning: not configured (ARCHIVE_TARGET is empty)", inline=False)
        else:
            lag = data.get("copy_lag_seconds")
            copy = f"{lag}s behind" if lag is not None else "no successful copy yet"
            if data.get("last_error"):
                copy += f"\n:warning: last error: {data['last_error'][:300]}"
            embed.add_field(name="Copy to node 1", value=copy, inline=False)
            remote = data.get("remote", {})
            embed.add_field(name="HDD (node 1)", value=f"{gb(remote.get('free_bytes'))} free of {gb(remote.get('total_bytes'))}")
            last = remote.get("last_stitched")
            embed.add_field(name="Last stitched", value=f"{last['match_id']} ({_ts(last['at'], 'R')})" if last else "none yet")
            if remote.get("stitch_errors"):
                embed.add_field(name="Stitch problems", inline=False, value="\n".join(
                    f"{e.get('match_id')} {_esc(self._name_of(ctx.guild.id, e.get('team')))}: {e.get('error')}"
                    for e in remote["stitch_errors"][-5:])[:1024])
        embed.add_field(name="Footage lost", value=str(data.get("lost_segments", 0)) + " segment(s)")
        await ctx.send(embed=embed)

    @stream.command(name="vods", usage="<team | set ID>")
    async def vods(self, ctx: commands.Context, *, target: str):
        """Archived files for a team or a set (e.g. !stream vods Team Falcon, !stream vods 12)."""
        try:
            query = f"?team={self._resolve_team(ctx.guild.id, target)}"
        except StreamError:
            set_id = re.fullmatch(r"#?(?:set)?\s*(\d+)", target.strip().lower())
            if not set_id:
                raise
            query = f"?match=set{set_id.group(1)}-"
        files = (await self._call("GET", "/vods" + query)).get("vods", [])
        if not files:
            return await ctx.send("No archived files for that yet.")
        lines = [f"`{f['match_id']}` {_esc(self._name_of(ctx.guild.id, f['team']))}: `{f['path']}` "
                 f"({f.get('size_bytes', 0) / 1e6:.0f} MB{', has gaps' if f.get('gaps') else ''})" for f in files]
        await discord_text.send_lines(ctx, lines)

    # -- automatic behaviour (signals from the other cogs) -----------------------------

    async def _assign_set(self, guild: discord.Guild, set_id: int, free: bool = False) -> None:
        found = _find_set(self.bot.store.get(guild.id), set_id)
        if found is None:
            return
        _, _, pos, s = found
        if pos > SET_COUNT:
            log.warning("Set #%s is position %s in its round; only %s slots exist", set_id, pos, SET_COUNT)
            return
        paths = self._team_paths(guild.id)
        mapping = {f"s{pos}t1": paths.get(s.team1.name), f"s{pos}t2": paths.get(s.team2.name)}
        if free:
            current = await self._call("GET", "/slots")
            mapping = {slot: None for slot, team in mapping.items() if current.get(slot) == team}
            if not mapping:
                return
        await self._call("PUT", "/slots", mapping)

    @commands.Cog.listener()
    async def on_set_started(self, guild: discord.Guild, set_id: int):
        if not self.configured:
            return
        state = self._state(guild.id)
        state.set_starts[str(set_id)] = time.time()
        state.save()
        try:
            await self._assign_set(guild, set_id)
        except StreamError as e:
            log.warning("Couldn't assign slots for set #%s: %s", set_id, e)
        found = _find_set(self.bot.store.get(guild.id), set_id)
        channel = guild.get_channel(found[3].channel_id) if found and found[3].channel_id else None
        if channel is not None:
            try:
                msg = await channel.send(embed=discord.Embed(title="Stream status", description="Checking streams..."))
                state.panels[str(set_id)] = msg.id
                state.save()
            except discord.HTTPException as e:
                log.warning("Couldn't post the stream panel for set #%s: %r", set_id, e)

    @commands.Cog.listener()
    async def on_bala_started(self, guild: discord.Guild, started_at: float):
        if not self.configured:
            return
        t = self.bot.store.get(guild.id)
        rnd = t.get_current_round()
        if rnd is None:
            return
        state = self._state(guild.id)
        for s in rnd.matchups:
            if s.channel_id is None or s.get_winner() is not None:
                continue
            current = reports.current_match(s)
            if current is not None:
                state.match_starts[f"{s.set_id}:{current[0]}"] = started_at
        state.save()

    @commands.Cog.listener()
    async def on_match_finished(self, guild: discord.Guild, set_id: int, match_no: int):
        if not self.configured:
            return
        state = self._state(guild.id)
        start = state.match_starts.pop(f"{set_id}:{match_no}", None)
        state.save()
        if start is None:
            return  # no !bala start for this match (manual starts): covered by the whole-set archive
        found = _find_set(self.bot.store.get(guild.id), set_id)
        if found is None:
            return
        si, ri, _, s = found
        paths = self._team_paths(guild.id)
        try:
            await self._call("POST", "/matches", {
                "match_id": f"set{set_id}-m{match_no}", "set_id": set_id, "match_no": match_no,
                "stage": si + 1, "round": ri + 1, "teams": [paths[s.team1.name], paths[s.team2.name]],
                "start": start - ARCHIVE_PAD_SECONDS, "end": time.time() + ARCHIVE_PAD_SECONDS})
            state.archived_matches.setdefault(str(set_id), []).append(match_no)
            state.save()
        except StreamError as e:
            log.warning("Couldn't send the manifest for set #%s match %s: %s", set_id, match_no, e)

    @commands.Cog.listener()
    async def on_set_decided(self, guild: discord.Guild, set_id: int):
        if not self.configured:
            return
        state = self._state(guild.id)
        try:
            await self._assign_set(guild, set_id, free=True)
        except StreamError as e:
            log.warning("Couldn't free the slots of set #%s: %s", set_id, e)
        found = _find_set(self.bot.store.get(guild.id), set_id)
        start = state.set_starts.get(str(set_id))
        if found and start and not state.archived_matches.get(str(set_id)):
            si, ri, _, s = found
            paths = self._team_paths(guild.id)
            try:  # no match had a start time: archive the whole set instead
                await self._call("POST", "/matches", {
                    "match_id": f"set{set_id}-all", "set_id": set_id, "stage": si + 1, "round": ri + 1,
                    "teams": [paths[s.team1.name], paths[s.team2.name]],
                    "start": start - ARCHIVE_PAD_SECONDS, "end": time.time() + ARCHIVE_PAD_SECONDS})
            except StreamError as e:
                log.warning("Couldn't send the set manifest for set #%s: %s", set_id, e)
        message_id = state.panels.pop(str(set_id), None)
        state.save()
        if found and message_id and found[3].channel_id:
            channel = guild.get_channel(found[3].channel_id)
            if channel is not None:
                try:
                    await channel.get_partial_message(message_id).edit(
                        embed=discord.Embed(title="Stream status", description="Set finished: streams aren't tracked anymore."))
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_set_reopened(self, guild: discord.Guild, set_id: int):
        if not self.configured:
            return
        await self.on_set_started(guild, set_id)

    @commands.Cog.listener()
    async def on_tournament_reset(self, guild: discord.Guild):
        state = self._state(guild.id)
        for saved in (state.team_paths, state.panels, state.set_starts, state.match_starts, state.archived_matches):
            saved.clear()
        state.save()
        if not self.configured:
            return
        try:  # the paths go to new teams, so nothing of the old teams may stick to them
            await self._call("PUT", "/slots", {f"s{pos}t{n}": None for pos in range(1, SET_COUNT + 1) for n in (1, 2)})
            for n in range(1, TEAM_COUNT + 1):
                await self._call("PUT", f"/teams/team{n:02d}/twitch", {"channel": None})
        except StreamError as e:
            log.warning("Couldn't clear the stream service after the reset: %s", e)

    # -- status panels -----------------------------------------------------------------

    def _panel_text(self, guild_id: int, s, status: dict | None) -> str:
        if status is None:
            return "Stream status is unavailable right now."
        teams = {t["path"]: t for t in status.get("teams", [])}
        paths = self._state(guild_id).team_paths
        now = time.time()
        lines = []
        for team in (s.team1, s.team2):
            info = teams.get(paths.get(team.name) or "", {})
            if info.get("live"):
                line = f":red_circle: **{_esc(team.name)}**: streaming ({_esc(info.get('player_name') or info.get('login'))}, " \
                       f"since {_ts(info['since'], 'R')})"
            else:
                line = f":black_circle: **{_esc(team.name)}**: not streaming" + (" directly" if info.get("twitch") else "")
            if info.get("live") and (problems := _settings_problems(info.get("settings"))):
                line += f"\n:warning: Stream settings need fixing: {', '.join(problems)} (see the go-live page)."
            if info.get("twitch"):
                line += f"\n:tv: Casters see their Twitch stream, {_twitch_text(info['twitch'])}."
                if not info.get("live"):
                    line += " It isn't recorded: also streaming directly (page or OBS) puts it in the archive."
            refused = info.get("last_refused")
            if refused and now - refused["at"] < REFUSAL_NOTICE_SECONDS and info.get("live") and \
                    refused.get("login") != info.get("login"):
                line += (f"\n:warning: {_esc(refused.get('name') or refused['login'])} tried to start, but "
                         f"{_esc(info.get('player_name') or info.get('login'))} is still streaming: they must stop first.")
            lines.append(line)
        if not status.get("mediamtx_ok"):
            lines.append(":warning: The media server isn't answering; this may be out of date.")
        return "\n".join(lines)

    @tasks.loop(seconds=PANEL_SECONDS)
    async def refresh_panels(self):
        if self.configured:
            await self.update_panels(self.bot.guilds)

    async def update_panels(self, guilds) -> None:
        """Bring every open panel of these servers up to date (one status request for all of them)."""
        guilds = [(g, self._state(g.id)) for g in guilds]
        guilds = [(g, st) for g, st in guilds if st.panels]
        if not guilds:
            return
        try:
            status = await self._call("GET", "/status")
        except StreamError:
            status = None
        for guild, state in guilds:
            t = self.bot.store.get(guild.id)
            for set_id, message_id in list(state.panels.items()):
                found = _find_set(t, int(set_id))
                if found is None or found[3].channel_id is None:
                    state.panels.pop(set_id, None)
                    state.save()
                    continue
                text = self._panel_text(guild.id, found[3], status)
                if self._panel_cache.get(message_id) == text:
                    continue
                channel = guild.get_channel(found[3].channel_id)
                if channel is None:
                    continue
                try:
                    await channel.get_partial_message(message_id).edit(
                        embed=discord.Embed(title="Stream status", description=text))
                    self._panel_cache[message_id] = text
                except discord.NotFound:  # someone deleted the panel
                    state.panels.pop(set_id, None)
                    state.save()
                except discord.HTTPException as e:
                    log.warning("Couldn't update the stream panel of set #%s: %r", set_id, e)

    @refresh_panels.before_loop
    async def _before_panels(self):
        await self.bot.wait_until_ready()

    # -- errors --------------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if checks.is_silent(error):
            return
        error = getattr(error, "original", error)
        if isinstance(error, StreamError):
            await ctx.send(str(error))
        elif isinstance(error, discord.Forbidden):
            await ctx.send("I'm missing a Discord permission (Manage Channels and Manage Roles for the casters channel).")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(f"I couldn't read that. Usage: `!{ctx.command.qualified_name} {ctx.command.signature}`")
        else:
            log.error("Unexpected error in %s", ctx.command, exc_info=error)
            await ctx.send("Something went wrong. Check the bot's log.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Stream(bot))
