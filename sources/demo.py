"""Replays a finished draft on a timer.

This is the primary test surface. A live draft is unforgiving and un-debuggable,
so everything gets proven against a real completed draft first — including the
burst behaviour that autodrafting causes.

Default replay data is the completed 2025 ESPN league 899513 (204 picks).
"""

from __future__ import annotations

import logging
import time

from .base import DraftSource, Pick
from .espn import EspnSource
from .sleeper import SleeperSource

log = logging.getLogger(__name__)


class DemoSource(DraftSource):
    platform = "demo"
    poll_interval = 0.5

    def __init__(self, picks: list[Pick], interval: float = 10.0) -> None:
        super().__init__()
        self._all = sorted(picks, key=lambda p: p.overall)
        self.interval = interval
        self._t0 = time.monotonic()
        log.info("Demo source loaded with %d picks, %.1fs apart", len(self._all), interval)

    @classmethod
    async def from_espn(
        cls,
        league_id: str = "899513",
        season: int = 2025,
        interval: float = 10.0,
        espn_s2: str | None = None,
        swid: str | None = None,
    ) -> "DemoSource":
        source = EspnSource(league_id, season, espn_s2, swid)
        try:
            await source.load_metadata()
            picks = await source.fetch_picks()
        finally:
            await source.close()
        if not picks:
            raise ValueError(f"ESPN league {league_id} season {season} has no picks")
        return cls(picks, interval)

    @classmethod
    async def from_sleeper(cls, draft_id: str, interval: float = 10.0) -> "DemoSource":
        source = SleeperSource(draft_id)
        try:
            await source.load_metadata()
            picks = await source.fetch_picks()
        finally:
            await source.close()
        if not picks:
            raise ValueError(f"Sleeper draft {draft_id} has no picks")
        return cls(picks, interval)

    async def fetch_picks(self) -> list[Pick]:
        """Reveal picks gradually, as if the draft were happening now."""
        elapsed = time.monotonic() - self._t0
        revealed = int(elapsed / self.interval) + 1
        return self._all[:revealed]

    async def snapshot(self) -> int:
        # A replay should always start from pick 1.
        self._t0 = time.monotonic()
        self._seen = set()
        self.started = True
        return 0

    @property
    def exhausted(self) -> bool:
        return len(self._seen) >= len(self._all)
