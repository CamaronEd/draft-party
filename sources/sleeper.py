"""Sleeper draft source.

Fully public — no auth, no cookies. Sleeper's guidance is to stay under 1000
requests/minute; polling every 2s is 30/min.
"""

from __future__ import annotations

import logging
import re

import httpx

from .base import DraftSource, Pick

log = logging.getLogger(__name__)

PICKS_URL = "https://api.sleeper.app/v1/draft/{draft_id}/picks"
DRAFT_URL = "https://api.sleeper.app/v1/draft/{draft_id}"
LEAGUE_DRAFTS_URL = "https://api.sleeper.app/v1/league/{league_id}/drafts"
LEAGUE_USERS_URL = "https://api.sleeper.app/v1/league/{league_id}/users"


def parse_draft_id(value: str) -> str:
    """Accept a bare ID or any Sleeper URL containing one."""
    value = value.strip()
    if value.isdigit():
        return value
    match = re.search(r"/draft/(?:nfl/)?(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"(\d{15,})", value)
    if match:
        return match.group(1)
    raise ValueError(f"Could not find a Sleeper draft ID in {value!r}")


class SleeperSource(DraftSource):
    platform = "sleeper"
    poll_interval = 2.0

    def __init__(self, draft_id: str) -> None:
        super().__init__()
        self.draft_id = parse_draft_id(draft_id)
        self._client = httpx.AsyncClient(timeout=15)
        self._slot_names: dict[int, str] = {}

    @classmethod
    async def from_league(cls, league_id: str) -> "SleeperSource":
        """Resolve the most recent draft for a league ID."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(LEAGUE_DRAFTS_URL.format(league_id=league_id))
            resp.raise_for_status()
            drafts = resp.json()
        if not drafts:
            raise ValueError(f"League {league_id} has no drafts")
        return cls(str(drafts[0]["draft_id"]))

    async def load_metadata(self) -> dict:
        """Draft settings, plus real team names keyed by draft slot."""
        resp = await self._client.get(DRAFT_URL.format(draft_id=self.draft_id))
        resp.raise_for_status()
        draft = resp.json()

        await self._load_team_names(draft)

        metadata = draft.get("metadata") or {}
        return {
            "type": draft.get("type"),
            "status": draft.get("status"),
            "teams": draft.get("settings", {}).get("teams"),
            "rounds": draft.get("settings", {}).get("rounds"),
            "name": metadata.get("name") or draft.get("league_id"),
        }

    async def _load_team_names(self, draft: dict) -> None:
        """Resolve draft slot -> the manager's team name.

        `draft_order` maps user_id -> slot; the league's user list turns that
        into something worth putting on screen. Falls back to "Team N".
        """
        draft_order = draft.get("draft_order") or {}
        league_id = draft.get("league_id")
        if not draft_order or not league_id:
            return

        try:
            resp = await self._client.get(
                LEAGUE_USERS_URL.format(league_id=league_id)
            )
            resp.raise_for_status()
            users = resp.json()
        except Exception as exc:
            log.warning("Could not load Sleeper team names (%s)", exc)
            return

        by_user = {}
        for user in users:
            name = (user.get("metadata") or {}).get("team_name")
            by_user[user.get("user_id")] = name or user.get("display_name")

        for user_id, slot in draft_order.items():
            name = by_user.get(user_id)
            if name:
                self._slot_names[int(slot)] = name
        log.info("Resolved %d Sleeper team names", len(self._slot_names))

    async def fetch_picks(self) -> list[Pick]:
        resp = await self._client.get(PICKS_URL.format(draft_id=self.draft_id))
        resp.raise_for_status()

        picks: list[Pick] = []
        for row in resp.json():
            player_id = row.get("player_id")
            if not player_id:
                continue
            meta = row.get("metadata") or {}
            name = " ".join(
                filter(None, [meta.get("first_name"), meta.get("last_name")])
            ) or None
            picks.append(
                Pick(
                    overall=int(row.get("pick_no") or 0),
                    round=int(row.get("round") or 0),
                    round_pick=int(row.get("draft_slot") or 0),
                    team_name=self._team_name(row),
                    player_key=str(player_id),
                    platform=self.platform,
                    raw_name=name,
                )
            )
        return picks

    def _team_name(self, row: dict) -> str:
        slot = row.get("draft_slot")
        if slot and slot in self._slot_names:
            return self._slot_names[slot]
        return f"Team {slot}" if slot else "Unknown"

    async def close(self) -> None:
        await self._client.aclose()
