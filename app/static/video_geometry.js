// Pure geometry for the video overlay (object-fit: contain letterboxing).
// Mirrors app/ai_tracking.py click_to_stream_box so the hover preview matches what is sent.

/** Rectangle actually covered by the picture inside an element, or null if sizes are unknown. */
export function contentRect(elemW, elemH, videoW, videoH) {
  if (!(elemW > 0 && elemH > 0 && videoW > 0 && videoH > 0)) return null;
  const scale = Math.min(elemW / videoW, elemH / videoH);
  const w = videoW * scale;
  const h = videoH * scale;
  return { x: (elemW - w) / 2, y: (elemH - h) / 2, w, h };
}

/** Element-local point -> normalized picture coords, or null when on the black bars. */
export function toNormalized(px, py, rect) {
  if (!rect) return null;
  const nx = (px - rect.x) / rect.w;
  const ny = (py - rect.y) / rect.h;
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return null;
  return { nx, ny };
}

function axisSpan(center, size, limit) {
  const s = Math.min(size, limit - 1);
  const start = Math.max(0, Math.min(Math.round(center - s / 2), limit - 1 - s));
  return [start, start + s];
}

/** Display rect of the stream-pixel box the server will select for a click at (nx, ny). */
export function previewBox(nx, ny, rect, streamW, streamH, boxPx) {
  if (!rect || !(streamW > 1 && streamH > 1)) return null;
  const [lx, rx] = axisSpan(nx * streamW, boxPx, streamW);
  const [ly, ry] = axisSpan(ny * streamH, boxPx, streamH);
  const sx = rect.w / streamW;
  const sy = rect.h / streamH;
  return { x: rect.x + lx * sx, y: rect.y + ly * sy, w: (rx - lx) * sx, h: (ry - ly) * sy };
}

/** Normalized top-left box (from 0x50) -> display rect. */
export function trackToDisplay(track, rect) {
  return {
    x: rect.x + track.x * rect.w,
    y: rect.y + track.y * rect.h,
    w: track.w * rect.w,
    h: track.h * rect.h,
  };
}

/** Smallest detection box (normalized corners) containing the point, or null. */
export function detectionAt(detections, nx, ny) {
  let best = null;
  let bestArea = Infinity;
  for (const d of detections) {
    if (nx < d.x0 || nx > d.x1 || ny < d.y0 || ny > d.y1) continue;
    const area = (d.x1 - d.x0) * (d.y1 - d.y0);
    if (area < bestArea) {
      best = d;
      bestArea = area;
    }
  }
  return best;
}

/** Normalized corner box (0x5F detection) -> display rect. */
export function detectionToDisplay(d, rect) {
  return {
    x: rect.x + d.x0 * rect.w,
    y: rect.y + d.y0 * rect.h,
    w: (d.x1 - d.x0) * rect.w,
    h: (d.y1 - d.y0) * rect.h,
  };
}
