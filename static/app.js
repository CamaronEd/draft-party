/* Draft Party display.
 *
 * Owns three things the backend can't:
 *   1. A queue, so an autodraft burst doesn't leave the TV minutes behind.
 *   2. The embed fallback chain — NFL clips are often embed-restricted, and the
 *      IFrame API reports that as onError 101/150.
 *   3. Autoplay-with-audio, which browsers only grant after a user gesture.
 */

const CARD_MS = 3500;      // how long the stat card holds before the clip
const VIDEO_MS = 16000;    // clip runtime
const OUTRO_MS = 600;
const VIDEO_START = 4;     // skip past intro cards on highlight reels

// When picks pile up, shorten the moment rather than falling behind.
const COMPRESS_AT = 3;
const CARD_ONLY_AT = 6;
// Hard cap. Past this the display is drifting away from the actual draft, so
// drop the oldest waiting picks — the room cares about the pick that just
// happened, and skipped ones still show up on the idle recap board.
const MAX_QUEUE = 8;

const el = (id) => document.getElementById(id);
const dom = {
  splash: el("splash"), startBtn: el("start-btn"),
  idle: el("idle"), idleRecent: el("idle-recent"), idleStatus: el("idle-status"),
  stage: el("stage"), pickLabel: el("pick-label"), pickTeam: el("pick-team"),
  card: el("card"), photo: el("card-photo"), logo: el("card-logo"),
  pos: el("card-pos"), name: el("card-name"), meta: el("card-meta"),
  stats: el("card-stats"), videoWrap: el("video-wrap"),
  videoName: el("video-name"), videoSub: el("video-sub"),
  queueBadge: el("queue-badge"), toast: el("toast"),
};

const queue = [];
const recent = [];
let ytReady = false;
let player = null;
let playing = false;
let started = false;
let muted = false;
let lastEvent = null;
let advance = null;      // resolves the current moment early (skip / video end)

/* ---------- YouTube ---------- */

window.onYouTubeIframeAPIReady = () => {
  player = new YT.Player("player", {
    height: "100%",
    width: "100%",
    playerVars: {
      autoplay: 1, controls: 0, modestbranding: 1, rel: 0,
      iv_load_policy: 3, fs: 0, disablekb: 1, playsinline: 1,
      // Captions default on for some accounts and cover the name overlay.
      cc_load_policy: 0, cc_lang_pref: "none",
    },
    events: {
      onReady: () => { ytReady = true; },
      onError: (e) => handleVideoError(e.data, videoGeneration),
      onStateChange: (e) => {
        if (e.data === YT.PlayerState.PLAYING) killCaptions();
        if (e.data === YT.PlayerState.ENDED) finishVideo();
      },
    },
  });
};

let videoCandidates = [];
let candidateIndex = 0;
let videoTimer = null;
// Bumped every moment. A stale error arriving from the previous pick's clip
// would otherwise skip candidates on the current one.
let videoGeneration = 0;

function handleVideoError(code, generation) {
  if (generation !== videoGeneration) return;
  // 101 / 150 mean the owner disabled embedding. 100 means removed.
  console.warn("YouTube error", code, "candidate", candidateIndex);
  candidateIndex += 1;
  if (candidateIndex < videoCandidates.length) {
    playCandidate();
  } else {
    toast("No playable clip");
    finishVideo();
  }
}

function playCandidate() {
  const video = videoCandidates[candidateIndex];
  if (!video) { finishVideo(); return; }
  dom.videoSub.textContent = video.channel || "";
  try {
    player.loadVideoById({ videoId: video.video_id, startSeconds: VIDEO_START });
    if (muted) player.mute(); else player.unMute();
    player.playVideo();
  } catch (err) {
    console.warn("loadVideoById failed", err);
    handleVideoError(-1, videoGeneration);
  }
}

/** Resolves once the IFrame API is live, so a pick early in the draft doesn't
 *  silently lose its clip while the API is still loading. */
function waitForYt(timeoutMs = 6000) {
  if (ytReady) return Promise.resolve(true);
  return new Promise((resolve) => {
    const started = Date.now();
    const tick = setInterval(() => {
      if (ytReady || Date.now() - started > timeoutMs) {
        clearInterval(tick);
        resolve(ytReady);
      }
    }, 100);
  });
}

/** cc_load_policy alone doesn't stick when the viewer's account defaults
 *  captions on, and subtitles sit right on top of the name overlay. */
function killCaptions() {
  ["captions", "cc"].forEach((mod) => {
    try { player.unloadModule(mod); } catch (_) {}
  });
}

function finishVideo() {
  clearTimeout(videoTimer);
  if (advance) { const done = advance; advance = null; done(); }
}

/* ---------- Moment ---------- */

function renderCard(ev) {
  const p = ev.player;
  document.documentElement.style.setProperty("--accent", p.color || "#2d9dff");
  dom.stage.dataset.tier = p.tier || "depth";

  dom.pickLabel.textContent = ev.label;
  dom.pickTeam.textContent = ev.team_name || "";

  dom.photo.onerror = () => {
    if (p.headshot_fallback && dom.photo.src !== p.headshot_fallback) {
      dom.photo.src = p.headshot_fallback;
    } else if (p.team_logo) {
      dom.photo.src = p.team_logo;
    }
  };
  dom.photo.src = p.headshot || p.team_logo || "";
  dom.logo.src = p.team_logo || "";
  dom.logo.style.display = p.team_logo && !p.headshot?.includes("teamlogos") ? "" : "none";

  dom.pos.textContent = p.position || "";
  dom.name.textContent = p.name || "";

  const bits = [p.team_full || p.team, p.college, p.is_rookie ? "Rookie" : null]
    .filter(Boolean);
  dom.meta.textContent = bits.join(" · ");

  dom.stats.innerHTML = "";
  (p.stat_line || []).forEach((s, i) => {
    const node = document.createElement("div");
    node.className = "stat" + (s.highlight ? " hl" : "");
    node.style.animationDelay = `${0.25 + i * 0.09}s`;
    node.innerHTML =
      `<div class="stat-value"></div><div class="stat-label"></div>`;
    node.querySelector(".stat-value").textContent = s.value;
    node.querySelector(".stat-label").textContent = s.label;
    dom.stats.appendChild(node);
  });

  dom.videoName.textContent = p.name || "";
}

function wait(ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { advance = null; resolve(); }, ms);
    advance = () => { clearTimeout(timer); resolve(); };
  });
}

async function playMoment(ev) {
  playing = true;
  lastEvent = ev;
  updateQueueBadge();

  const backlog = queue.length;
  const cardOnly = backlog >= CARD_ONLY_AT || !(ev.player.videos || []).length;
  // Compression only shortens the clip — it's the expensive part of the
  // moment (16s vs. the card's 3.5s). Shrinking the card too used to make
  // picks flash by too fast to even read the name during a burst, without
  // meaningfully speeding up how fast the queue drains.
  const speed = backlog >= COMPRESS_AT ? 0.55 : 1;

  dom.idle.classList.add("hidden");
  dom.stage.classList.remove("hidden");
  dom.videoWrap.classList.remove("active");
  dom.card.classList.remove("out");

  renderCard(ev);
  await wait(CARD_MS);

  if (!cardOnly) {
    videoCandidates = ev.player.videos || [];
    candidateIndex = 0;
    videoGeneration += 1;
    dom.card.classList.add("out");
    await wait(300);
    // The very first pick can land before the IFrame API has finished loading.
    await waitForYt();
    dom.videoWrap.classList.add("active");
    playCandidate();

    await new Promise((resolve) => {
      advance = resolve;
      videoTimer = setTimeout(() => { advance = null; resolve(); }, VIDEO_MS * speed);
    });

    try { player.stopVideo(); } catch (_) {}
    dom.videoWrap.classList.remove("active");
  }

  await wait(OUTRO_MS);
  dom.stage.classList.add("hidden");
  playing = false;
  pump();
}

function pump() {
  if (playing || !started) return;
  const next = queue.shift();
  if (next) {
    playMoment(next);
  } else {
    showIdle();
  }
  updateQueueBadge();
}

function updateQueueBadge() {
  if (queue.length > 0) {
    dom.queueBadge.textContent = `+${queue.length} queued`;
    dom.queueBadge.classList.remove("hidden");
  } else {
    dom.queueBadge.classList.add("hidden");
  }
}

/* ---------- Idle ---------- */

function showIdle() {
  dom.stage.classList.add("hidden");
  dom.idle.classList.remove("hidden");
  dom.idleRecent.innerHTML = "";
  recent.slice(-6).reverse().forEach((ev) => {
    const row = document.createElement("div");
    row.className = "idle-row";
    row.style.borderLeftColor = ev.player.color || "#2d9dff";
    row.innerHTML = `<span class="r-pick"></span><span class="r-name"></span><span class="r-team"></span>`;
    row.querySelector(".r-pick").textContent = ev.label;
    row.querySelector(".r-name").textContent = ev.player.name;
    row.querySelector(".r-team").textContent =
      `${ev.player.position} · ${ev.team_name || ""}`;
    dom.idleRecent.appendChild(row);
  });
}

function toast(message) {
  dom.toast.textContent = message;
  dom.toast.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => dom.toast.classList.add("hidden"), 2200);
}

/* ---------- Events ---------- */

function connect() {
  const source = new EventSource("/events");

  source.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "hello") {
      (data.recent || []).forEach((ev) => recent.push(ev));
      dom.idleStatus.textContent = "Connected · waiting for the next pick…";
      if (!playing) showIdle();
      return;
    }
    if (data.type === "pick") {
      recent.push(data);
      queue.push(data);
      if (queue.length > MAX_QUEUE) {
        const dropped = queue.splice(0, queue.length - MAX_QUEUE);
        console.warn("Queue over cap — skipped", dropped.length, "pick(s)");
        toast(`Caught up · skipped ${dropped.length}`);
      }
      updateQueueBadge();
      pump();
    }
  };

  source.onerror = () => {
    dom.idleStatus.textContent = "Reconnecting…";
  };
}

/* ---------- Start ---------- */

dom.startBtn.addEventListener("click", async () => {
  started = true;
  dom.splash.classList.add("hidden");
  // The click is the gesture that lets every later clip play with sound.
  try { await document.documentElement.requestFullscreen(); } catch (_) {}
  showIdle();
  pump();
});

// Safety net. Nothing needs pressing, but a dud clip shouldn't hold the room.
document.addEventListener("keydown", (e) => {
  if (e.key === "m" || e.key === "M") {
    muted = !muted;
    try { muted ? player.mute() : player.unMute(); } catch (_) {}
    toast(muted ? "Muted" : "Unmuted");
  } else if (e.key === "ArrowRight") {
    toast("Skipped");
    finishVideo();
  } else if (e.key === "r" || e.key === "R") {
    if (lastEvent && !playing) { queue.unshift(lastEvent); toast("Replay"); pump(); }
  }
});

connect();
