// ROI preset CSV import: "名前,lat,lon,alt" (alt = MSL metres). Pure functions (tested with node --test).

export const ROI_SLOT_COUNT = 10;
const NAME_MAX = 64;

/** Decode file bytes: UTF-8 (with or without BOM), falling back to Shift_JIS (Excel on Windows). */
export function decodeCsvBytes(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return new TextDecoder("shift_jis").decode(bytes);
  }
}

/** Split one CSV line, honouring "quoted, fields" and "" escapes. */
function splitCsvLine(line) {
  const fields = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else if (c === '"') {
        quoted = false;
      } else {
        cur += c;
      }
    } else if (c === '"') {
      quoted = true;
    } else if (c === ",") {
      fields.push(cur);
      cur = "";
    } else {
      cur += c;
    }
  }
  fields.push(cur);
  return fields.map((f) => f.trim());
}

function toNumber(text) {
  return text === "" ? NaN : Number(text);
}

function validateRow(fields) {
  if (fields.length < 4) return { error: "列が足りません（名前,lat,lon,alt）" };
  const [name, latText, lonText, altText] = fields;
  const lat = toNumber(latText);
  const lon = toNumber(lonText);
  const alt = toNumber(altText);
  if (name.length > NAME_MAX) return { error: `名前が長すぎます（${NAME_MAX}文字まで）` };
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) return { error: `lat が不正です（${latText}）` };
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) return { error: `lon が不正です（${lonText}）` };
  if (!Number.isFinite(alt) || alt < -500 || alt > 9000) return { error: `alt が不正です（${altText}）` };
  return { row: { name, lat, lon, alt_msl: alt } };
}

/**
 * Parse ROI CSV text. Uses the first ROI_SLOT_COUNT data rows; a first line whose lat/lon
 * are not numbers is treated as a header. Returns { rows, errors, skippedRows }.
 */
export function parseRoiCsv(text, maxRows = ROI_SLOT_COUNT) {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/);
  const rows = [];
  const errors = [];
  let skippedRows = 0;
  let firstContent = true;

  lines.forEach((raw, index) => {
    if (raw.trim() === "") return;
    const fields = splitCsvLine(raw);
    if (firstContent) {
      firstContent = false;
      if (Number.isNaN(toNumber(fields[1] ?? "")) && Number.isNaN(toNumber(fields[2] ?? ""))) return; // header
    }
    if (rows.length + errors.length >= maxRows) {
      skippedRows += 1;
      return;
    }
    const { row, error } = validateRow(fields);
    if (error) errors.push(`${index + 1}行目: ${error}`);
    else rows.push(row);
  });

  if (rows.length === 0 && errors.length === 0) errors.push("データ行がありません");
  return { rows, errors, skippedRows };
}

/** Rows -> roi_1..roi_N targets; slots without a row are emptied (the file is the full list). */
export function toRoiTargets(rows, slotCount = ROI_SLOT_COUNT) {
  return Array.from({ length: slotCount }, (_, i) => {
    const r = rows[i];
    return r
      ? { id: `roi_${i + 1}`, name: r.name, lat: r.lat, lon: r.lon, alt_msl: r.alt_msl }
      : { id: `roi_${i + 1}`, name: "", lat: 0, lon: 0, alt_msl: 0 };
  });
}
