"""Assembles hype packets and caches them to disk.

A packet is everything the display needs for one pick. Prewarming these before
draft day is what makes a pick reveal feel instant instead of laggy — a cold
YouTube lookup takes 1-2s, which is dead air on a TV.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from .constants import POSITION_COLORS
from .players import Player, PlayerDB
from .stats import StatsDB
from .youtube import find_videos

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_FILE = DATA_DIR / "hype_cache.json"


class HypeCache:
    def __init__(self, players: PlayerDB, stats: StatsDB) -> None:
        self.players = players
        self.stats = stats
        self._cache: dict[str, dict] = {}

    def load(self) -> None:
        if CACHE_FILE.exists():
            try:
                self._cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                log.info("Loaded %d prewarmed hype packets", len(self._cache))
                return
            except Exception as exc:
                log.warning("Hype cache unreadable (%s); starting empty", exc)
        self._cache = {}

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")
        log.info("Saved %d hype packets", len(self._cache))

    def _base_packet(self, player: Player) -> dict:
        return {
            "player_id": player.player_id,
            "name": player.name,
            "position": player.position,
            "team": player.team,
            "team_full": player.display_team,
            "college": player.college,
            "age": player.age,
            "number": player.number,
            "is_rookie": player.is_rookie,
            "headshot": player.headshot_url,
            "headshot_fallback": player.fallback_headshot_url,
            "team_logo": f"https://a.espncdn.com/i/teamlogos/nfl/500/{player.team.lower()}.png"
            if player.team and player.team != "FA"
            else None,
            "color": POSITION_COLORS.get(player.position, "#8a94a6"),
            "stat_line": self.stats.stat_line(player),
            "tier": self.stats.tier(player),
            "ppr_points": self.stats.ppr_points(player),
            "ppr_rank": self.stats.ppr_rank(player),
        }

    async def build(
        self,
        player: Player,
        client: httpx.AsyncClient | None = None,
        use_cache: bool = True,
    ) -> dict:
        """Return the packet for a player, hitting the disk cache when possible."""
        key = str(player.player_id)
        if use_cache and key in self._cache:
            cached = self._cache[key]
            # Stats and bio are cheap to recompute and may have changed;
            # the expensive part (the video lookup) is what we reuse.
            packet = self._base_packet(player)
            packet["videos"] = cached.get("videos", [])
            packet["cached"] = True
            return packet

        # Uncached pick mid-draft: no time to verify embeds, so pull a deeper
        # candidate list and let the display's fallback chain sort it out.
        # Roughly a third of raw search hits actually play.
        packet = self._base_packet(player)
        packet["videos"] = await find_videos(player, client, limit=5)
        packet["cached"] = False
        self._cache[key] = {"name": player.name, "videos": packet["videos"]}
        return packet

    def set_videos(self, player_id: str, name: str, videos: list[dict]) -> None:
        self._cache[str(player_id)] = {"name": name, "videos": videos}

    def has(self, player_id: str) -> bool:
        return str(player_id) in self._cache

    def has_videos(self, player_id: str) -> bool:
        """True only if the cached entry actually has a clip. A player can be
        `has()` (an entry exists) but still empty — a prior prewarm searched
        and came up with nothing playable. `--missing` should retry those,
        not treat an empty result as done forever."""
        entry = self._cache.get(str(player_id))
        return bool(entry and entry.get("videos"))

    @property
    def size(self) -> int:
        return len(self._cache)
