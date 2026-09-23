// Main-stream encoding switcher (0x20 / 0x21). Recording stream is configured separately.

const presetSelect = document.getElementById("enc-preset");
const applyBtn = document.getElementById("enc-apply-btn");
const currentText = document.getElementById("enc-current");
const msgText = document.getElementById("enc-msg");

const CODEC_LABELS = { h264: "H.264", h265: "H.265" };

function describe(cur) {
  if (!cur) return "取得中...";
  const fps = cur.fps ? ` ${cur.fps}fps` : "";
  const kbps = cur.bitrate_kbps ? ` ${Math.round(cur.bitrate_kbps / 100) / 10}Mbps` : "";
  return `${CODEC_LABELS[cur.codec] ?? cur.codec} ${cur.width}×${cur.height}${fps}${kbps}`;
}

async function refresh(populate = false) {
  try {
    const res = await fetch("/api/video/encoding");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    currentText.textContent = describe(data.current);
    if (populate) {
      presetSelect.replaceChildren(
        ...data.presets.map((p) => {
          const opt = document.createElement("option");
          opt.value = p.key;
          opt.textContent = p.label;
          return opt;
        }),
      );
      const cur = data.current;
      const match = cur && data.presets.find((p) => p.key === `${cur.codec}_${cur.height === 2160 ? "4k" : `${cur.height}p`}`);
      if (match) presetSelect.value = match.key;
    }
    return data;
  } catch (e) {
    currentText.textContent = `取得失敗: ${e.message}`;
    return null;
  }
}

applyBtn.addEventListener("click", async () => {
  const preset = presetSelect.value;
  if (!preset) return;
  applyBtn.disabled = true;
  msgText.textContent = "変更中...（映像が一時的に途切れます）";
  try {
    const res = await fetch("/api/video/encoding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = body.detail;
      throw new Error(Array.isArray(d) ? d.map((e) => e.msg).join("; ") : d ?? `HTTP ${res.status}`);
    }
    setTimeout(async () => {
      const data = await refresh();
      const ok = data?.last_set_ok;
      msgText.textContent = ok === false ? "カメラが変更を拒否しました" : "変更を送信しました（反映まで数秒かかる場合があります）";
    }, 1500);
  } catch (e) {
    msgText.textContent = `変更に失敗: ${e.message}`;
  } finally {
    applyBtn.disabled = false;
  }
});

refresh(true);
setInterval(() => refresh(false), 10000);
