// Pure helpers for the live-video source switch (MT11 RTSP / Android relay).

export const SOURCE_LABELS = {
  mt11: ["MT11", "ok"],
  android: ["ANDROID", "warn"],
  none: ["NO SOURCE", "bad"],
};
export const SCANNING_LABEL = ["SCANNING", "warn"];

/** Badge for a source view ({source, scanning}). */
export function sourceLabel(source, scanning) {
  if (source === "none" && scanning) return SCANNING_LABEL;
  return SOURCE_LABELS[source] ?? [String(source).toUpperCase(), ""];
}

/** What the player should do when the server-side source changes. */
export function playerAction(prev, next) {
  if (prev === next) return "none";
  if (next === "none") return "stop";
  if (prev === null || prev === "none") return "start";
  return "restart";
}

/** Status text for the message line, or null when nothing needs saying. */
export function sourceMessage(source, rescanS) {
  if (source === "none") {
    return `映像ソースがありません。MT11（RTSP）→ Android の順に ${rescanS} 秒ごとに再スキャンしています`;
  }
  if (source === "android") {
    return "MT11 に接続できないため Android 端末の画面を表示しています（カメラが復帰すると自動で切り替わります）";
  }
  return null;
}
