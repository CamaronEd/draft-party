"""Common shape for every draft source.

ESPN and Sleeper differ in auth, IDs, and payload shape, but both reduce to
"a new pick happened". Everything downstream works off `Pick`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Pick:
    overall: int
    round: int
    round_pick: int
    team_name: str
    player_key: str            # Sleeper player_id, or ESPN athlete id
    platform: str
    raw_name: str | None = None
    espn_position_id: int | None = None
    espn_pro_team_id: int | None = None

    @property
    def label(self) -> str:
        """Draft-board notation, e.g. '3.07'."""
        return f"{self.round}.{self.round_pick:02d}"


class DraftSource(ABC):
    """Polls a platform and emits only picks it hasn't seen before."""

    platform = "base"
    poll_interval = 3.0

    def __init__(self) -> None:
        self._seen: set[int] = set()
        self.started = False

    @abstractmethod
    async def fetch_picks(self) -> list[Pick]:
        """Return every pick currently visible on the platform."""

    async def snapshot(self) -> int:
        """Mark existing picks as seen so joining mid-draft doesn't replay them."""
        picks = await self.fetch_picks()
        self._seen = {p.overall for p in picks}
        self.started = True
        return len(self._seen)

    async def poll(self) -> list[Pick]:
        """Return only picks made since the last call, in draft order."""
        picks = await self.fetch_picks()
        fresh = [p for p in picks if p.overall not in self._seen]
        for pick in fresh:
            self._seen.add(pick.overall)
        fresh.sort(key=lambda p: p.overall)
        return fresh

    async def close(self) -> None:
        return None
