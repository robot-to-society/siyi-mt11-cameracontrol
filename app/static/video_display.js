// Fullscreen and Picture-in-Picture for the live video panel.
// Chrome's Document Picture-in-Picture moves the whole video+overlay into the PiP window,
// so click-to-track keeps working there. Other browsers fall back to plain video PiP.

const PIP_DEFAULT_W = 640;
const PIP_DEFAULT_H = 360;

export function setupDisplayModes({ section, wrap, video, fullBtn, pipBtn, onLayoutChange, onMessage }) {
  let pipWindow = null;
  let placeholder = null;

  // ── fullscreen (whole panel: badges, video, controls) ─────────
  function updateFullLabel() {
    fullBtn.textContent = document.fullscreenElement === section ? "Exit Full" : "Full";
  }

  fullBtn.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement === section) {
        await document.exitFullscreen();
      } else {
        await section.requestFullscreen();
      }
    } catch (e) {
      onMessage(`全画面にできません: ${e.message}`, true);
    }
  });
  document.addEventListener("fullscreenchange", () => {
    updateFullLabel();
    onLayoutChange();
  });
  if (!document.fullscreenEnabled) fullBtn.disabled = true;

  // ── picture-in-picture ───────────────────────────────────────
  function restoreFromPip() {
    if (placeholder) {
      placeholder.replaceWith(wrap);
      placeholder = null;
    }
    wrap.classList.remove("in-pip");
    pipWindow = null;
    pipBtn.textContent = "PiP";
    onLayoutChange();
  }

  async function openDocumentPip() {
    const win = await window.documentPictureInPicture.requestWindow({
      width: wrap.clientWidth || PIP_DEFAULT_W,
      height: wrap.clientHeight || PIP_DEFAULT_H,
    });
    const css = win.document.createElement("link");
    css.rel = "stylesheet";
    css.href = new URL("/static/styles.css", window.location.href).href;
    win.document.head.appendChild(css);
    win.document.body.classList.add("pip-body");

    placeholder = document.createElement("div");
    placeholder.className = "video-pip-placeholder";
    placeholder.textContent = "ピクチャーインピクチャーで表示中";
    wrap.replaceWith(placeholder);
    wrap.classList.add("in-pip");
    win.document.body.appendChild(wrap);
    video.play().catch(() => {});

    pipWindow = win;
    pipBtn.textContent = "Close PiP";
    win.addEventListener("resize", onLayoutChange);
    win.addEventListener("pagehide", restoreFromPip, { once: true });
    onLayoutChange();
  }

  pipBtn.addEventListener("click", async () => {
    try {
      if (pipWindow) {
        pipWindow.close(); // pagehide restores the panel
      } else if ("documentPictureInPicture" in window) {
        await openDocumentPip();
      } else if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
      } else if (document.pictureInPictureEnabled) {
        await video.requestPictureInPicture();
        onMessage("このブラウザのPiPは映像のみです（追跡枠の表示とクリック操作は元の画面で）");
      } else {
        onMessage("このブラウザはピクチャーインピクチャーに対応していません", true);
      }
    } catch (e) {
      onMessage(`PiPにできません: ${e.message}`, true);
    }
  });

  updateFullLabel();
}
