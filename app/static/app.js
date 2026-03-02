const statusBadge = document.getElementById("status-badge");
const connectionText = document.getElementById("connection-text");
const ipInput = document.getElementById("camera-ip");
const applyIpBtn = document.getElementById("apply-ip");
const recordBtn = document.getElementById("record-btn");
const recordLabel = document.getElementById("record-label");
const shutterBtn = document.getElementById("shutter-btn");
const zoomSection = document.getElementById("zoom-section");
const zoomValue = document.getElementById("zoom-value");
const zoomMaxValue = document.getElementById("zoom-max-value");
const zoomBar = document.getElementById("zoom-bar");
const zoomIncBtn = document.getElementById("zoom-inc-btn");
const zoomDecBtn = document.getElementById("zoom-dec-btn");
const videoModeValue = document.getElementById("video-mode-value");
const modeButtons = Array.from(document.querySelectorAll(".mode-btn"));

let recordState = "idle";

async function postJSON(url, body = undefined) {
  const res = await fetch(url, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json();
}

function setStatusUI(data) {
  ipInput.value = data.ip ?? ipInput.value;
  const connectedText = data.connected ? "Connected" : "Disconnected";
  const errorText = data.last_error ? ` / ${data.last_error}` : "";
  connectionText.textContent = `${connectedText}${errorText}`;

  if (data.record_text === "recording") {
    recordState = "recording";
    statusBadge.textContent = "RECORDING";
    statusBadge.className = "status-badge recording";
    recordBtn.classList.add("recording");
    recordLabel.textContent = "Stop Rec";
  } else if (data.record_text === "idle") {
    recordState = "idle";
    statusBadge.textContent = "IDLE";
    statusBadge.className = "status-badge idle";
    recordBtn.classList.remove("recording");
    recordLabel.textContent = "Start Rec";
  } else {
    recordState = "error";
    statusBadge.textContent = "ERROR";
    statusBadge.className = "status-badge error";
    recordBtn.classList.remove("recording");
    recordLabel.textContent = "Record";
  }

  const currentZoom = Number(data.zoom_current ?? 1);
  const maxZoom = Math.max(1, Math.floor(Number(data.zoom_max ?? 1)));
  zoomBar.max = String(maxZoom);
  zoomValue.textContent = `${currentZoom.toFixed(1)}x`;
  zoomMaxValue.textContent = `${Number(data.zoom_max).toFixed(1)}x`;
  zoomBar.disabled = false;
  zoomDecBtn.disabled = currentZoom <= 1.0;
  zoomIncBtn.disabled = currentZoom >= maxZoom;

  const currentMode = data.video_mode || "custom";
  const modeLabelMap = {
    rgb: "RGB",
    thermal: "THERMAL",
    side_by_side: "SIDE BY SIDE",
    custom: "CUSTOM",
  };
  videoModeValue.textContent = modeLabelMap[currentMode] || "CUSTOM";
  for (const btn of modeButtons) {
    btn.classList.toggle("active", btn.dataset.mode === currentMode);
  }
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    setStatusUI(data);
  } catch (e) {
    connectionText.textContent = `Status error: ${e}`;
  }
}

applyIpBtn.addEventListener("click", async () => {
  try {
    applyIpBtn.disabled = true;
    await postJSON("/api/camera/ip", { ip: ipInput.value.trim() });
    connectionText.textContent = "IP updated";
    await refreshStatus();
  } catch (e) {
    connectionText.textContent = `IP update failed: ${e}`;
  } finally {
    applyIpBtn.disabled = false;
  }
});

recordBtn.addEventListener("click", async () => {
  try {
    recordBtn.disabled = true;
    if (recordState === "recording") {
      await postJSON("/api/record/stop");
    } else {
      await postJSON("/api/record/start");
    }
    await refreshStatus();
  } catch (e) {
    connectionText.textContent = `Record error: ${e}`;
  } finally {
    recordBtn.disabled = false;
  }
});

shutterBtn.addEventListener("click", async () => {
  try {
    shutterBtn.disabled = true;
    await postJSON("/api/photo");
    connectionText.textContent = "Photo trigger sent";
    setTimeout(refreshStatus, 160);
  } catch (e) {
    connectionText.textContent = `Shutter error: ${e}`;
  } finally {
    shutterBtn.disabled = false;
  }
});

zoomIncBtn.addEventListener("click", async () => {
  try {
    zoomIncBtn.disabled = true;
    await postJSON("/api/zoom/inc");
    await refreshStatus();
  } catch (e) {
    connectionText.textContent = `Zoom error: ${e}`;
  } finally {
    zoomIncBtn.disabled = false;
  }
});

zoomDecBtn.addEventListener("click", async () => {
  try {
    zoomDecBtn.disabled = true;
    await postJSON("/api/zoom/dec");
    await refreshStatus();
  } catch (e) {
    connectionText.textContent = `Zoom error: ${e}`;
  } finally {
    zoomDecBtn.disabled = false;
  }
});

zoomBar.addEventListener("change", async () => {
  const targetZoom = Number(zoomBar.value || "1");
  try {
    zoomBar.disabled = true;
    await postJSON("/api/zoom/set", { zoom: targetZoom });
    await refreshStatus();
  } catch (e) {
    connectionText.textContent = `Zoom error: ${e}`;
  } finally {
    zoomBar.disabled = false;
  }
});

for (const btn of modeButtons) {
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    if (!mode) return;
    try {
      btn.disabled = true;
      await postJSON("/api/video-mode", { mode });
      await refreshStatus();
    } catch (e) {
      connectionText.textContent = `Video mode error: ${e}`;
    } finally {
      btn.disabled = false;
    }
  });
}

refreshStatus();
setInterval(refreshStatus, 1000);

// ═══════════════════════════════════════════════════════════════
// Joystick / Gamepad Support
// ═══════════════════════════════════════════════════════════════

// ─── Tab Switching ───────────────────────────────────────────────
const tabBtns = Array.from(document.querySelectorAll(".tab-btn"));
const tabCameraEl = document.getElementById("tab-camera");
const tabJoystickEl = document.getElementById("tab-joystick");

tabBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    tabBtns.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    tabCameraEl.classList.toggle("hidden", tab !== "camera");
    tabJoystickEl.classList.toggle("hidden", tab !== "joystick");
    if (tab === "joystick") renderJoystickConfig();
  });
});

// ─── Joystick Config ─────────────────────────────────────────────
const AXIS_FUNCTIONS = ["none", "pan", "tilt", "zoom_abs", "zoom_speed"];
const BTN_FUNCTIONS = ["none", "shutter", "thermal_toggle", "center_gimbal", "record_toggle"];

let jsConfig = {
  enabled: false,
  max_pan_speed: 30.0,
  max_tilt_speed: 20.0,
  zoom_step_hz: 2.0,
  axis_mappings: [
    { axis_id: 0, function: "pan",      deadzone: 0.08, invert: false, scale: 1.0 },
    { axis_id: 1, function: "tilt",     deadzone: 0.08, invert: true,  scale: 1.0 },
    { axis_id: 3, function: "zoom_abs", deadzone: 0.02, invert: false, scale: 1.0 },
  ],
  button_mappings: [
    { button_id: 0, function: "shutter" },
    { button_id: 1, function: "thermal_toggle" },
    { button_id: 3, function: "center_gimbal" },
  ],
};

async function loadJsConfig() {
  try {
    const res = await fetch("/api/joystick/config");
    jsConfig = await res.json();
  } catch (e) {
    console.warn("Failed to load joystick config:", e);
  }
  const chk = document.getElementById("js-enable-chk");
  if (chk) chk.checked = jsConfig.enabled;
}

// ─── Gamepad Management ──────────────────────────────────────────
let activeGamepadIndex = -1;
const connectedGamepads = {};

window.addEventListener("gamepadconnected", (e) => {
  connectedGamepads[e.gamepad.index] = e.gamepad;
  if (activeGamepadIndex === -1) activeGamepadIndex = e.gamepad.index;
  updateGamepadSelect();
  updateJsBadge();
});

window.addEventListener("gamepaddisconnected", (e) => {
  delete connectedGamepads[e.gamepad.index];
  if (activeGamepadIndex === e.gamepad.index) {
    const remaining = Object.keys(connectedGamepads);
    activeGamepadIndex = remaining.length > 0 ? parseInt(remaining[0]) : -1;
  }
  updateGamepadSelect();
  updateJsBadge();
  if (activeGamepadIndex < 0) {
    postJSON("/api/gimbal/speed", { yaw: 0, pitch: 0 }).catch(() => {});
  }
});

function updateGamepadSelect() {
  const sel = document.getElementById("js-gamepad-select");
  if (!sel) return;
  sel.innerHTML = '<option value="">-- Select gamepad --</option>';
  for (const [idx, gp] of Object.entries(connectedGamepads)) {
    const opt = document.createElement("option");
    opt.value = idx;
    opt.textContent = `[${idx}] ${gp.id}`;
    if (parseInt(idx) === activeGamepadIndex) opt.selected = true;
    sel.appendChild(opt);
  }
}

function updateJsBadge() {
  const badge = document.getElementById("js-badge");
  const hint = document.getElementById("js-hint");
  if (!badge) return;
  if (activeGamepadIndex >= 0) {
    badge.textContent = "CONNECTED";
    badge.className = "js-badge connected";
    if (hint) hint.textContent = connectedGamepads[activeGamepadIndex]?.id ?? "";
  } else {
    badge.textContent = "NOT FOUND";
    badge.className = "js-badge not-found";
    if (hint) hint.textContent = "ページ操作後にゲームパッドが検出されます";
  }
}

document.getElementById("js-scan-btn")?.addEventListener("click", () => {
  const gps = navigator.getGamepads();
  for (let i = 0; i < gps.length; i++) {
    if (gps[i]) {
      connectedGamepads[i] = gps[i];
      if (activeGamepadIndex === -1) activeGamepadIndex = i;
    }
  }
  updateGamepadSelect();
  updateJsBadge();
  if (document.querySelector(".tab-btn.active")?.dataset?.tab === "joystick") {
    renderJoystickConfig();
  }
});

document.getElementById("js-gamepad-select")?.addEventListener("change", (e) => {
  activeGamepadIndex = e.target.value !== "" ? parseInt(e.target.value) : -1;
});

document.getElementById("js-enable-chk")?.addEventListener("change", (e) => {
  jsConfig.enabled = e.target.checked;
  if (!jsConfig.enabled) {
    postJSON("/api/gimbal/speed", { yaw: 0, pitch: 0 }).catch(() => {});
  }
});

// ─── Config UI Render ────────────────────────────────────────────
function renderJoystickConfig() {
  renderAxisTable();
  renderBtnTable();
  const panInput = document.getElementById("js-max-pan");
  const tiltInput = document.getElementById("js-max-tilt");
  if (panInput) panInput.value = jsConfig.max_pan_speed;
  if (tiltInput) tiltInput.value = jsConfig.max_tilt_speed;
  const chk = document.getElementById("js-enable-chk");
  if (chk) chk.checked = jsConfig.enabled;
}

function renderAxisTable() {
  const tbody = document.getElementById("js-axis-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const gp = activeGamepadIndex >= 0 ? navigator.getGamepads()[activeGamepadIndex] : null;
  const numAxes = gp ? gp.axes.length : 4;
  for (let axisId = 0; axisId < numAxes; axisId++) {
    const mapping = jsConfig.axis_mappings.find((m) => m.axis_id === axisId) || {
      axis_id: axisId, function: "none", deadzone: 0.08, invert: false, scale: 1.0,
    };
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${axisId}</td>` +
      `<td><select class="js-axis-fn" data-axis="${axisId}">` +
      AXIS_FUNCTIONS.map((f) => `<option value="${f}"${f === mapping.function ? " selected" : ""}>${f}</option>`).join("") +
      `</select></td>` +
      `<td><input type="number" class="js-axis-dz js-num-input" data-axis="${axisId}" min="0" max="0.99" step="0.01" value="${mapping.deadzone}" /></td>` +
      `<td><input type="checkbox" class="js-axis-inv" data-axis="${axisId}"${mapping.invert ? " checked" : ""} /></td>` +
      `<td><input type="number" class="js-axis-scale js-num-input" data-axis="${axisId}" min="0.1" max="5" step="0.1" value="${mapping.scale}" /></td>`;
    tbody.appendChild(tr);
  }
}

function renderBtnTable() {
  const tbody = document.getElementById("js-btn-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const gp = activeGamepadIndex >= 0 ? navigator.getGamepads()[activeGamepadIndex] : null;
  const numBtns = gp ? gp.buttons.length : 8;
  for (let btnId = 0; btnId < numBtns; btnId++) {
    const mapping = jsConfig.button_mappings.find((m) => m.button_id === btnId) || {
      button_id: btnId, function: "none",
    };
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${btnId}</td>` +
      `<td><select class="js-btn-fn" data-btn="${btnId}">` +
      BTN_FUNCTIONS.map((f) => `<option value="${f}"${f === mapping.function ? " selected" : ""}>${f}</option>`).join("") +
      `</select></td>`;
    tbody.appendChild(tr);
  }
}

function collectAxisMappings() {
  const mappings = [];
  document.querySelectorAll(".js-axis-fn").forEach((sel) => {
    const axisId = parseInt(sel.dataset.axis);
    const fn = sel.value;
    if (fn === "none") return;
    const dz = document.querySelector(`.js-axis-dz[data-axis="${axisId}"]`);
    const inv = document.querySelector(`.js-axis-inv[data-axis="${axisId}"]`);
    const sc = document.querySelector(`.js-axis-scale[data-axis="${axisId}"]`);
    mappings.push({
      axis_id: axisId,
      function: fn,
      deadzone: parseFloat(dz?.value ?? 0.08),
      invert: inv?.checked ?? false,
      scale: parseFloat(sc?.value ?? 1.0),
    });
  });
  return mappings;
}

function collectBtnMappings() {
  const mappings = [];
  document.querySelectorAll(".js-btn-fn").forEach((sel) => {
    const btnId = parseInt(sel.dataset.btn);
    const fn = sel.value;
    if (fn === "none") return;
    mappings.push({ button_id: btnId, function: fn });
  });
  return mappings;
}

document.getElementById("js-save-btn")?.addEventListener("click", async () => {
  const panInput = document.getElementById("js-max-pan");
  const tiltInput = document.getElementById("js-max-tilt");
  jsConfig.max_pan_speed = parseFloat(panInput?.value ?? 30);
  jsConfig.max_tilt_speed = parseFloat(tiltInput?.value ?? 20);
  jsConfig.axis_mappings = collectAxisMappings();
  jsConfig.button_mappings = collectBtnMappings();
  try {
    await postJSON("/api/joystick/config", jsConfig);
    const hint = document.getElementById("js-hint");
    if (hint) {
      hint.textContent = "Config saved!";
      setTimeout(updateJsBadge, 1500);
    }
  } catch (e) {
    alert(`Save failed: ${e}`);
  }
});

document.getElementById("js-center-btn")?.addEventListener("click", async () => {
  try {
    await postJSON("/api/gimbal/center");
  } catch (e) {
    alert(`Center gimbal failed: ${e}`);
  }
});

// ─── Game Loop ───────────────────────────────────────────────────
let prevBtns = [];
let lastGimbalSend = 0;
let lastZoomAbsSend = 0;
let lastZoomSpeedSend = 0;
let gimbalStopped = true;
let prevZoomAbsTarget = null;

function applyDeadzone(val, dz) {
  if (Math.abs(val) < dz) return 0;
  const sign = val > 0 ? 1 : -1;
  return sign * (Math.abs(val) - dz) / (1 - dz);
}

function getAxisMapping(axisId) {
  return jsConfig.axis_mappings.find((m) => m.axis_id === axisId) ?? null;
}

function getBtnMapping(btnId) {
  return jsConfig.button_mappings.find((m) => m.button_id === btnId) ?? null;
}

function processAxes(axes, now) {
  let panVal = 0;
  let tiltVal = 0;
  let zoomAbsRaw = null;
  let zoomAbsMapping = null;
  let zoomSpeedVal = 0;

  for (let i = 0; i < axes.length; i++) {
    const m = getAxisMapping(i);
    if (!m || m.function === "none") continue;
    let raw = axes[i];
    if (m.invert) raw = -raw;
    const val = applyDeadzone(raw, m.deadzone) * m.scale;
    switch (m.function) {
      case "pan":        panVal = val; break;
      case "tilt":       tiltVal = val; break;
      case "zoom_abs":   zoomAbsRaw = axes[i]; zoomAbsMapping = m; break;
      case "zoom_speed": zoomSpeedVal = val; break;
    }
  }

  // Gimbal speed (10 Hz max)
  if (now - lastGimbalSend >= 100) {
    const yaw = Math.round(panVal * jsConfig.max_pan_speed);
    const pitch = Math.round(tiltVal * jsConfig.max_tilt_speed);
    if (yaw !== 0 || pitch !== 0) {
      postJSON("/api/gimbal/speed", { yaw, pitch }).catch(() => {});
      gimbalStopped = false;
      lastGimbalSend = now;
    } else if (!gimbalStopped) {
      postJSON("/api/gimbal/speed", { yaw: 0, pitch: 0 }).catch(() => {});
      gimbalStopped = true;
      lastGimbalSend = now;
    }
  }

  // Zoom absolute (2 Hz max, only when value changes significantly)
  if (zoomAbsMapping !== null && now - lastZoomAbsSend >= 500) {
    let raw = zoomAbsRaw;
    if (zoomAbsMapping.invert) raw = -raw;
    const normalized = (raw + 1) / 2; // -1..+1 → 0..1
    const zoomMaxVal = parseFloat(zoomBar.max || "30");
    const targetZoom = Math.max(1.0, Math.round((1 + normalized * (zoomMaxVal - 1)) * 10) / 10);
    if (prevZoomAbsTarget === null || Math.abs(targetZoom - prevZoomAbsTarget) >= 0.5) {
      postJSON("/api/zoom/set", { zoom: targetZoom }).catch(() => {});
      prevZoomAbsTarget = targetZoom;
      lastZoomAbsSend = now;
    }
  }

  // Zoom speed
  if (now - lastZoomSpeedSend >= 200) {
    if (zoomSpeedVal > 0) {
      postJSON("/api/zoom/speed", { direction: 1 }).catch(() => {});
      lastZoomSpeedSend = now;
    } else if (zoomSpeedVal < 0) {
      postJSON("/api/zoom/speed", { direction: -1 }).catch(() => {});
      lastZoomSpeedSend = now;
    }
  }
}

function processButtons(buttons) {
  for (let i = 0; i < buttons.length; i++) {
    const pressed = buttons[i].pressed;
    const wasPressed = prevBtns[i] ?? false;
    if (pressed && !wasPressed) {
      const m = getBtnMapping(i);
      if (m) handleButtonAction(m.function);
    }
    prevBtns[i] = pressed;
  }
}

function handleButtonAction(fn) {
  switch (fn) {
    case "shutter":
      postJSON("/api/photo").catch(() => {});
      break;
    case "thermal_toggle": {
      const modes = ["rgb", "thermal", "side_by_side"];
      const activeBtn = modeButtons.find((btn) => btn.classList.contains("active"));
      const cur = activeBtn?.dataset.mode ?? "rgb";
      const idx = modes.indexOf(cur);
      const next = modes[(idx + 1) % modes.length];
      postJSON("/api/video-mode", { mode: next }).then(() => refreshStatus()).catch(() => {});
      break;
    }
    case "center_gimbal":
      postJSON("/api/gimbal/center").catch(() => {});
      break;
    case "record_toggle":
      if (recordState === "recording") {
        postJSON("/api/record/stop").then(() => refreshStatus()).catch(() => {});
      } else {
        postJSON("/api/record/start").then(() => refreshStatus()).catch(() => {});
      }
      break;
  }
}

function updateLiveDisplay(gp) {
  const axesCont = document.getElementById("js-axes-container");
  const btnsCont = document.getElementById("js-buttons-container");
  if (!axesCont || !btnsCont) return;

  if (axesCont.children.length !== gp.axes.length) {
    axesCont.innerHTML = "";
    for (let i = 0; i < gp.axes.length; i++) {
      const row = document.createElement("div");
      row.className = "js-axis-row";
      row.innerHTML =
        `<span class="js-axis-label">Axis ${i}</span>` +
        `<div class="js-axis-bar-track"><div class="js-axis-bar" id="js-ax-bar-${i}"></div></div>` +
        `<span class="js-axis-val" id="js-ax-val-${i}">0.00</span>`;
      axesCont.appendChild(row);
    }
  }
  for (let i = 0; i < gp.axes.length; i++) {
    const v = gp.axes[i];
    const bar = document.getElementById(`js-ax-bar-${i}`);
    const val = document.getElementById(`js-ax-val-${i}`);
    if (bar) {
      bar.style.width = `${Math.abs(v) * 50}%`;
      bar.style.left = v >= 0 ? "50%" : `${50 + v * 50}%`;
    }
    if (val) val.textContent = v.toFixed(2);
  }

  if (btnsCont.children.length !== gp.buttons.length) {
    btnsCont.innerHTML = "";
    for (let i = 0; i < gp.buttons.length; i++) {
      const cell = document.createElement("span");
      cell.className = "js-btn-cell";
      cell.id = `js-btn-${i}`;
      cell.textContent = `B${i}`;
      btnsCont.appendChild(cell);
    }
  }
  for (let i = 0; i < gp.buttons.length; i++) {
    const cell = document.getElementById(`js-btn-${i}`);
    if (cell) cell.classList.toggle("active", gp.buttons[i].pressed);
  }
}

function gameLoop() {
  requestAnimationFrame(gameLoop);
  if (activeGamepadIndex < 0) return;
  const gp = navigator.getGamepads()[activeGamepadIndex];
  if (!gp) return;
  const now = performance.now();
  if (jsConfig.enabled) {
    processAxes(gp.axes, now);
    processButtons(gp.buttons);
  }
  const activeTab = document.querySelector(".tab-btn.active")?.dataset?.tab;
  if (activeTab === "joystick") {
    updateLiveDisplay(gp);
  }
}

// ─── Init ─────────────────────────────────────────────────────────
loadJsConfig().then(() => gameLoop());
