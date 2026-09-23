// Live video panel: WHEP playback, click-to-track, tracking box overlay (SSE).
import { WhepPlayer } from "./whep.js";
import {
  contentRect,
  detectionAt,
  detectionToDisplay,
  previewBox,
  toNormalized,
  trackToDisplay,
} from "./video_geometry.js";
import { setupDisplayModes } from "./video_display.js";

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
const DETECTION_COLOR = "rgba(77, 208, 255, 0.7)";
const DETECTION_HOVER_COLOR = "#4dd0ff";

const wrap = document.getElementById("video-wrap");
const video = document.getElementById("video-el");
const canvas = document.getElementById("video-overlay");
const connBadge = document.getElementById("video-conn-badge");
const trackBadge = document.getElementById("video-track-badge");
const latencyText = document.getElementById("video-latency");
const boxInput = document.getElementById("video-box-px");
const cancelBtn = document.getElementById("video-cancel-btn");
const msgText = document.getElementById("video-msg");
const section = document.getElementById("video-section");

// The wrap may live in a Document PiP window; draw/size with that window's clock and DPR.
function hostWindow() {
  return wrap.ownerDocument.defaultView ?? window;
}

let snapshot = null; // latest /api/ai/events payload
let hover = null; // { nx, ny }
let flash = null; // { nx, ny, until }
let lastClickAt = 0; // performance.now() of the last track request
let ctrlDown = false; // Ctrl+click selects a camera-detected object

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
  const dpr = hostWindow().devicePixelRatio || 1;
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

function drawDetections(ctx, rect) {
  const detections = snapshot?.detections ?? [];
  const target = hover ? detectionAt(detections, hover.nx, hover.ny) : null;
  for (const d of detections) {
    const r = detectionToDisplay(d, rect);
    const isTarget = d === target;
    strokeRect(ctx, r, isTarget ? DETECTION_HOVER_COLOR : DETECTION_COLOR, !isTarget, isTarget ? 3 : 1.5);
    if (isTarget) {
      ctx.save();
      ctx.fillStyle = DETECTION_HOVER_COLOR;
      ctx.font = "12px sans-serif";
      ctx.fillText(`${d.class_name} ${Math.round(d.score * 100)}%`, r.x + 2, Math.max(12, r.y - 4));
      ctx.restore();
    }
  }
}

function draw() {
  // main-window rAF pauses when the tab is hidden, which would freeze the PiP overlay
  hostWindow().requestAnimationFrame(draw);
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
  if (ctrlDown && trackingAllowed()) {
    drawDetections(ctx, rect);
  } else if (hover && trackingAllowed()) {
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
  ctrlDown = ev.ctrlKey;
});

// Track Ctrl in whichever window hosts the video (main page or Document PiP)
function bindCtrlKey(win) {
  const update = (ev) => { ctrlDown = ev.ctrlKey; };
  win.addEventListener("keydown", update);
  win.addEventListener("keyup", update);
  win.addEventListener("blur", () => { ctrlDown = false; });
}
bindCtrlKey(window);

async function postTrack(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(errorDetail(body, res.status));
    err.status = res.status;
    throw err;
  }
  return body;
}

async function trackDetection(p) {
  try {
    const body = await postTrack("/api/ai/track-detection", { x: p.nx, y: p.ny });
    const d = body.detection;
    setMessage(`検出物体を選択: ${d.class_name} ${Math.round(d.score * 100)}%（点 [${body.point.x},${body.point.y}]）`);
  } catch (e) {
    const text = e.status === 404 ? "その位置に検出枠がありません" : `選択に失敗: ${e.message}`;
    setMessage(text, true);
  }
}
canvas.addEventListener("mouseleave", () => { hover = null; });

// macOS turns Ctrl+click into a right-click (contextmenu, no click event): handle it the same way
canvas.addEventListener("contextmenu", (ev) => {
  if (!ev.ctrlKey) return;
  ev.preventDefault();
  handleVideoClick(ev);
});

canvas.addEventListener("click", (ev) => handleVideoClick(ev));

async function handleVideoClick(ev) {
  if (!trackingAllowed()) {
    setMessage("AIトラッキングはRGBモードでのみ使えます", true);
    return;
  }
  const p = toNormalized(...localPoint(ev), pictureRect());
  if (!p) return; // clicked on the black bars
  lastClickAt = performance.now();
  if (ev.ctrlKey) {
    await trackDetection(p);
    return;
  }
  flash = { ...p, until: performance.now() + FLASH_MS };
  try {
    const body = await postTrack("/api/ai/track-point", { x: p.nx, y: p.ny, box_px: currentBoxPx() });
    const b = body.box;
    setMessage(`追跡指示を送信: [${b.lx},${b.ly}]-[${b.rx},${b.ry}]（${body.stream.width}×${body.stream.height}）`);
  } catch (e) {
    setMessage(`追跡開始に失敗: ${e.message}`, true);
  }
}

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
setupDisplayModes({
  section,
  wrap,
  video,
  fullBtn: document.getElementById("video-full-btn"),
  pipBtn: document.getElementById("video-pip-btn"),
  onLayoutChange: resizeCanvas,
  onMessage: setMessage,
  onPipWindow: bindCtrlKey,
});
resizeCanvas();
requestAnimationFrame(draw);
openEvents();
player.start();
