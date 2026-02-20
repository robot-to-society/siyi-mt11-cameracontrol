const statusBadge = document.getElementById("status-badge");
const connectionText = document.getElementById("connection-text");
const ipInput = document.getElementById("camera-ip");
const applyIpBtn = document.getElementById("apply-ip");
const recordBtn = document.getElementById("record-btn");
const recordLabel = document.getElementById("record-label");
const shutterBtn = document.getElementById("shutter-btn");

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

refreshStatus();
setInterval(refreshStatus, 1000);
