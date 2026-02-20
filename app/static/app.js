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
  const zoomReady = Boolean(data.zoom_ready);
  const maxZoom = Math.max(1, Math.floor(Number(data.zoom_max ?? 1)));
  zoomSection.classList.toggle("hidden", !zoomReady);
  if (zoomReady) {
    zoomBar.max = String(maxZoom);
    zoomBar.value = String(Math.max(1, Math.min(maxZoom, Math.round(currentZoom))));
    zoomValue.textContent = `${currentZoom.toFixed(1)}x`;
    zoomMaxValue.textContent = `${Number(data.zoom_max).toFixed(1)}x`;
    zoomBar.disabled = false;
    zoomDecBtn.disabled = currentZoom <= 1.0;
    zoomIncBtn.disabled = currentZoom >= maxZoom;
  }

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

zoomBar.addEventListener("input", () => {
  const v = Number(zoomBar.value || "1");
  zoomValue.textContent = `${v.toFixed(1)}x`;
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
