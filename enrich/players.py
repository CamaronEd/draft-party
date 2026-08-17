"""Player database: Sleeper's full NFL player DB plus the ESPN name bridge.

Sleeper's `espn_id` field is only ~46% populated (null even for stars like
Ja'Marr Chase), so ESPN picks are matched into Sleeper by normalized name +
position rather than by ID.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .constants import ESPN_POSITIONS, ESPN_PRO_TEAMS, POSITION_GROUPS, TEAM_NAMES

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
PLAYERS_FILE = DATA_DIR / "players.json"
MAX_AGE_SECONDS = 24 * 60 * 60

# Includes IDP so defensive leagues work — ESPN leagues commonly draft them.
SKILL_POSITIONS = {
    "QB", "RB", "WR", "TE", "K", "FB", "DST", "DEF",
    "DL", "DE", "DT", "NT", "EDGE",
    "LB", "OLB", "ILB", "MLB",
    "DB", "CB", "S", "SS", "FS",
}

# Suffixes stripped before name matching, so "Marvin Harrison Jr." from ESPN
# lines up with "Marvin Harrison" from Sleeper.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, punctuation, and generational suffixes."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    tokens = re.sub(r"[^a-zA-Z\s]", "", ascii_name).lower().split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return "".join(tokens)


@dataclass
class Player:
    """A resolved player, normalized across both platforms."""

    player_id: str          # Sleeper ID, or team abbreviation for a DST
    name: str
    position: str
    team: str
    college: str | None = None
    age: int | None = None
    years_exp: int | None = None
    number: int | None = None
    height: int | None = None
    weight: int | None = None
    espn_id: str | None = None
    search_rank: int = 9999
    matched: bool = True    # False when an ESPN pick had no Sleeper counterpart

    @property
    def is_rookie(self) -> bool:
        return self.years_exp == 0

    @property
    def is_dst(self) -> bool:
        return self.position in ("DST", "DEF")

    @property
    def is_team_entity(self) -> bool:
        """A whole-team slot (D/ST or ESPN's Team QB) rather than a person."""
        return self.position in ("DST", "DEF", "TQB")

    @property
    def headshot_url(self) -> str:
        """Sleeper CDN for people, ESPN team logo for team slots."""
        if self.is_team_entity:
            return f"https://a.espncdn.com/i/teamlogos/nfl/500/{self.team.lower()}.png"
        return f"https://sleepercdn.com/content/nfl/players/{self.player_id}.jpg"

    @property
    def fallback_headshot_url(self) -> str | None:
        if self.espn_id and not self.is_team_entity:
            return f"https://a.espncdn.com/i/headshots/nfl/players/full/{self.espn_id}.png"
        return None

    @property
    def display_team(self) -> str:
        return TEAM_NAMES.get(self.team, self.team)


class PlayerDB:
    """Loads the Sleeper player DB and indexes it for both platforms."""

    def __init__(self) -> None:
        self._raw: dict = {}
        self._by_id: dict[str, Player] = {}
        self._by_name_pos: dict[tuple[str, str], list[Player]] = {}
        self._by_name: dict[str, list[Player]] = {}

    async def load(self, force_refresh: bool = False) -> None:
        raw = await self._fetch(force_refresh)
        self._raw = raw
        self._build_index()
        log.info("Player DB ready: %d indexed players", len(self._by_id))

    async def _fetch(self, force_refresh: bool) -> dict:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fresh = (
            PLAYERS_FILE.exists()
            and (time.time() - PLAYERS_FILE.stat().st_mtime) < MAX_AGE_SECONDS
        )
        if fresh and not force_refresh:
            log.info("Using cached player DB")
            return json.loads(PLAYERS_FILE.read_text(encoding="utf-8"))

        log.info("Downloading Sleeper player DB (~14MB, this takes a moment)...")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.get(PLAYERS_URL)
                resp.raise_for_status()
                data = resp.json()
            PLAYERS_FILE.write_text(json.dumps(data), encoding="utf-8")
            return data
        except Exception as exc:
            # A stale cache beats no cache — the draft is live and can't wait.
            if PLAYERS_FILE.exists():
                log.warning("Player DB download failed (%s); using stale cache", exc)
                return json.loads(PLAYERS_FILE.read_text(encoding="utf-8"))
            raise

    def _build_index(self) -> None:
        self._by_id.clear()
        self._by_name_pos.clear()
        self._by_name.clear()

        for pid, rec in self._raw.items():
            position = rec.get("position") or ""
            if position not in SKILL_POSITIONS:
                continue
            name = rec.get("full_name") or " ".join(
                filter(None, [rec.get("first_name"), rec.get("last_name")])
            )
            if not name:
                continue

            player = Player(
                player_id=pid,
                name=name,
                position="DST" if position in ("DST", "DEF") else position,
                team=rec.get("team") or "FA",
                college=rec.get("college"),
                age=rec.get("age"),
                years_exp=rec.get("years_exp"),
                number=rec.get("number"),
                height=rec.get("height"),
                weight=rec.get("weight"),
                espn_id=str(rec["espn_id"]) if rec.get("espn_id") else None,
                search_rank=rec.get("search_rank") or 9999,
            )
            self._by_id[pid] = player
            norm = normalize_name(name)
            self._by_name_pos.setdefault((norm, player.position), []).append(player)
            self._by_name.setdefault(norm, []).append(player)

    def get_by_sleeper_id(self, player_id: str) -> Player | None:
        """Sleeper picks. DSTs come through keyed by team abbreviation."""
        player = self._by_id.get(str(player_id))
        if player:
            return player
        # Sleeper keys team defenses as "CIN", "KC", etc.
        key = str(player_id).upper()
        if key in TEAM_NAMES:
            return Player(
                player_id=key,
                name=f"{TEAM_NAMES[key]} D/ST",
                position="DST",
                team=key,
            )
        return None

    def get_by_espn(
        self,
        espn_id: int | str,
        name: str,
        position_id: int | None = None,
        pro_team_id: int | None = None,
    ) -> Player:
        """Bridge an ESPN pick into a Player, falling back to ESPN's own data."""
        position = ESPN_POSITIONS.get(position_id or -1, "")
        team = ESPN_PRO_TEAMS.get(pro_team_id or -1, "FA")

        # ESPN team entities (D/ST and its "Team QB" slot) have no Sleeper
        # counterpart — build them straight from the pro team.
        if position in ("DST", "TQB"):
            suffix = "D/ST" if position == "DST" else "Team QB"
            return Player(
                player_id=f"{team}-{position}",
                name=f"{TEAM_NAMES.get(team, team)} {suffix}",
                position=position,
                team=team,
                espn_id=str(espn_id),
            )

        norm = normalize_name(name)

        # Tier 1: exact name + position.
        candidates = self._by_name_pos.get((norm, position), [])

        # Tier 2: same name, same side of the ball. ESPN and Sleeper disagree on
        # defensive labels (ESPN "DE" where Sleeper says "DL" or "LB").
        if not candidates:
            group = POSITION_GROUPS.get(position)
            candidates = [
                p for p in self._by_name.get(norm, [])
                if not group or POSITION_GROUPS.get(p.position) == group
            ]

        # Tier 3: name alone. Full names are distinctive enough that this is
        # safe, and a slightly-wrong position beats a blank card on the TV.
        if not candidates:
            candidates = self._by_name.get(norm, [])

        match = None
        if len(candidates) == 1:
            match = candidates[0]
        elif candidates:
            # Same name more than once: pro team is the tiebreaker.
            match = next((c for c in candidates if c.team == team), candidates[0])

        if match:
            if not match.espn_id:
                match.espn_id = str(espn_id)
            return match

        log.warning("No Sleeper match for ESPN player %s (%s %s)", name, position, team)
        return Player(
            player_id=f"espn-{espn_id}",
            name=name,
            position=position or "?",
            team=team,
            espn_id=str(espn_id),
            matched=False,
        )

    def top_by_adp(self, limit: int = 300) -> list[Player]:
        """Best available proxy for ADP: Sleeper's own search_rank."""
        ranked = [p for p in self._by_id.values() if p.search_rank < 9999]
        ranked.sort(key=lambda p: p.search_rank)
        return ranked[:limit]
