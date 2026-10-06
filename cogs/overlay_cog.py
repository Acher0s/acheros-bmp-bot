"""OBS browser sources: plain text in the m6x11 font (black, transparent background), one piece of
text per URL, to place into any box of a stream layout.

  /overlay                          list of every URL (open it in a browser to copy them)
  /overlay/slot/<slot>/team         s1t1 .. s8t2: the team on that slot's feed
  /overlay/slot/<slot>/p1 , /p2     its players (Discord display name)
  /overlay/slot/<slot>/standing     its record in the current stage, e.g. 2-1
  /overlay/round                    e.g. "Stage 1 · Round 2"
  /overlay/state.json               the data the pages poll every 2 s

The text is as large as fits the Browser Source's box (width and height, refitted when the source
is resized), centered both ways. Optional query options: size=N (largest size in px), align=left|
center|right, valign=top|middle|bottom, color=000000, key=... (required when OVERLAY_KEY is set).

The text follows the casters' delayed feeds (stream service lineup): the team on each slot and
standings as of feed time, so it never shows a result before viewers see it. Without the stream
service it shows the live situation. An empty slot shows no text.

Settings (.env): OVERLAY_BIND_HOST (127.0.0.1), OVERLAY_PORT (8081), OVERLAY_KEY (optional),
OVERLAY_PUBLIC_URL (optional, for the URL list), OVERLAY_GUILD_ID (if the bot is in several servers).
"""
import hmac
import logging
import os
import time
from pathlib import Path

from aiohttp import web
from discord.ext import commands

import overlays

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "overlay"
SLOTS = [f"s{s}t{t}" for s in range(1, 9) for t in (1, 2)]
CACHE_SECONDS = 1.5
FIELDS = ["team", "p1", "p2", "standing"]


class Overlay(commands.Cog):
    """OBS overlays (web pages, no commands)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.host = os.getenv("OVERLAY_BIND_HOST", "127.0.0.1")
        self.port = int(os.getenv("OVERLAY_PORT", "8081"))
        self.key = os.getenv("OVERLAY_KEY", "")
        self.public_url = os.getenv("OVERLAY_PUBLIC_URL", "").rstrip("/")
        self.guild_id = int(os.getenv("OVERLAY_GUILD_ID", "0") or 0)
        self.guild_override = None  # tests
        self._cache: tuple[float, dict] | None = None
        self._runner: web.AppRunner | None = None

    async def cog_load(self):
        app = web.Application()
        app.router.add_get("/overlay", self.index)
        app.router.add_get("/overlay/", self.index)
        app.router.add_get("/overlay/slot/{slot}/{field}", self.page)
        app.router.add_get("/overlay/round", self.page)
        app.router.add_get("/overlay/state.json", self.state_json)
        app.router.add_get("/overlay/m6x11.ttf", self.font)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("Overlays on http://%s:%s/overlay", self.host, self.port)

    async def cog_unload(self):
        if self._runner is not None:
            await self._runner.cleanup()

    def _authorized(self, request: web.Request) -> bool:
        return not self.key or hmac.compare_digest(request.query.get("key", "").encode(), self.key.encode())

    def _guild(self):
        if self.guild_override is not None:
            return self.guild_override
        if self.guild_id:
            return self.bot.get_guild(self.guild_id)
        guilds = self.bot.guilds
        if len(guilds) == 1:
            return guilds[0]
        for guild in guilds:  # several servers: the one with a tournament running
            if self.bot.store.get(guild.id).get_current_round() is not None:
                return guild
        return None

    # -- data ----------------------------------------------------------------------

    async def build(self) -> dict:
        guild = self._guild()
        if guild is None:
            return {"round": None, "slots": {slot: None for slot in SLOTS}, "source": "none"}
        t = self.bot.store.get(guild.id)
        stream = self.bot.get_cog("Stream")
        set_starts = stream._state(guild.id).set_starts if stream else {}
        now = time.time()
        at, delay, source = now, 0.0, "live"
        slot_teams = None
        if stream is not None and stream.configured:
            try:
                lineup = await stream._call("GET", "/lineup")
                at, delay, source = lineup["feed_time"], lineup["delay_minutes"], "feed"
                slot_teams = {s["slot"]: stream._name_of(guild.id, s["team"]) if s["team"] else None
                              for s in lineup["slots"]}
            except Exception as e:  # stream service down: fall back to the live situation
                log.debug("Overlay lineup unavailable: %r", e)
        if slot_teams is None:
            slot_teams = {slot: None for slot in SLOTS}
            rnd = t.get_current_round()
            for pos, s in enumerate(rnd.matchups if rnd else [], start=1):
                if pos <= 8 and s.channel_id is not None and s.get_winner() is None:
                    slot_teams[f"s{pos}t1"], slot_teams[f"s{pos}t2"] = s.team1.name, s.team2.name

        def display_name(player) -> str:
            if player.uid.isdigit():
                member = guild.get_member(int(player.uid))
                if member is not None:
                    return member.display_name
            return player.username

        state = overlays.build_state(t, at, slot_teams, set_starts, display_name)
        state.update(feed_time=at, delay_minutes=delay, source=source, generated_at=now)
        return state

    # -- routes --------------------------------------------------------------------

    async def state_json(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        if self._cache is None or time.time() - self._cache[0] > CACHE_SECONDS:
            try:
                self._cache = (time.time(), await self.build())
            except Exception:
                log.exception("Couldn't build the overlay state")
                if self._cache is None:
                    return web.json_response({"error": "unavailable"}, status=503)
        return web.json_response(self._cache[1], headers={"Cache-Control": "no-store"})

    async def page(self, request: web.Request) -> web.Response:
        slot, field = request.match_info.get("slot"), request.match_info.get("field")
        if slot is not None and (slot not in SLOTS or field not in FIELDS):
            raise web.HTTPNotFound()
        return web.FileResponse(ASSETS / "overlay.html", headers={"Cache-Control": "no-store"})

    async def font(self, request: web.Request) -> web.Response:
        return web.FileResponse(ASSETS / "m6x11.ttf", headers={"Cache-Control": "max-age=86400"})

    async def index(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        base = self.public_url or f"{request.headers.get('X-Forwarded-Proto', request.scheme)}://{request.host}"
        query = f"?key={self.key}" if self.key else ""
        rows = []
        for slot in SLOTS:
            links = [f'<a href="{base}/overlay/slot/{slot}/{f}{query}">{f}</a>' for f in FIELDS]
            rows.append(f"<tr><td>{slot}</td><td>{' · '.join(links)}</td></tr>")
        rows.append(f'<tr><td>round</td><td><a href="{base}/overlay/round{query}">round</a></td></tr>')
        html = ("<!doctype html><meta charset=utf-8><title>Overlays</title>"
                "<style>body{font-family:sans-serif;margin:2em;background:#222;color:#eee}a{color:#7cf}"
                "td{padding:.3em 1em}</style><h1>OBS browser sources</h1>"
                "<p>Add each as a Browser Source sized to its box: the text fills the box (as large as fits, centered), "
                "black on a transparent background. Add <code>?size=48</code> (largest size), "
                "<code>?align=left</code>, <code>?valign=top</code> or <code>?color=ffffff</code> to change it "
                "(join several with <code>&amp;</code>).</p>"
                f"<table>{''.join(rows)}</table>")
        return web.Response(text=html, content_type="text/html")


async def setup(bot: commands.Bot):
    await bot.add_cog(Overlay(bot))
