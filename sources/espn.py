"""ESPN draft source.

Contrary to a lot of published advice, `view=mDraftDetail` *does* work during a
live draft in the current season. It returns the full pick grid pre-seeded with
`playerId: -1`, and each slot flips to a real ESPN athlete ID as picks land.

Private leagues need `espn_s2` and `SWID` cookies; public leagues need nothing.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from enrich.constants import ESPN_POSITIONS

from .base import DraftSource, Pick

log = logging.getLogger(__name__)

BASE_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
    "/seasons/{season}/segments/0/leagues/{league_id}"
)

# One bulk pull of the relevant player pool beats a lookup call per pick.
POOL_FILTER = json.dumps(
    {"players": {"limit": 1500, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
)


class EspnSource(DraftSource):
    platform = "espn"
    poll_interval = 3.0

    def __init__(
        self,
        league_id: str,
        season: int,
        espn_s2: str | None = None,
        swid: str | None = None,
    ) -> None:
        super().__init__()
        self.league_id = str(league_id)
        self.season = int(season)

        cookies = {}
        if espn_s2 and swid:
            cookies = {"espn_s2": espn_s2, "SWID": swid}
            log.info("ESPN cookies supplied (private league mode)")

        self._client = httpx.AsyncClient(
            timeout=15,
            cookies=cookies,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        self._pool: dict[int, dict] = {}
        self._team_names: dict[int, str] = {}

    @property
    def _url(self) -> str:
        return BASE_URL.format(season=self.season, league_id=self.league_id)

    async def load_metadata(self) -> dict:
        """Fetch team names and the player pool. Call once before polling."""
        resp = await self._client.get(self._url, params={"view": "mTeam"})
        resp.raise_for_status()
        data = resp.json()
        for team in data.get("teams", []):
            name = team.get("name") or " ".join(
                filter(None, [team.get("location"), team.get("nickname")])
            )
            self._team_names[team["id"]] = name or f"Team {team['id']}"

        await self._load_pool()

        return {
            "name": data.get("settings", {}).get("name"),
            "teams": len(self._team_names),
            "pool": len(self._pool),
        }

    async def _load_pool(self) -> None:
        try:
            resp = await self._client.get(
                self._url,
                params={"view": "kona_player_info"},
                headers={"X-Fantasy-Filter": POOL_FILTER},
            )
            resp.raise_for_status()
            for entry in resp.json().get("players", []):
                player = entry.get("player") or {}
                if player.get("id"):
                    self._pool[int(player["id"])] = player
            log.info("ESPN player pool: %d players", len(self._pool))
        except Exception as exc:
            log.warning("ESPN player pool fetch failed (%s); names come from IDs", exc)

    async def _lookup_missing(self, player_id: int) -> dict | None:
        """Deep-bench pick outside the cached pool — ask ESPN's core API.

        The core API uses its own position abbreviations rather than the
        fantasy numeric IDs, so map them back to keep one vocabulary.
        """
        url = (
            "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
            f"/athletes/{player_id}"
        )
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()

            abbrev = (data.get("position") or {}).get("abbreviation")
            position_id = next(
                (pid for pid, name in ESPN_POSITIONS.items() if name == abbrev), None
            )

            team_id = None
            team_ref = (data.get("team") or {}).get("$ref", "")
            match = re.search(r"/teams/(\d+)", team_ref)
            if match:
                team_id = int(match.group(1))

            return {
                "id": player_id,
                "fullName": data.get("displayName"),
                "defaultPositionId": position_id,
                "proTeamId": team_id,
            }
        except Exception as exc:
            log.warning("Could not resolve ESPN athlete %s: %s", player_id, exc)
            return None

    async def fetch_picks(self) -> list[Pick]:
        resp = await self._client.get(self._url, params={"view": "mDraftDetail"})
        resp.raise_for_status()
        detail = resp.json().get("draftDetail") or {}

        picks: list[Pick] = []
        for row in detail.get("picks", []):
            player_id = row.get("playerId")
            # -1 means the slot exists but the pick hasn't been made yet.
            if not player_id or player_id == -1:
                continue

            player = self._pool.get(int(player_id))
            if player is None:
                player = await self._lookup_missing(int(player_id)) or {}
                if player:
                    self._pool[int(player_id)] = player

            picks.append(
                Pick(
                    overall=int(row.get("overallPickNumber") or 0),
                    round=int(row.get("roundId") or 0),
                    round_pick=int(row.get("roundPickNumber") or 0),
                    team_name=self._team_names.get(
                        row.get("teamId"), f"Team {row.get('teamId')}"
                    ),
                    player_key=str(player_id),
                    platform=self.platform,
                    raw_name=player.get("fullName"),
                    espn_position_id=player.get("defaultPositionId"),
                    espn_pro_team_id=player.get("proTeamId"),
                )
            )
        return picks

    async def is_live(self) -> bool:
        resp = await self._client.get(self._url, params={"view": "mDraftDetail"})
        resp.raise_for_status()
        detail = resp.json().get("draftDetail") or {}
        return bool(detail.get("inProgress"))

    async def close(self) -> None:
        await self._client.aclose()
