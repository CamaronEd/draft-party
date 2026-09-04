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

from .constants import TEAM_NAMES
from .players import Player

log = logging.getLogger(__name__)

# sp=EgIQAQ%3D%3D filters results to videos only (no channels, playlists, Shorts).
SEARCH_URL = "https://www.youtube.com/results?search_query={q}&sp=EgIQAQ%253D%253D"
_INITIAL_DATA = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});</script>", re.DOTALL)

# Matches STATS_SEASON in stats.py — bump both together each offseason.
CURRENT_SEASON_YEAR = 2025

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
    # Fan edits imagining a trade/signing that hasn't happened — not what the
    # room wants when the player's real team just drafted him.
    "welcome to": -50,
    # High school tape turns up disproportionately often for the biggest NFL
    # stars — recruiting-site channels tag it heavily with the player's full
    # name, and it's never the right clip for an NFL hype card.
    "high school": -80, "maxpreps": -60,
    # Content-farm bio slideshows ("Meet <Player>: Age, Net Worth, Girlfriend")
    # tag every star's full name too but contain zero game footage.
    "net worth": -80, "girlfriend": -80, "lifestyle": -60, "biography": -60,
}

# A D/ST pick wants the defense, not the team's generic (usually offense-heavy)
# reel — these tip the scale toward clips that are actually about the defense.
_DEFENSE_WORDS = {
    "defense", "defensive", "sack", "sacks", "interception", "interceptions",
    "pick six", "turnover", "turnovers", "takeaway", "takeaways",
    "goal line stand", "forced fumble", "fumble recovery",
}
_OFFENSE_WORDS = {
    "touchdown", "touchdowns", "offense", "offensive", "passing", "receiving",
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


def _names_a_different_player(lower_title: str, surname: str, first_name: str) -> bool:
    """True if the word right before `surname` in the title is a first name
    that isn't this player's — e.g. "Luke McCaffrey" when we searched for
    Christian McCaffrey. A title with no name in front of the surname at all
    ("McCaffrey Highlights") isn't flagged — that's just missing, not wrong."""
    if not first_name:
        return False
    idx = lower_title.find(surname)
    if idx <= 0:
        return False
    before = lower_title[:idx].rstrip(" .")
    match = re.search(r"([a-z']+)$", before)
    if not match:
        return False
    preceding = match.group(1)
    if not preceding or preceding == surname:
        return False
    # Allow initials ("C. McCaffrey") and partial typing either direction.
    return not (first_name.startswith(preceding) or preceding.startswith(first_name))


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

    if player.is_team_entity:
        # Team slots (D/ST, ESPN's Team QB) aren't a person — there's no
        # "surname" to check. Reward the team's city/mascot showing up instead.
        team_words = [w for w in player.display_team.lower().split() if len(w) > 3]
        if team_words and any(w in lower_title for w in team_words):
            score += 20
    else:
        # The player's name in the title is a strong relevance signal. But
        # surname alone isn't enough — plenty of NFL players share one
        # (Christian McCaffrey vs. his brother Luke, Josh Allen vs. Keenan
        # Allen, etc.) — so if a *different* first name sits right next to the
        # surname, it's almost certainly someone else's clip.
        name_parts = player.name.lower().split() if player.name else []
        surname = name_parts[-1] if name_parts else ""
        first_name = name_parts[0] if len(name_parts) > 1 else ""
        if surname and surname in lower_title:
            if _names_a_different_player(lower_title, surname, first_name):
                score -= 40
            else:
                score += 25
        elif surname:
            score -= 45

    if player.is_dst:
        # A team's default "highlights" reel skews offense — a defense pick
        # wants footage of the defense, not the team's touchdown catches.
        if any(word in lower_title for word in _DEFENSE_WORDS):
            score += 25
        elif any(word in lower_title for word in _OFFENSE_WORDS):
            score -= 30

    if not player.is_team_entity:
        if not player.is_rookie:
            # A veteran's college name in the title is almost always footage
            # from years before he was even drafted — worse than useless on
            # a draft-night reveal for a player with an NFL career to show.
            if player.college and player.college.lower() in lower_title:
                score -= 45
            elif "college" in lower_title or "ncaa" in lower_title:
                score -= 20

        if player.team and player.team != "FA":
            # A different team's name in the title — and not the player's own
            # — is usually a trade-rumor fan edit or leftover title from
            # before a trade, not real footage of him on his current team.
            own_words = {w for w in player.display_team.lower().split() if len(w) > 3}
            for abbr, full in TEAM_NAMES.items():
                if abbr == player.team:
                    continue
                other_words = [w for w in full.lower().split() if len(w) > 3 and w not in own_words]
                if other_words and any(w in lower_title for w in other_words):
                    score -= 35
                    break

    if lower_channel in _OFFICIAL or "highlight" in lower_channel:
        score += 20
    if player.team and player.display_team.lower() in lower_channel:
        score += 18

    # Recency: last season's tape is what everyone wants to argue about.
    if "2025" in title:
        score += 22
    elif any(year in title for year in ("2018", "2019", "2020", "2021", "2022")):
        score -= 12

    if not player.is_team_entity and not player.is_rookie and player.years_exp:
        # A veteran's rough NFL debut season — footage dated well before it is
        # pre-draft (college or earlier) even when the title never says so,
        # e.g. "2018-19 Season Highlights" for a QB who debuted in 2020.
        debut_year = CURRENT_SEASON_YEAR - player.years_exp
        if any(str(y) in title for y in range(debut_year - 8, debut_year)):
            score -= 30

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


def build_queries(player: Player) -> list[str]:
    """Ranked query variants — the primary one first. The fallbacks trade
    precision for reach: A-list stars get so much official broadcast coverage
    that their top results are almost entirely NFL/team channels, which
    aggressively block embedding on that footage. A plain "<name> highlights"
    or "career highlights compilation" query surfaces independent compilation
    channels the templated query doesn't, and some of those aren't blocked."""
    if player.is_dst:
        return [f"{player.display_team} defense highlights 2025",
                f"{player.display_team} defense highlights"]
    if player.position == "TQB":
        return [f"{player.display_team} quarterback highlights 2025",
                f"{player.display_team} quarterback highlights"]
    if player.position == "K":
        return [f"{player.name} longest field goals highlights"]
    if player.is_rookie:
        college = player.college or ""
        return [f"{player.name} {college} highlights".strip(), f"{player.name} highlights"]
    if player.position in DEFENSIVE_POSITIONS:
        # "Highlights" alone tends to surface offensive plays against them.
        return [f"{player.name} sacks tackles highlights 2025", f"{player.name} highlights"]
    # The team name in the query steers a veteran's search away from his old
    # college tape, which a bare name query surfaces far more than you'd
    # expect (it's evergreen, well-tagged content from his draft year).
    return [
        f"{player.name} 2025 highlights",
        f"{player.name} {player.display_team} highlights",
        f"{player.name} highlights",
        f"{player.name} career highlights compilation",
    ]


def build_query(player: Player) -> str:
    """The single most-targeted query. Kept for callers that only want one."""
    return build_queries(player)[0]


async def _search(query: str, client: httpx.AsyncClient, player: Player) -> list[VideoCandidate]:
    resp = await client.get(SEARCH_URL.format(q=quote_plus(query)))
    resp.raise_for_status()
    match = _INITIAL_DATA.search(resp.text)
    if not match:
        log.warning("No ytInitialData for %r (page structure changed?)", query)
        return []

    raw = _extract(json.loads(match.group(1)))
    candidates: list[VideoCandidate] = []
    for video_id, title, channel, duration in raw:
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
    return candidates


async def find_videos(
    player: Player,
    client: httpx.AsyncClient | None = None,
    limit: int = 3,
    broaden: bool = False,
) -> list[dict]:
    """Return up to `limit` ranked clips. Never raises — an empty list means
    the display falls back to the stat card.

    `broaden` runs every query variant instead of just the primary one. It's
    slower (several searches instead of one) so live picks skip it, but it's
    what rescues A-list players whose primary query is entirely embed-blocked
    official footage — see `build_queries`."""
    queries = build_queries(player) if broaden else build_queries(player)[:1]
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True)

    try:
        seen: set[str] = set()
        candidates: list[VideoCandidate] = []
        for query in queries:
            try:
                found = await _search(query, client, player)
            except Exception as exc:
                log.warning("YouTube lookup failed for %s (%r): %s", player.name, query, exc)
                continue
            for c in found:
                if c.video_id in seen:
                    continue
                seen.add(c.video_id)
                candidates.append(c)

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
    players: list[Player], delay: float = 1.0, limit: int = 3, broaden: bool = False
) -> dict[str, list[dict]]:
    """Sequential lookups with a delay, for the prewarm pass."""
    results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(
        timeout=20, headers=_HEADERS, follow_redirects=True
    ) as client:
        for index, player in enumerate(players, 1):
            results[player.player_id] = await find_videos(player, client, limit, broaden=broaden)
            print(
                f"  [{index}/{len(players)}] {player.name:<28} "
                f"{len(results[player.player_id])} clip(s)",
                flush=True,
            )
            if index < len(players):
                await asyncio.sleep(delay)
    return results
