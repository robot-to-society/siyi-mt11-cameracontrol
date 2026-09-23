// Live video panel: WHEP playback, click-to-track, tracking box overlay (SSE).
import { WhepPlayer } from "./whep.js";
import { contentRect, previewBox, toNormalized, trackToDisplay } from "./video_geometry.js";

const WHEP_URL = "/api/video/whep"; // proxied to MediaMTX by the app
const BOX_MIN = 32;
const BOX_MAX = 600;
const BOX_DEFAULT = 150;
const BOX_STORAGE_KEY = "mt11.trackBoxPx";
const FLASH_MS = 400;
const RESULT_SHOW_S = 5;

const TRACK_LABELS = {
  tracking: ["TRACKING", "ok"],
  tracking_any: ["TRACKING", "ok"],
  lost_temporarily: ["LOSING", "warn"],
  lost: ["LOST", "bad"],
  cancelled: ["CANCELLED", ""],
};
const CONN_LABELS = {
  connected: ["LIVE", "ok"],
  connecting: ["CONNECTING", "warn"],
  new: ["CONNECTING", "warn"],
  disconnected: ["RECONNECTING", "warn"],
  failed: ["FAILED", "bad"],
  closed: ["CLOSED", "bad"],
};
const TRACK_COLORS = { ok: "#43d39e", warn: "#ffc34d", bad: "#ff4d5c", "": "#8f98ab" };

const wrap = document.getElementById("video-wrap");
const video = document.getElementById("video-el");
const canvas = document.getElementById("video-overlay");
const connBadge = document.getElementById("video-conn-badge");
const trackBadge = document.getElementById("video-track-badge");
const latencyText = document.getElementById("video-latency");
const boxInput = document.getElementById("video-box-px");
const cancelBtn = document.getElementById("video-cancel-btn");
const msgText = document.getElementById("video-msg");

let snapshot = null; // latest /api/ai/events payload
let hover = null; // { nx, ny }
let flash = null; // { nx, ny, until }
let lastClickAt = 0; // performance.now() of the last track request

function errorDetail(body, status) {
  const d = body?.detail;
  if (Array.isArray(d)) return d.map((e) => e.msg ?? JSON.stringify(e)).join("; ");
  return d ?? `HTTP ${status}`;
}

function setBadge(el, [label, cls]) {
  el.textContent = label;
  el.className = `video-badge ${cls}`;
}

function setMessage(text, isError = false) {
  msgText.textContent = text;
  msgText.classList.toggle("video-msg-error", isError);
}

// ── box size (per-viewer preference) ────────────────────────────
function loadBoxPx() {
  try {
    const v = parseInt(localStorage.getItem(BOX_STORAGE_KEY) ?? "", 10);
    return v >= BOX_MIN && v <= BOX_MAX ? v : BOX_DEFAULT;
  } catch {
    return BOX_DEFAULT;
  }
}

function currentBoxPx() {
  const v = parseInt(boxInput.value, 10);
  return Number.isFinite(v) ? Math.max(BOX_MIN, Math.min(BOX_MAX, v)) : BOX_DEFAULT;
}

boxInput.value = String(loadBoxPx());
boxInput.addEventListener("change", () => {
  boxInput.value = String(currentBoxPx());
  try {
    localStorage.setItem(BOX_STORAGE_KEY, boxInput.value);
  } catch {
    /* storage unavailable: keep in-page value only */
  }
});

// ── geometry helpers ────────────────────────────────────────────
function streamSize() {
  const s = snapshot?.stream;
  if (s?.width > 1 && s?.height > 1) return [s.width, s.height];
  return [video.videoWidth, video.videoHeight];
}

function pictureRect() {
  const vw = video.videoWidth || streamSize()[0];
  const vh = video.videoHeight || streamSize()[1];
  return contentRect(wrap.clientWidth, wrap.clientHeight, vw, vh);
}

function localPoint(ev) {
  const r = canvas.getBoundingClientRect();
  return [ev.clientX - r.left, ev.clientY - r.top];
}

function trackingAllowed() {
  return !snapshot || snapshot.video_mode === "rgb";
}

// ── drawing ─────────────────────────────────────────────────────
function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(wrap.clientWidth * dpr);
  canvas.height = Math.round(wrap.clientHeight * dpr);
  canvas.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
}

function strokeRect(ctx, r, color, dashed = false, width = 2) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dashed ? [6, 4] : []);
  ctx.strokeRect(r.x, r.y, r.w, r.h);
  ctx.restore();
}

function draw() {
  requestAnimationFrame(draw);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, wrap.clientWidth, wrap.clientHeight);
  const rect = pictureRect();
  if (!rect) return;
  const [sw, sh] = streamSize();

  const track = snapshot?.track;
  if (track) {
    const cls = (TRACK_LABELS[track.status] ?? ["", ""])[1];
    strokeRect(ctx, trackToDisplay(track, rect), TRACK_COLORS[cls], false, 3);
  }
  if (hover && trackingAllowed()) {
    const b = previewBox(hover.nx, hover.ny, rect, sw, sh, currentBoxPx());
    if (b) strokeRect(ctx, b, "rgba(255,255,255,0.8)", true);
  }
  if (flash && performance.now() < flash.until) {
    const b = previewBox(flash.nx, flash.ny, rect, sw, sh, currentBoxPx());
    if (b) strokeRect(ctx, b, "#ffc34d", false, 3);
  }
}

// ── interaction ─────────────────────────────────────────────────
canvas.addEventListener("mousemove", (ev) => {
  hover = toNormalized(...localPoint(ev), pictureRect());
});
canvas.addEventListener("mouseleave", () => { hover = null; });

canvas.addEventListener("click", async (ev) => {
  if (!trackingAllowed()) {
    setMessage("AIトラッキングはRGBモードでのみ使えます", true);
    return;
  }
  const p = toNormalized(...localPoint(ev), pictureRect());
  if (!p) return; // clicked on the black bars
  flash = { ...p, until: performance.now() + FLASH_MS };
  lastClickAt = performance.now();
  try {
    const res = await fetch("/api/ai/track-point", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x: p.nx, y: p.ny, box_px: currentBoxPx() }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(errorDetail(body, res.status));
    const b = body.box;
    setMessage(`追跡指示を送信: [${b.lx},${b.ly}]-[${b.rx},${b.ry}]（${body.stream.width}×${body.stream.height}）`);
  } catch (e) {
    setMessage(`追跡開始に失敗: ${e.message}`, true);
  }
});

cancelBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/api/ai/cancel", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setMessage("追跡を解除しました");
  } catch (e) {
    setMessage(`解除に失敗: ${e.message}`, true);
  }
});

// ── live data ───────────────────────────────────────────────────
function onSnapshot(data) {
  snapshot = data;
  const track = data.track;
  setBadge(trackBadge, track ? (TRACK_LABELS[track.status] ?? [track.status.toUpperCase(), ""]) : ["NO TARGET", ""]);
  const age = data.ai_select_age_s;
  const sinceClickS = (performance.now() - lastClickAt) / 1000;
  // only ACKs that arrived after our latest click, and only for a few seconds
  const fresh = age !== null && age < RESULT_SHOW_S && age <= sinceClickS;
  if (fresh && data.ai_select_result && data.ai_select_result !== "ok") {
    setMessage(`カメラの応答: ${data.ai_select_result}`, true);
  }
  canvas.classList.toggle("disabled", data.video_mode !== "rgb");
  const active = ["tracking", "tracking_any", "lost_temporarily"].includes(track?.status);
  window.dispatchEvent(new CustomEvent("ai-tracking-state", { detail: { active } }));
}

function openEvents() {
  const es = new EventSource("/api/ai/events");
  es.onmessage = (ev) => {
    try {
      onSnapshot(JSON.parse(ev.data));
    } catch {
      /* ignore malformed event */
    }
  };
  // EventSource reconnects by itself; nothing else to do on error
}

const player = new WhepPlayer(WHEP_URL, video, (state) => {
  const label = CONN_LABELS[state] ?? (state.startsWith("error") ? ["NO VIDEO", "bad"] : [state.toUpperCase(), ""]);
  setBadge(connBadge, label);
  if (state.startsWith("error")) setMessage(`映像に接続できません（${state}）。MediaMTXが起動しているか確認してください`, true);
});

setInterval(async () => {
  try {
    const ms = await player.latencyMs();
    latencyText.textContent = ms === null ? "-" : `受信遅延 ≈ ${ms} ms`;
  } catch {
    latencyText.textContent = "-";
  }
}, 1000);

new ResizeObserver(resizeCanvas).observe(wrap);
resizeCanvas();
requestAnimationFrame(draw);
openEvents();
player.start();
