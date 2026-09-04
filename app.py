"""Fantasy Draft Party — backend.

Polls a live draft (ESPN or Sleeper), builds a hype packet for every new pick,
and pushes it to the display over Server-Sent Events.

    python app.py                     # uses config.json
    python app.py --source demo       # replay a finished draft
    python app.py --source demo --interval 1   # burst test
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from enrich.cache import HypeCache
from enrich.players import PlayerDB
from enrich.stats import StatsDB
from sources.base import DraftSource, Pick
from sources.demo import DemoSource
from sources.espn import EspnSource
from sources.sleeper import SleeperSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)-18s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("draft-party")

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"

# Populated by main() before uvicorn starts the app.
CLI_ARGS = argparse.Namespace(source=None, interval=None, replay=False, port=8000)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup(CLI_ARGS)
    try:
        yield
    finally:
        await shutdown()


app = FastAPI(title="Draft Party", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# Broadcast state, shared between the poller and every connected display.
subscribers: list[asyncio.Queue] = []
recent_picks: list[dict] = []
state: dict = {"source": None, "players": None, "stats": None, "hype": None,
               "status": "starting", "league": {}}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("No config.json found. Copy config.example.json and fill it in.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


async def build_source(config: dict, args: argparse.Namespace) -> DraftSource:
    kind = (args.source or config.get("source") or "sleeper").lower()

    if kind == "demo":
        demo_cfg = config.get("demo", {})
        interval = args.interval or demo_cfg.get("interval", 10.0)
        if demo_cfg.get("sleeper_draft_id"):
            return await DemoSource.from_sleeper(demo_cfg["sleeper_draft_id"], interval)
        espn_cfg = config.get("espn", {})
        return await DemoSource.from_espn(
            league_id=demo_cfg.get("espn_league_id", "899513"),
            season=demo_cfg.get("espn_season", 2025),
            interval=interval,
            espn_s2=espn_cfg.get("espn_s2") or None,
            swid=espn_cfg.get("swid") or None,
        )

    if kind == "sleeper":
        cfg = config.get("sleeper", {})
        if cfg.get("draft_id"):
            return SleeperSource(str(cfg["draft_id"]))
        if cfg.get("league_id"):
            return await SleeperSource.from_league(str(cfg["league_id"]))
        raise ValueError("config.sleeper needs a draft_id or league_id")

    if kind == "espn":
        cfg = config.get("espn", {})
        if not cfg.get("league_id"):
            raise ValueError("config.espn needs a league_id")
        return EspnSource(
            league_id=str(cfg["league_id"]),
            season=int(cfg.get("season", 2026)),
            espn_s2=cfg.get("espn_s2") or None,
            swid=cfg.get("swid") or None,
        )

    raise ValueError(f"Unknown source {kind!r} (use espn, sleeper, or demo)")


async def resolve_player(pick: Pick):
    players: PlayerDB = state["players"]
    if pick.platform == "espn" or pick.espn_position_id is not None:
        return players.get_by_espn(
            pick.player_key,
            pick.raw_name or "",
            pick.espn_position_id,
            pick.espn_pro_team_id,
        )
    player = players.get_by_sleeper_id(pick.player_key)
    if player is None:
        log.warning("Unknown Sleeper player %s (%s)", pick.player_key, pick.raw_name)
    return player


async def broadcast(event: dict) -> None:
    payload = json.dumps(event)
    for queue in list(subscribers):
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(payload)


async def build_pick_event(pick: Pick, client: httpx.AsyncClient) -> dict | None:
    player = await resolve_player(pick)
    if player is None:
        return None

    hype: HypeCache = state["hype"]
    packet = await hype.build(player, client)
    event = {
        "type": "pick",
        "overall": pick.overall,
        "round": pick.round,
        "round_pick": pick.round_pick,
        "label": pick.label,
        "team_name": pick.team_name,
        "player": packet,
    }

    videos = len(packet.get("videos") or [])
    log.info(
        "PICK %s  %-24s %-4s %-3s  %s  [%d clip%s]",
        pick.label, player.name, player.position, player.team,
        pick.team_name, videos, "" if videos == 1 else "s",
    )
    return event


async def handle_picks(picks: list[Pick], client: httpx.AsyncClient) -> None:
    """Resolve every pick in a poll batch concurrently — an autodraft burst can
    drop several picks in one 2s window, and each uncached lookup is a 1-2s
    network round trip. Building them one at a time would serialize those
    round trips and leave the display stalled well behind the live draft.
    Broadcasting stays in draft order since gather() preserves input order."""
    if not picks:
        return
    events = await asyncio.gather(*(build_pick_event(p, client) for p in picks))
    for event in events:
        if event is None:
            continue
        recent_picks.append(event)
        del recent_picks[:-12]
        await broadcast(event)


async def poller() -> None:
    """Watches the draft for the life of the process."""
    source: DraftSource = state["source"]
    failures = 0

    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
        follow_redirects=True,
    ) as client:
        while True:
            try:
                await handle_picks(await source.poll(), client)
                if failures:
                    log.info("Draft connection recovered")
                    failures = 0
                state["status"] = "watching"
                await asyncio.sleep(source.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                state["status"] = "reconnecting"
                # Never let a transient blip kill the poller mid-draft.
                backoff = min(30, source.poll_interval * (2 ** min(failures, 4)))
                log.warning("Poll failed (%s) — retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status() -> dict:
    source: DraftSource = state["source"]
    return {
        "status": state["status"],
        "platform": source.platform if source else None,
        "league": state["league"],
        "picks_seen": len(source._seen) if source else 0,
        "cached_packets": state["hype"].size if state["hype"] else 0,
        "recent": recent_picks[-6:],
    }


@app.get("/events")
async def events() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    subscribers.append(queue)
    log.info("Display connected (%d active)", len(subscribers))

    async def stream():
        try:
            hello = json.dumps({"type": "hello", "recent": recent_picks[-5:],
                                "status": state["status"]})
            yield f"data: {hello}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in subscribers:
                subscribers.remove(queue)
            log.info("Display disconnected (%d active)", len(subscribers))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def startup(args: argparse.Namespace) -> None:
    config = load_config()

    players = PlayerDB()
    stats = StatsDB()
    await asyncio.gather(players.load(), stats.load())

    hype = HypeCache(players, stats)
    hype.load()

    source = await build_source(config, args)
    metadata = {}
    if hasattr(source, "load_metadata"):
        with contextlib.suppress(Exception):
            metadata = await source.load_metadata() or {}

    replay = args.replay or config.get("replay_existing", False)
    if replay or source.platform == "demo":
        source.started = True
        log.info("Replaying existing picks")
    else:
        existing = await source.snapshot()
        log.info("Snapshot: %d existing picks marked as seen", existing)

    state.update(source=source, players=players, stats=stats, hype=hype,
                 status="watching", league=metadata)

    log.info("=" * 62)
    log.info("  Watching %s draft — %s", source.platform.upper(), metadata or "")
    log.info("  Display:  http://localhost:%d", args.port)
    log.info("  Cache:    %d prewarmed packets", hype.size)
    log.info("=" * 62)

    app.state.poller = asyncio.create_task(poller())


async def shutdown() -> None:
    task = getattr(app.state, "poller", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if state.get("hype"):
        state["hype"].save()
    if state.get("source"):
        await state["source"].close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fantasy Draft Party")
    parser.add_argument("--source", choices=["espn", "sleeper", "demo"])
    parser.add_argument("--interval", type=float, help="demo seconds between picks")
    parser.add_argument("--replay", action="store_true",
                        help="replay picks that already exist")
    parser.add_argument("--port", type=int, default=8000)
    global CLI_ARGS
    CLI_ARGS = parser.parse_args()

    # Checked here (before uvicorn ever starts) rather than left to
    # load_config() alone: sys.exit() from inside the ASGI lifespan still
    # prints a full traceback on the way out, which reads like a crash to
    # someone setting this up for the first time.
    if not CONFIG_FILE.exists():
        log.error("No config.json found. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)

    uvicorn.run(app, host="0.0.0.0", port=CLI_ARGS.port, log_level="warning")


if __name__ == "__main__":
    main()
