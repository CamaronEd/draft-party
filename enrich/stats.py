"""2025 season stat lines from Sleeper, formatted for the hype card.

Each position gets its own handful of headline numbers — a QB's card should
show passing yards, a kicker's should show field goals.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from .players import Player

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATS_SEASON = "2025"
STATS_URL = f"https://api.sleeper.app/v1/stats/nfl/regular/{STATS_SEASON}"
STATS_FILE = DATA_DIR / f"stats_{STATS_SEASON}.json"
MAX_AGE_SECONDS = 24 * 60 * 60

IDP_POSITIONS = {
    "DL", "DE", "DT", "NT", "EDGE", "LB", "OLB", "ILB", "MLB",
    "DB", "CB", "S", "SS", "FS",
}


def _num(value, decimals: int = 0) -> str:
    if value is None:
        return "0"
    if decimals:
        return f"{value:,.{decimals}f}"
    return f"{value:,.0f}"


class StatsDB:
    def __init__(self) -> None:
        self._stats: dict = {}

    async def load(self, force_refresh: bool = False) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fresh = (
            STATS_FILE.exists()
            and (time.time() - STATS_FILE.stat().st_mtime) < MAX_AGE_SECONDS
        )
        if fresh and not force_refresh:
            self._stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            log.info("Using cached %s stats (%d players)", STATS_SEASON, len(self._stats))
            return

        log.info("Downloading %s season stats...", STATS_SEASON)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(STATS_URL)
                resp.raise_for_status()
                self._stats = resp.json()
            STATS_FILE.write_text(json.dumps(self._stats), encoding="utf-8")
        except Exception as exc:
            if STATS_FILE.exists():
                log.warning("Stats download failed (%s); using stale cache", exc)
                self._stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            else:
                log.error("Stats unavailable (%s); cards will show bio only", exc)
                self._stats = {}

    def raw(self, player: Player) -> dict:
        return self._stats.get(str(player.player_id), {}) or {}

    def ppr_rank(self, player: Player) -> int | None:
        """Rank *within the position* — the WR4 in 'WR4', not the overall rank."""
        rank = self.raw(player).get("pos_rank_ppr")
        return int(rank) if rank else None

    def overall_rank(self, player: Player) -> int | None:
        """Rank across every position. Sleeper calls this `rank_ppr`."""
        rank = self.raw(player).get("rank_ppr")
        return int(rank) if rank else None

    def ppr_points(self, player: Player) -> float | None:
        pts = self.raw(player).get("pts_ppr")
        return float(pts) if pts else None

    def stat_line(self, player: Player) -> list[dict]:
        """Headline numbers for the card, as [{label, value}] in display order."""
        s = self.raw(player)
        pos = player.position

        if not s:
            return self._bio_line(player)

        line: list[dict] = []
        if pos == "QB":
            line = [
                {"label": "PASS YDS", "value": _num(s.get("pass_yd"))},
                {"label": "PASS TD", "value": _num(s.get("pass_td"))},
                {"label": "INT", "value": _num(s.get("pass_int"))},
                {"label": "RUSH YDS", "value": _num(s.get("rush_yd"))},
            ]
        elif pos == "RB":
            line = [
                {"label": "RUSH YDS", "value": _num(s.get("rush_yd"))},
                {"label": "RUSH TD", "value": _num(s.get("rush_td"))},
                {"label": "REC", "value": _num(s.get("rec"))},
                {"label": "REC YDS", "value": _num(s.get("rec_yd"))},
            ]
        elif pos in ("WR", "TE"):
            line = [
                {"label": "REC", "value": _num(s.get("rec"))},
                {"label": "REC YDS", "value": _num(s.get("rec_yd"))},
                {"label": "REC TD", "value": _num(s.get("rec_td"))},
                {"label": "TARGETS", "value": _num(s.get("rec_tgt"))},
            ]
        elif pos == "K":
            line = [
                {"label": "FG MADE", "value": _num(s.get("fgm"))},
                {"label": "FG ATT", "value": _num(s.get("fga"))},
                {"label": "XP MADE", "value": _num(s.get("xpm"))},
                {"label": "LONG", "value": _num(s.get("fgm_lng"))},
            ]
        elif pos in ("DST", "DEF"):
            line = [
                {"label": "SACKS", "value": _num(s.get("def_sack"))},
                {"label": "INT", "value": _num(s.get("def_int"))},
                {"label": "FUM REC", "value": _num(s.get("def_fr"))},
                {"label": "DEF TD", "value": _num(s.get("def_td"))},
            ]
        elif pos in IDP_POSITIONS:
            # Front seven lead with pressure, secondary leads with coverage.
            if pos in ("CB", "S", "SS", "FS", "DB"):
                line = [
                    {"label": "TACKLES", "value": _num(s.get("idp_tkl"))},
                    {"label": "PASS DEF", "value": _num(s.get("idp_pass_def"))},
                    {"label": "INT", "value": _num(s.get("idp_int"))},
                    {"label": "SACKS", "value": _num(s.get("idp_sack"), 1)},
                ]
            else:
                line = [
                    {"label": "SACKS", "value": _num(s.get("idp_sack"), 1)},
                    {"label": "TACKLES", "value": _num(s.get("idp_tkl"))},
                    {"label": "TFL", "value": _num(s.get("idp_tkl_loss"))},
                    {"label": "QB HITS", "value": _num(s.get("idp_qb_hit"))},
                ]

        # Fantasy production is the number that actually matters at a draft.
        pts = self.ppr_points(player)
        if pts:
            rank = self.ppr_rank(player)
            label = f"{pos}{rank}" if rank else "PPR PTS"
            line.append({"label": label, "value": _num(pts, 1), "highlight": True})

        line = [x for x in line if x["value"] != "0"] or self._bio_line(player)
        return line[:5]

    def _bio_line(self, player: Player) -> list[dict]:
        """Rookies and anyone without 2025 production get a bio card instead."""
        line: list[dict] = []
        if player.college:
            line.append({"label": "COLLEGE", "value": player.college})
        if player.age:
            line.append({"label": "AGE", "value": str(player.age)})
        if player.height and player.weight:
            feet, inches = divmod(int(player.height), 12)
            line.append({"label": "SIZE", "value": f"{feet}'{inches}\" · {player.weight}"})
        if player.is_rookie:
            line.append({"label": "STATUS", "value": "ROOKIE", "highlight": True})
        elif player.years_exp:
            line.append({"label": "EXP", "value": f"{player.years_exp} YRS"})
        return line[:5]

    def tier(self, player: Player) -> str:
        """Drives how big the moment feels on screen. Uses overall rank, since
        that reflects real draft value across positions."""
        rank = self.overall_rank(player)
        if rank and rank <= 24:
            return "elite"
        if rank and rank <= 72:
            return "starter"
        if player.is_rookie:
            return "rookie"
        return "depth"
