# Fantasy Draft Party

A second screen for your draft. It watches your live ESPN or Sleeper draft and,
the moment anyone picks, slams the player's highlight reel onto the TV with
their photo and 2025 stats. Fully automatic once started.

```
  pick lands  ->  PICK 1.04 banner  ->  headshot + stat card  ->  highlight reel
                                                                  (audio on)
```

## Setup

Requires Python 3.10+.

```bash
pip install -r requirements.txt
pip install playwright        # optional but strongly recommended, see below
```

Copy `config.example.json` to `config.json` and fill in your league.
`config.json` is gitignored — it can hold ESPN login cookies, so it's never
committed. See `directions.txt` for a fuller step-by-step walkthrough.

### Sleeper

No auth needed. Put your draft ID (or paste the whole draft URL) in
`config.sleeper.draft_id`, then set `"source": "sleeper"`.

### ESPN

Set `config.espn.league_id` and `season`, then `"source": "espn"`.
Public leagues need nothing else.

**Private leagues** need two cookies:

1. Log into `fantasy.espn.com` in Chrome.
2. `F12` → Application → Storage → Cookies → `https://fantasy.espn.com`
3. Copy `espn_s2` (long) and `SWID` (in curly braces) into `config.json`.

These expire periodically. If ESPN polling starts failing with a 401, re-grab them.

## Run it

**The night before — this is the important step:**

```bash
python prewarm.py --top 300
```

Then on draft day:

```bash
python app.py
```

Open `http://localhost:8000` on the TV and click **TAP TO START**. That single
click is what lets every later clip play with sound — browsers block autoplay
audio without one gesture. After that, don't touch anything.

Other machines on your network can point at `http://<your-ip>:8000`.

## Why prewarm matters

Two reasons, and the second one is the real one:

1. A cold highlight lookup takes 1–2s, which is dead air on a TV.
2. **About two thirds of YouTube search results are embed-blocked.** The clip
   looks fine in search and then plays as a black screen — YouTube reports it
   as player error 150, and nothing in the video's metadata predicts it.

`prewarm.py` searches 10 clips per player, loads each one in a real embedded
player, and keeps only the ones that actually play. Measured coverage on the
top 12 players by ADP: **11/12 with a verified playable clip (92%)**, versus
roughly a third of raw search hits.

Verification needs `playwright` plus Edge or Chrome (already on most Windows
machines — no browser download). Without it, prewarm still works but caches
unverified clips, and more picks will fall back to the stat card.

```bash
python prewarm.py --top 300              # full run, do this once
python prewarm.py --top 300 --missing    # fill gaps from an earlier run
python prewarm.py --top 100 --no-verify  # fast and less reliable
```

Players outside the prewarmed set still work — they're looked up live and use
the display's fallback chain instead.

## Testing without a draft

You cannot debug this during the real thing, so there's a replay mode. It walks
a *completed* draft pick by pick:

```bash
python app.py --source demo                 # 10s between picks
python app.py --source demo --interval 1    # burst test: does the queue keep up?
```

Point `config.demo` at any finished draft. It defaults to a real completed 2025
ESPN league.

To confirm your own league config resolves before draft day:

```bash
python app.py --replay      # replays picks that already exist
```

## How it holds up live

- **Pick queue.** Autodraft bursts fire several picks in seconds while each
  hype moment runs ~20s. Picks queue and play in order; past 3 deep the moment
  compresses, past 6 it drops to card-only, and past 8 it discards the oldest
  waiting picks so the screen never drifts away from the live draft. Skipped
  picks still appear on the idle recap board. Verified with
  `--source demo --interval 1`: queue holds at 8 instead of running to 69.
- **Fallback chain.** Three ranked clips per player. On player error 101/150
  it advances to the next, then to the stat card. Never a black screen.
- **Reconnects.** Poll failures back off and retry rather than killing the run.
- **Joins mid-draft cleanly.** Existing picks are marked seen at startup, so
  you don't get 40 clips at once. Override with `--replay`.

## Keyboard (nothing needs pressing)

| Key | Does |
|-----|------|
| `M` | mute / unmute |
| `→` | skip the current clip |
| `R` | replay the last pick |

## Layout

```
app.py              FastAPI server, SSE stream, poll loop
prewarm.py          pre-draft cache warmer
sources/            espn.py, sleeper.py, demo.py  -> a common Pick
enrich/             players.py, stats.py, youtube.py, verify.py, cache.py
static/             the display
data/               cached player DB, stats, warmed clips
```

## Notes on the data

- ESPN's `mDraftDetail` **does** work during a live draft in the current
  season, despite common claims otherwise. Unmade picks read `playerId: -1`.
- Sleeper's `espn_id` is null for over half of players, so ESPN picks are
  matched to Sleeper by normalized name + position, with pro team as a
  tiebreaker. Measured 180/180 on a real 12-team IDP league.
- Sleeper's `rank_ppr` is an **overall** rank; `pos_rank_ppr` is the one that
  makes "WR4" say WR4.
- IDP and ESPN's Team QB slot are both handled.
