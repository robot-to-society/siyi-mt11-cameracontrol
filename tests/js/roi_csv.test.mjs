import { test } from "node:test";
import assert from "node:assert/strict";

import { decodeCsvBytes, parseRoiCsv, toRoiTargets } from "../../app/static/roi_csv.js";

test("parses rows with a header line", () => {
  const r = parseRoiCsv("名前,lat,lon,alt\n東京タワー,35.6585805,139.7454329,150\n鉄塔,35.1,139.2,40.5\n");
  assert.deepEqual(r.errors, []);
  assert.equal(r.skippedRows, 0);
  assert.deepEqual(r.rows, [
    { name: "東京タワー", lat: 35.6585805, lon: 139.7454329, alt_msl: 150 },
    { name: "鉄塔", lat: 35.1, lon: 139.2, alt_msl: 40.5 },
  ]);
});

test("works without a header, with CRLF, BOM, spaces and blank lines", () => {
  const r = parseRoiCsv("﻿ A , 35.0 , 139.0 , 10 \r\n\r\nB,35.1,139.1,20\r\n");
  assert.deepEqual(r.errors, []);
  assert.deepEqual(r.rows.map((x) => x.name), ["A", "B"]);
});

test("quoted names may contain commas and quotes", () => {
  const r = parseRoiCsv('"Tower, north",35,139,10\n"He said ""hi""",35,139,10\n');
  assert.deepEqual(r.rows.map((x) => x.name), ["Tower, north", 'He said "hi"']);
});

test("uses only the first 10 data rows", () => {
  const lines = Array.from({ length: 13 }, (_, i) => `P${i + 1},35.${i},139,0`).join("\n");
  const r = parseRoiCsv(lines);
  assert.equal(r.rows.length, 10);
  assert.equal(r.rows[9].name, "P10");
  assert.equal(r.skippedRows, 3);
});

test("reports invalid rows with line numbers", () => {
  const r = parseRoiCsv("名前,lat,lon,alt\nA,95,139,0\nB,35,abc,0\nC,35,139\nD,35,139,0\n");
  assert.equal(r.errors.length, 3);
  assert.match(r.errors[0], /^2行目/);
  assert.match(r.errors[0], /lat/);
  assert.match(r.errors[1], /^3行目/);
  assert.match(r.errors[2], /^4行目/);
});

test("rejects empty file and too long names", () => {
  assert.match(parseRoiCsv("名前,lat,lon,alt\n").errors[0], /データ行がありません/);
  assert.match(parseRoiCsv(`${"x".repeat(65)},35,139,0`).errors[0], /名前/);
});

test("toRoiTargets fills roi_1..roi_N and clears the rest", () => {
  const targets = toRoiTargets([{ name: "A", lat: 35, lon: 139, alt_msl: 10 }], 3);
  assert.deepEqual(targets, [
    { id: "roi_1", name: "A", lat: 35, lon: 139, alt_msl: 10 },
    { id: "roi_2", name: "", lat: 0, lon: 0, alt_msl: 0 },
    { id: "roi_3", name: "", lat: 0, lon: 0, alt_msl: 0 },
  ]);
});

test("decodeCsvBytes: UTF-8 and Shift_JIS", () => {
  const utf8 = new TextEncoder().encode("名前,lat");
  assert.equal(decodeCsvBytes(utf8), "名前,lat");
  // "名前" in Shift_JIS
  const sjis = new Uint8Array([0x96, 0xbc, 0x91, 0x4f, 0x2c, 0x6c, 0x61, 0x74]);
  assert.equal(decodeCsvBytes(sjis), "名前,lat");
});
