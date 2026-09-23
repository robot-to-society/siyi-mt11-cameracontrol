// ROI tab: import presets from a CSV file ("名前,lat,lon,alt"). Fills the table; Save persists.
import { decodeCsvBytes, parseRoiCsv, toRoiTargets } from "./roi_csv.js";

const fileInput = document.getElementById("roi-csv-input");
const importBtn = document.getElementById("roi-csv-btn");
const hint = document.getElementById("roi-csv-hint");

function show(text, isError = false) {
  hint.textContent = text;
  hint.classList.toggle("roi-error", isError);
}

importBtn?.addEventListener("click", () => fileInput.click());

fileInput?.addEventListener("change", async () => {
  const file = fileInput.files?.[0];
  fileInput.value = ""; // allow re-selecting the same file
  if (!file) return;
  try {
    const text = decodeCsvBytes(new Uint8Array(await file.arrayBuffer()));
    const { rows, errors, skippedRows } = parseRoiCsv(text);
    if (errors.length > 0) {
      show(`読み込みを中止しました: ${errors.join(" / ")}`, true);
      return;
    }
    await window.applyRoiImport(toRoiTargets(rows));
    const skipped = skippedRows > 0 ? `（11行目以降の${skippedRows}行は無視）` : "";
    show(`${file.name} から ${rows.length} 件を読み込みました${skipped}。内容を確認して「Save ROI Config」で保存してください。`);
  } catch (e) {
    show(`読み込みに失敗しました: ${e.message}`, true);
  }
});
