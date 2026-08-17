"""Verifies that a YouTube video will actually play in an embed.

Roughly half of the clips a search returns are embed-blocked (error 150) — the
restriction is per-video, applied to copyright-claimed NFL footage, so neither
the channel nor any metadata field predicts it. The only reliable test is to
load the clip in a real embedded player and see what happens.

That is too slow for draft day, so it runs at prewarm time instead and only
verified-playable clips get cached.

Needs Playwright plus a Chromium-family browser. If either is missing,
verification is skipped and unverified candidates are used as-is.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import logging
import threading

log = logging.getLogger(__name__)

# The page must be served over http from a real origin — YouTube rejects
# embeds created via set_content or file:// with a generic error.
VERIFY_PAGE = """<!doctype html><html><body style="margin:0;background:#000">
<div id="p"></div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
let player, ready = false, pending = null;
window.onYouTubeIframeAPIReady = () => {
  player = new YT.Player('p', {
    height: '360', width: '640',
    playerVars: {autoplay: 1, mute: 1, controls: 0, playsinline: 1},
    events: {
      onReady: () => { ready = true; },
      onError: (e) => { if (pending) pending('err' + e.data); },
      onStateChange: (e) => { if (pending && e.data === 1) pending('playing'); }
    }
  });
};
window.ytReady = () => ready;

// Deliberately strict. A blocked clip often reports BUFFERING for a moment
// before error 150 arrives, so resolving on the first sign of life produces
// false positives. Require PLAYING *and* a clean settle window with no error.
window.check = (id, ms) => new Promise((resolve) => {
  let errored = null, played = false;
  pending = (r) => { if (r.startsWith('err')) errored = r; else played = true; };
  try { player.loadVideoById({videoId: id, startSeconds: 2}); player.playVideo(); }
  catch (e) { resolve('errload'); return; }
  setTimeout(() => {
    let state = -99;
    try { state = player.getPlayerState(); } catch (e) {}
    if (errored) resolve(errored);
    else if (played && state === 1) resolve('ok');
    else resolve('state' + state);
  }, ms);
});
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = VERIFY_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@contextlib.contextmanager
def _serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()


def available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


class EmbedVerifier:
    """Checks video IDs in real embedded players, a few tabs at a time."""

    def __init__(self, concurrency: int = 4, timeout_ms: int = 8000) -> None:
        self.concurrency = concurrency
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._pages: asyncio.Queue | None = None
        self._url = None
        self._ctx = None

    async def __aenter__(self) -> "EmbedVerifier":
        from playwright.async_api import async_playwright

        self._ctx = _serve()
        self._url = self._ctx.__enter__()

        self._pw = await async_playwright().start()
        for channel in ("msedge", "chrome", None):
            try:
                self._browser = await self._pw.chromium.launch(
                    channel=channel, headless=True
                )
                break
            except Exception:
                continue
        if self._browser is None:
            raise RuntimeError("No Chromium-family browser available")

        self._pages = asyncio.Queue()
        for _ in range(self.concurrency):
            page = await self._browser.new_page(
                viewport={"width": 640, "height": 360}
            )
            await page.goto(self._url, wait_until="domcontentloaded")
            for _ in range(40):
                if await page.evaluate("window.ytReady && window.ytReady()"):
                    break
                await page.wait_for_timeout(250)
            await self._pages.put(page)
        return self

    async def __aexit__(self, *exc) -> None:
        with contextlib.suppress(Exception):
            await self._browser.close()
        with contextlib.suppress(Exception):
            await self._pw.stop()
        with contextlib.suppress(Exception):
            self._ctx.__exit__(None, None, None)

    async def check(self, video_id: str) -> bool:
        page = await self._pages.get()
        try:
            result = await page.evaluate(
                "([id, ms]) => window.check(id, ms)", [video_id, self.timeout_ms]
            )
            return result == "ok"
        except Exception as exc:
            log.debug("verify failed for %s: %s", video_id, exc)
            return False
        finally:
            await self._pages.put(page)

    async def first_playable(
        self, candidates: list[dict], keep: int = 3
    ) -> list[dict]:
        """Return up to `keep` candidates that actually play, best-scoring first.

        Stops as soon as it has enough, so well-covered players cost 2-3 checks
        rather than the full list.
        """
        good: list[dict] = []
        for candidate in candidates:
            if len(good) >= keep:
                break
            if await self.check(candidate["video_id"]):
                candidate["verified"] = True
                good.append(candidate)
        return good


async def verify_batch(
    packets: dict[str, list[dict]], keep: int = 3, concurrency: int = 4
) -> dict[str, list[dict]]:
    """Filter every player's candidate list down to verified-playable clips."""
    if not available():
        log.warning("Playwright not installed — skipping embed verification")
        return packets

    out: dict[str, list[dict]] = {}
    async with EmbedVerifier(concurrency=concurrency) as verifier:
        items = list(packets.items())

        async def worker(player_id: str, candidates: list[dict]) -> None:
            out[player_id] = await verifier.first_playable(candidates, keep)

        # Bounded fan-out; each worker still serializes on a free tab.
        semaphore = asyncio.Semaphore(concurrency)

        async def guarded(pid, cands):
            async with semaphore:
                await worker(pid, cands)

        await asyncio.gather(*(guarded(p, c) for p, c in items))
    return out
