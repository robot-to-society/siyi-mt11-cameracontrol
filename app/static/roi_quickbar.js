// ROI quick bar under the live video: one button per saved preset (toggle), Stop, status.
// Classic script loaded after app.js; uses toggleRoi / stopRoi / roiActiveId / fmtNum from it.
// Buttons carry the "roi-go-btn" class, so app.js updateRoiUI() highlights the active one.

let roiQuickTargets = [];

function isRoiTargetSet(t) {
  return !(t.lat === 0 && t.lon === 0);
}

/** Render buttons from the *saved* presets (the server starts ROI from its saved config). */
function renderRoiQuickBar(targets) {
  roiQuickTargets = targets.filter(isRoiTargetSet);
  const box = document.getElementById("video-roi-buttons");
  if (!box) return;
  const buttons = roiQuickTargets.map((t) => {
    const btn = document.createElement("button");
    btn.className = "mode-btn roi-go-btn video-roi-btn";
    btn.dataset.id = t.id;
    btn.textContent = t.name || t.id;
    btn.title = `${t.id}: ${t.lat}, ${t.lon} / alt ${t.alt_msl} m`;
    btn.classList.toggle("active", t.id === roiActiveId);
    btn.addEventListener("click", () => toggleRoi(t.id));
    return btn;
  });
  if (buttons.length === 0) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "ROIタブでプリセットを登録・保存すると、ここにボタンが出ます";
    buttons.push(empty);
  }
  box.replaceChildren(...buttons);
}

/** Called from app.js updateRoiUI() every status poll. */
function updateRoiQuickStatus(roi, vehicle) {
  const status = document.getElementById("video-roi-status");
  if (!status || !roi) return;
  const active = roi.active_target_id;
  const error = roi.last_error || vehicle?.mavlink_error || "";
  if (!active) {
    status.textContent = "OFF";
    status.classList.remove("roi-error");
    return;
  }
  const target = roiQuickTargets.find((t) => t.id === active);
  const label = target?.name || active;
  status.textContent = error
    ? `${label} 追従中 / ${error}`
    : `${label} 追従中 / 距離 ${fmtNum(roi.distance_m, 0)} m`;
  status.classList.toggle("roi-error", Boolean(error));
}

document.getElementById("video-roi-stop")?.addEventListener("click", () => stopRoi());
