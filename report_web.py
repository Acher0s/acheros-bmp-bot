"""The report web page: https://conjoined.balala.pro/r/<token>, one per match.

Both teams open the same page. It shows a checklist per team (log uploaded /
right deck & stake / matches the other team's log) and refreshes itself, so each
team sees the other's upload land. No Discord imports: after an upload is stored,
the `on_upload` callback (from the report cog) posts in Discord and concludes the
match when it can.

Runs inside the bot process on 127.0.0.1, behind the reverse proxy.

Upload safety, in order, before anything is kept:
  * unknown or expired link, or a team that may not upload now      -> rejected
  * no Content-Length, or one above the limit                         -> rejected before reading
  * another upload for the same team still in progress               -> rejected
  * not multipart, not exactly one part called "file", not *.log     -> rejected
  * the body grows past the limit while streaming                     -> rejected, reading stops
  * binary data, not a Lovely log, no Multiplayer games in it         -> rejected
  * the exact file the other team uploaded                            -> rejected
Accepted files get a name chosen here (the uploader's filename is never used)
and are kept forever under data/logs/<server id>/.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import BodyPartReader, web

import reports
from persistence import StorageError, TournamentStore

log = logging.getLogger(__name__)

MULTIPART_OVERHEAD = 16 * 1024  # boundaries and part headers around the file itself
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")

OnUpload = Callable[[int, int, int, int], Awaitable[None]]  # guild id, set id, match number, slot

_SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",  # keeps the secret link out of Referer headers
    "Cache-Control": "no-store",
}


def _game_json(g) -> dict | None:
    if g is None:
        return None
    return {
        "player": g.player, "opponent": g.opponent, "seed": g.seed, "result": g.result,
        "deck": g.deck, "stake": g.stake_name, "rerolls": g.rerolls, "money_spent": g.money_spent,
        "highest_score": g.highest_score, "highest_hand": g.highest_hand, "started_at": g.started_at,
        "poly_hack_cards": g.poly_hack_cards,
    }


class ReportWeb:
    def __init__(self, store: TournamentStore, logs_dir: Path, on_upload: OnUpload):
        self.store = store
        self.logs_dir = Path(logs_dir)
        self.on_upload = on_upload
        self._busy: set[tuple[str, int]] = set()
        self._runner: web.AppRunner | None = None

        self.app = web.Application(client_max_size=reports.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD,
                                   middlewares=[self._headers])
        self.app.router.add_get("/r/{token}", self.page)
        self.app.router.add_get("/r/{token}/status", self.status)
        self.app.router.add_post("/r/{token}/upload/{slot}", self.upload)
        self.app.router.add_get("/static/report.js", self.script)

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)  # URLs contain secret tokens
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        log.info("Report page listening on http://%s:%s", host, port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- helpers --------------------------------------------------------------

    @web.middleware
    async def _headers(self, request: web.Request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = e
        response.headers.update(_SECURITY_HEADERS)
        return response

    def _find(self, token: str):
        """(guild id, tournament, set, match number, match) for an open report link, or None."""
        if not _TOKEN.match(token):
            return None
        for guild_id in self.store.guild_ids():
            try:
                t = self.store.get(guild_id)
            except StorageError:
                continue
            found = reports.find_by_token(t, token)
            if found is not None:
                s, n, m = found
                if reports.page_is_open(m.report, time.time()):
                    return guild_id, t, s, n, m
                return None
        return None

    @staticmethod
    def _error(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    def _status_json(self, t, s, n, m) -> dict:
        now = time.time()
        st = reports.check(m, reports.used_seeds(t, m))
        teams = []
        for slot, team in zip(reports.SLOTS, (s.team1, s.team2)):
            upload = m.report.uploads.get(slot)
            if st.pair is not None:
                game = st.pair[slot - 1]
            else:
                game = upload.games[-1] if upload and upload.games else None
            blocked = reports.why_no_upload(t, m, slot, now)
            teams.append({
                "slot": slot,
                "name": team.name,
                "uploaded": st.uploaded[slot],
                "has_game": st.has_game[slot],
                "paired": st.paired,
                "games_in_log": upload.total_games if upload else 0,
                "matching_games": len(upload.games) if upload else 0,
                "uploads_left": reports.MAX_UPLOADS_PER_TEAM - m.report.upload_counts.get(slot, 0),
                "can_upload": blocked is None,
                "blocked_reason": blocked,
                "game": _game_json(game),
            })
        winner = None
        if m.result is not None:
            winner = {"TEAM1_WIN": 1, "TEAM2_WIN": 2}.get(m.result.name)
        return {
            "set_id": s.set_id,
            "match_no": n,
            "best_of": s.best_of,
            "set_score": list(s.get_standings()),
            "deck": m.deck.name,
            "stake": m.stake.name,
            "teams": teams,
            "agreed_winner": st.agreed_winner,
            "log_winner": st.log_winner,
            "conflict": st.conflict,
            "concluded": winner is not None,
            "winner": winner,
            "expires_at": m.report.expires_at,
            "max_bytes": reports.MAX_UPLOAD_BYTES,
        }

    # -- routes ---------------------------------------------------------------

    async def page(self, request: web.Request) -> web.Response:
        if self._find(request.match_info["token"]) is None:
            return web.Response(text=NOT_FOUND_HTML, content_type="text/html", status=404)
        return web.Response(text=PAGE_HTML, content_type="text/html")

    async def script(self, request: web.Request) -> web.Response:
        return web.Response(text=PAGE_JS, content_type="application/javascript")

    async def status(self, request: web.Request) -> web.Response:
        found = self._find(request.match_info["token"])
        if found is None:
            return self._error(404, "This report link doesn't exist or has expired.")
        _, t, s, n, m = found
        return web.json_response(self._status_json(t, s, n, m))

    async def upload(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        found = self._find(token)
        if found is None:
            return self._error(404, "This report link doesn't exist or has expired.")
        guild_id, t, s, n, m = found
        try:
            slot = int(request.match_info["slot"])
        except ValueError:
            return self._error(404, "Unknown team.")

        # Everything that doesn't need the body is checked before reading any of it.
        reason = reports.why_no_upload(t, m, slot, time.time())
        if reason is not None:
            return self._error(409, reason)
        if request.content_length is None:
            return self._error(411, "The upload size is missing.")
        if request.content_length > reports.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
            return self._error(413, f"That file is too big (the limit is {reports.MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        if request.content_type != "multipart/form-data":
            return self._error(400, "Upload the log file with the form on the page.")
        key = (token, slot)
        if key in self._busy:
            return self._error(409, "Another upload for this team is still in progress.")

        self._busy.add(key)
        try:
            data, error = await self._read_single_log(request)
            if error is not None:
                return self._error(400 if not error.startswith("That file is too big") else 413, error)
            try:
                games = reports.parse_upload(data.decode("utf-8", errors="replace"))
            except reports.ReportError as e:
                return self._error(400, str(e))
            sha256 = hashlib.sha256(data).hexdigest()

            # Look everything up again: the tournament may have been reloaded while reading.
            found = self._find(token)
            if found is None:
                return self._error(404, "This report link doesn't exist or has expired.")
            guild_id, t, s, n, m = found
            other = m.report.uploads.get(3 - slot)
            if other is not None and other.sha256 == sha256:
                return self._error(400, "That's the exact file the other team uploaded. Each team uploads its own log.")
            reason = reports.why_no_upload(t, m, slot, time.time())
            if reason is not None:
                return self._error(409, reason)

            rel = self._save_file(guild_id, s.set_id, n, slot, sha256, data)
            # Change -> save with no `await` in between.
            reports.record_upload(t, m, slot, rel, sha256, games, time.time())
            self.store.save(guild_id)
            set_id = s.set_id
        finally:
            self._busy.discard(key)

        try:
            await self.on_upload(guild_id, set_id, n, slot)
        except Exception:
            log.exception("Posting about the upload for set #%s match %s failed", set_id, n)

        found = self._find(token)
        if found is None:  # e.g. the match concluded and the page closed right away
            return web.json_response({"ok": True})
        _, t, s, n, m = found
        return web.json_response(self._status_json(t, s, n, m))

    async def _read_single_log(self, request: web.Request) -> tuple[bytes | None, str | None]:
        """(file bytes, None) for a form with exactly one .log file in it, else (None, reason)."""
        try:
            reader = await request.multipart()
            part = await reader.next()
            if not isinstance(part, BodyPartReader) or part.name != "file" or not part.filename:
                return None, "Upload exactly one file."
            if not part.filename.lower().endswith(".log"):
                return None, "Only .log files can be uploaded."
            data = bytearray()
            while chunk := await part.read_chunk(64 * 1024):
                data += chunk
                if len(data) > reports.MAX_UPLOAD_BYTES:
                    return None, f"That file is too big (the limit is {reports.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."
            if await reader.next() is not None:
                return None, "Upload exactly one file."
        except (ValueError, AssertionError) as e:  # malformed multipart
            log.info("Rejected a malformed upload: %r", e)
            return None, "The upload was malformed."
        if not data:
            return None, "That file is empty."
        if b"\x00" in data:
            return None, "That isn't a text log file."
        return bytes(data), None

    def _save_file(self, guild_id: int, set_id: int, match_no: int, slot: int, sha256: str, data: bytes) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rel = Path(str(guild_id)) / f"set{set_id}-match{match_no}-team{slot}-{stamp}-{sha256[:12]}.log"
        path = self.logs_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # same second + same content: it's already saved
            with open(path, "xb") as f:
                f.write(data)
        return rel.as_posix()


NOT_FOUND_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link expired</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222;background:#fafafa}
@media (prefers-color-scheme:dark){body{color:#e6e6e6;background:#16181c}}</style></head>
<body><h1>This report link doesn't work anymore</h1>
<p>It has expired or never existed. To get a new link, click your team's pick again on the widget in your
set's Discord channel.</p></body></html>
"""

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Match report</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--text:#1d1f23;--muted:#62666d;--line:#dcdcd8;--ok:#1a7f37;--bad:#c62828;--wait:#8a6d00;--accent:#3056d3}
@media (prefers-color-scheme:dark){:root{--bg:#15171b;--card:#1e2126;--text:#e7e8ea;--muted:#9aa0a8;--line:#33373e;--ok:#4cc26b;--bad:#ff6b6b;--wait:#e0b84a;--accent:#7d9bff}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
main{max-width:60rem;margin:0 auto;padding:1.5rem 1rem 3rem}
h1{font-size:1.5rem;margin:0 0 .25rem}
.sub{color:var(--muted);margin:0 0 1.25rem}
.banner{border:1px solid var(--line);border-left:4px solid var(--accent);background:var(--card);padding:.75rem 1rem;border-radius:6px;margin-bottom:1.25rem}
.banner.ok{border-left-color:var(--ok)}.banner.bad{border-left-color:var(--bad)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media (max-width:40rem){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:1rem}
.card h2{font-size:1.15rem;margin:0 0 .75rem;overflow-wrap:anywhere}
ul.checks{list-style:none;padding:0;margin:0 0 1rem}
ul.checks li{display:flex;gap:.5rem;align-items:baseline;padding:.2rem 0}
.mark{font-weight:700;width:1.2em;flex:none;text-align:center}
.mark.ok{color:var(--ok)}.mark.bad{color:var(--bad)}.mark.wait{color:var(--muted)}
.upload{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:.5rem}
.upload input{max-width:100%}
button{font:inherit;padding:.4rem .9rem;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.msg{min-height:1.5em;font-size:.95rem}.msg.bad{color:var(--bad)}.msg.ok{color:var(--ok)}
.note{color:var(--muted);font-size:.9rem;margin:.25rem 0}
dl{display:grid;grid-template-columns:auto 1fr;gap:.15rem .75rem;margin:.75rem 0 0;font-size:.95rem}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
</style></head>
<body><main>
<h1 id="title">Match report</h1>
<p class="sub" id="sub">Loading...</p>
<div class="banner" id="banner" hidden></div>
<div class="grid" id="teams"></div>
<p class="note">Your Lovely log is in your Balatro folder under <code>Mods/lovely/log</code>. Upload the one from the session you played this match in.</p>
</main><script src="/static/report.js"></script></body></html>
"""

PAGE_JS = r"""
"use strict";
const base = location.pathname.replace(/\/+$/, "");
let state = null, busy = false, lastText = "";

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k);
  return e;
}

function check(ok, label, pending) {
  const mark = ok ? el("span", {class: "mark ok", text: "✓"})
             : pending ? el("span", {class: "mark wait", text: "–"})
             : el("span", {class: "mark bad", text: "✗"});
  return el("li", {}, mark, el("span", {text: label}));
}

function teamName(slot) { return state.teams[slot - 1].name; }

function banner() {
  const b = document.getElementById("banner");
  b.hidden = false; b.className = "banner";
  if (state.concluded) {
    b.classList.add("ok");
    b.textContent = "Result recorded: " + teamName(state.winner) + " won this match.";
  } else if (state.conflict) {
    b.classList.add("bad");
    b.textContent = "The logs show " + teamName(state.log_winner) + " won, but both teams picked " + teamName(state.agreed_winner) +
      " in Discord. Change your pick with the buttons in Discord, or ask an admin.";
  } else {
    const todo = [];
    for (const t of state.teams) if (!t.uploaded) todo.push(t.name + " to upload their log");
    if (state.agreed_winner == null) todo.push("both teams to pick the same winner in Discord");
    if (!todo.length && !state.teams[0].paired) todo.push("two logs of the same game (see the checklists below)");
    b.textContent = todo.length ? "Waiting for " + todo.join(", ") + "." : "Checking...";
  }
}

function stats(g) {
  if (!g) return null;
  const rows = [["Players", g.player + " vs " + g.opponent], ["Result", g.result === "win" ? g.player + " won" : g.player + " lost"],
    ["Deck / stake", g.deck + " / " + g.stake], ["Seed", g.seed], ["Rerolls", g.rerolls], ["Money spent", "$" + g.money_spent],
    ["Highest score", g.highest_score ?? "no PvP blind played"]];
  const dl = el("dl");
  for (const [k, v] of rows) dl.append(el("dt", {text: k}), el("dd", {text: String(v)}));
  return dl;
}

function teamCard(t) {
  const other = state.teams[2 - t.slot];
  const combo = state.deck + " Deck / " + state.stake + " Stake";
  const checks = el("ul", {class: "checks"},
    check(t.uploaded, t.uploaded ? "Log uploaded" : "No log uploaded yet", !t.uploaded),
    check(t.has_game, t.uploaded ? (t.has_game ? "Has a finished " + combo + " game"
            : "No finished " + combo + " game in this log (" + t.games_in_log + " game(s) found)") : "Has a finished " + combo + " game", !t.uploaded),
    check(t.paired, t.paired ? "Same game as " + other.name + "'s log" :
            "Same game as " + other.name + "'s log (same seed, same two players)", !(t.uploaded && other.uploaded)));
  const card = el("section", {class: "card"}, el("h2", {text: t.name}), checks);

  if (t.can_upload) {
    const input = el("input", {type: "file", accept: ".log"});
    const button = el("button", {type: "button", text: t.uploaded ? "Replace log" : "Upload log"});
    const msg = el("div", {class: "msg"});
    button.addEventListener("click", () => upload(t.slot, input, button, msg));
    card.append(el("div", {class: "upload"}, input, button), msg,
      el("p", {class: "note", text: "Only .log files up to " + Math.floor(state.max_bytes / 1048576) + " MB. " + t.uploads_left + " upload(s) left."}));
  } else if (t.blocked_reason && !state.concluded) {
    card.append(el("p", {class: "note", text: t.blocked_reason}));
  }
  const s = stats(t.game);
  if (s) card.append(s);
  return card;
}

function render() {
  document.getElementById("title").textContent = "Set #" + state.set_id + " · Match " + state.match_no;
  document.getElementById("sub").textContent = state.teams[0].name + " vs " + state.teams[1].name + " · " +
    state.deck + " Deck · " + state.stake + " Stake · set score " + state.set_score.join("-") + " (Bo" + state.best_of + ")";
  banner();
  const grid = document.getElementById("teams");
  grid.replaceChildren(...state.teams.map(teamCard));
}

async function refresh() {
  if (busy) return;
  try {
    const r = await fetch(base + "/status", {cache: "no-store"});
    if (r.status === 404) { location.reload(); return; }
    if (r.ok) {
      const text = await r.text();
      if (text !== lastText) { lastText = text; state = JSON.parse(text); render(); }  // keeps a chosen file
    }
  } catch (e) { /* offline for a moment; try again next tick */ }
}

async function upload(slot, input, button, msg) {
  const file = input.files[0];
  msg.className = "msg bad";
  if (!file) { msg.textContent = "Choose your .log file first."; return; }
  if (!file.name.toLowerCase().endsWith(".log")) { msg.textContent = "Only .log files can be uploaded."; return; }
  if (file.size > state.max_bytes) { msg.textContent = "That file is too big."; return; }
  const form = new FormData();
  form.append("file", file);
  busy = true; button.disabled = true; msg.className = "msg"; msg.textContent = "Uploading...";
  try {
    const r = await fetch(base + "/upload/" + slot, {method: "POST", body: form});
    const body = await r.json().catch(() => ({}));
    busy = false;
    if (!r.ok) { button.disabled = false; msg.className = "msg bad"; msg.textContent = body.error || "Upload failed."; return; }
    lastText = "";
    if (body.teams) { state = body; render(); } else { refresh(); }
  } catch (e) {
    busy = false; button.disabled = false; msg.className = "msg bad"; msg.textContent = "Upload failed. Check your connection and try again.";
  }
}

refresh();
setInterval(refresh, 4000);
"""
