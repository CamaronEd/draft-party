"""Finds highlight clips by scraping YouTube search results.

No API key. The official Data API charges 100 quota units per search against a
10,000/day cap, which a 200-pick draft plus a 300-player prewarm would blow
through several times over. Scraping the results page has no such ceiling.

Returns three ranked candidates per player because NFL-owned highlight footage
is frequently embed-restricted; the display walks the list on player error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from urllib.parse import quote_plus

import httpx

from .players import Player

log = logging.getLogger(__name__)

# sp=EgIQAQ%3D%3D filters results to videos only (no channels, playlists, Shorts).
SEARCH_URL = "https://www.youtube.com/results?search_query={q}&sp=EgIQAQ%253D%253D"
_INITIAL_DATA = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});</script>", re.DOTALL)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Channels that consistently post real game footage.
_OFFICIAL = {"nfl", "nfl films", "espn", "bleacher report", "ncaa", "espn college football"}

_GOOD_WORDS = {
    "highlights": 30, "season highlights": 25, "career": 10,
    "every touchdown": 20, "touchdowns": 12, "best plays": 15, "top plays": 15,
}
_BAD_WORDS = {
    "reaction": -60, "podcast": -80, "interview": -50, "press conference": -80,
    "fantasy": -35, "rankings": -50, "draft profile": -20, "analysis": -35,
    "breakdown": -25, "workout": -30, "edit": -40, "mix": -35, "amv": -60,
    "madden": -70, "highlight song": -40, "vs": -8, "full game": -45,
    "live": -30, "preview": -30, "news": -40, "trade": -40, "injury": -50,
    # Camp and preseason footage is technically "highlights" but it is guys in
    # shorts doing drills — not what anyone wants to see on a draft night.
    "training camp": -70, "practice": -55, "preseason": -50, "ota": -50,
    "minicamp": -60, "combine": -25,
}


@dataclass
class VideoCandidate:
    video_id: str
    title: str
    channel: str
    duration: str
    seconds: int
    score: int

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_duration(text: str | None) -> int:
    if not text:
        return 0
    parts = [p for p in text.split(":") if p.strip().isdigit()]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _extract(payload: dict) -> list[tuple]:
    """Walk ytInitialData collecting every videoRenderer."""
    found: list[tuple] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            renderer = node.get("videoRenderer")
            if isinstance(renderer, dict) and renderer.get("videoId"):
                title = ""
                runs = renderer.get("title", {}).get("runs") or []
                if runs:
                    title = runs[0].get("text", "")
                channel = ""
                owner = renderer.get("ownerText", {}).get("runs") or []
                if owner:
                    channel = owner[0].get("text", "")
                duration = renderer.get("lengthText", {}).get("simpleText", "")
                found.append((renderer["videoId"], title, channel, duration))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _score(title: str, channel: str, seconds: int, player: Player) -> int:
    lower_title = title.lower()
    lower_channel = channel.lower().strip()
    score = 0

    for word, weight in _GOOD_WORDS.items():
        if word in lower_title:
            score += weight
    for word, weight in _BAD_WORDS.items():
        if word in lower_title:
            score += weight

    # The player's name in the title is a strong relevance signal.
    surname = player.name.split()[-1].lower() if player.name else ""
    if surname and surname in lower_title:
        score += 25
    elif surname:
        score -= 45

    if lower_channel in _OFFICIAL or "highlight" in lower_channel:
        score += 20
    if player.team and player.display_team.lower() in lower_channel:
        score += 18

    # Recency: last season's tape is what everyone wants to argue about.
    if "2025" in title:
        score += 22
    elif any(year in title for year in ("2019", "2020", "2021", "2022")):
        score -= 12

    # A punchy 2-12 minute reel beats a 40-minute compilation.
    if 120 <= seconds <= 720:
        score += 15
    elif seconds < 45:
        score -= 25
    elif seconds > 1800:
        score -= 20

    return score


DEFENSIVE_POSITIONS = {
    "DL", "DE", "DT", "NT", "EDGE", "LB", "OLB", "ILB", "MLB",
    "DB", "CB", "S", "SS", "FS",
}


def build_query(player: Player) -> str:
    """Query shape varies by player type — this is where the flavor comes from."""
    if player.is_dst:
        return f"{player.display_team} defense highlights 2025"
    if player.position == "TQB":
        return f"{player.display_team} quarterback highlights 2025"
    if player.position == "K":
        return f"{player.name} longest field goals highlights"
    if player.is_rookie:
        college = player.college or ""
        return f"{player.name} {college} highlights".strip()
    if player.position in DEFENSIVE_POSITIONS:
        # "Highlights" alone tends to surface offensive plays against them.
        return f"{player.name} sacks tackles highlights 2025"
    return f"{player.name} 2025 highlights"


async def find_videos(
    player: Player,
    client: httpx.AsyncClient | None = None,
    limit: int = 3,
) -> list[dict]:
    """Return up to `limit` ranked clips. Never raises — an empty list means
    the display falls back to the stat card."""
    query = build_query(player)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True)

    try:
        resp = await client.get(SEARCH_URL.format(q=quote_plus(query)))
        resp.raise_for_status()
        match = _INITIAL_DATA.search(resp.text)
        if not match:
            log.warning("No ytInitialData for %r (page structure changed?)", query)
            return []

        raw = _extract(json.loads(match.group(1)))
        seen: set[str] = set()
        candidates: list[VideoCandidate] = []
        for video_id, title, channel, duration in raw:
            if video_id in seen:
                continue
            seen.add(video_id)
            seconds = _parse_duration(duration)
            candidates.append(
                VideoCandidate(
                    video_id=video_id,
                    title=title,
                    channel=channel,
                    duration=duration,
                    seconds=seconds,
                    score=_score(title, channel, seconds, player),
                )
            )

        candidates.sort(key=lambda c: c.score, reverse=True)
        top = [c for c in candidates if c.score > 0][:limit]
        if not top:
            log.info("No positively-scored clip for %s", player.name)
        return [c.to_dict() for c in top]

    except Exception as exc:
        log.warning("YouTube lookup failed for %s: %s", player.name, exc)
        return []
    finally:
        if owns_client:
            await client.aclose()


async def find_videos_rate_limited(
    players: list[Player], delay: float = 1.0, limit: int = 3
) -> dict[str, list[dict]]:
    """Sequential lookups with a delay, for the prewarm pass."""
    results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(
        timeout=20, headers=_HEADERS, follow_redirects=True
    ) as client:
        for index, player in enumerate(players, 1):
            results[player.player_id] = await find_videos(player, client, limit)
            print(
                f"  [{index}/{len(players)}] {player.name:<28} "
                f"{len(results[player.player_id])} clip(s)",
                flush=True,
            )
            if index < len(players):
                await asyncio.sleep(delay)
    return results
