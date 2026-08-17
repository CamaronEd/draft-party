"""Pre-draft cache warmer. Run this the night before your draft.

Resolving a highlight clip live takes 1-2s, which is dead air on a TV. This
walks the top N players by ADP and caches their clips to disk so draft-day
reveals are instant.

    python prewarm.py              # top 300
    python prewarm.py --top 500
    python prewarm.py --missing    # only fill gaps from a previous run
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from enrich import verify
from enrich.cache import HypeCache
from enrich.players import PlayerDB
from enrich.stats import StatsDB
from enrich.youtube import find_videos_rate_limited

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Warm the hype cache")
    parser.add_argument("--top", type=int, default=300, help="players by ADP")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between lookups")
    parser.add_argument("--missing", action="store_true", help="only fetch uncached players")
    parser.add_argument("--refresh", action="store_true", help="re-download player data")
    parser.add_argument("--candidates", type=int, default=10,
                        help="clips to search per player before verifying")
    parser.add_argument("--keep", type=int, default=3,
                        help="verified clips to cache per player")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip embed verification (much faster, less reliable)")
    parser.add_argument("--verify-jobs", type=int, default=4,
                        help="parallel browser tabs used for verification")
    args = parser.parse_args()

    print("Loading player data...")
    players = PlayerDB()
    stats = StatsDB()
    await asyncio.gather(
        players.load(force_refresh=args.refresh),
        stats.load(force_refresh=args.refresh),
    )

    hype = HypeCache(players, stats)
    hype.load()

    targets = players.top_by_adp(args.top)
    if args.missing:
        targets = [p for p in targets if not hype.has(p.player_id)]

    if not targets:
        print("Nothing to do — cache already covers the top", args.top)
        return

    print(f"\nStep 1/2 — searching clips for {len(targets)} players "
          f"(~{len(targets) * args.delay / 60:.1f} min)\n")

    try:
        results = await find_videos_rate_limited(
            targets, delay=args.delay, limit=args.candidates
        )
    except KeyboardInterrupt:
        print("\nInterrupted — saving what we have...")
        results = {}

    # Roughly two thirds of search results are embed-blocked, and that only
    # shows up as a black screen at play time. Verification is what makes
    # draft-day playback reliable.
    if results and not args.no_verify:
        if verify.available():
            found = sum(len(v) for v in results.values())
            print(f"\nStep 2/2 — verifying {found} clips actually play "
                  f"(this is the slow part; grab a drink)\n")
            try:
                results = await verify.verify_batch(
                    results, keep=args.keep, concurrency=args.verify_jobs
                )
            except Exception as exc:
                print(f"  Verification unavailable ({exc}) — caching unverified clips.")
                results = {k: v[: args.keep] for k, v in results.items()}
        else:
            print("\nPlaywright not installed — skipping verification.")
            print("  For reliable playback: pip install playwright\n")
            results = {k: v[: args.keep] for k, v in results.items()}
    else:
        results = {k: v[: args.keep] for k, v in results.items()}

    for player in targets:
        videos = results.get(player.player_id)
        if videos is not None:
            hype.set_videos(player.player_id, player.name, videos)

    hype.save()

    empty = [p.name for p in targets if not results.get(p.player_id)]
    covered = len(targets) - len(empty)
    print(f"\nDone. {hype.size} packets cached — "
          f"{covered}/{len(targets)} players have a playable clip "
          f"({covered / max(len(targets), 1) * 100:.0f}%).")
    if empty:
        print(f"\n{len(empty)} players found no playable clip "
              f"(they'll show the stat card, which is a fine fallback):")
        for name in empty[:25]:
            print(f"  - {name}")
        if len(empty) > 25:
            print(f"  ... and {len(empty) - 25} more")


if __name__ == "__main__":
    asyncio.run(main())
