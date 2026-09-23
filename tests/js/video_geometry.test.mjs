import { test } from "node:test";
import assert from "node:assert/strict";

import {
  contentRect,
  previewBox,
  toNormalized,
  trackToDisplay,
} from "../../app/static/video_geometry.js";

test("contentRect: pillarbox (element wider than video)", () => {
  const r = contentRect(1000, 400, 1920, 1080);
  assert.equal(r.h, 400);
  assert.ok(Math.abs(r.w - 711.11) < 0.01);
  assert.ok(Math.abs(r.x - 144.44) < 0.01);
  assert.equal(r.y, 0);
});

test("contentRect: letterbox (element taller than video)", () => {
  const r = contentRect(640, 480, 1280, 720);
  assert.deepEqual([r.x, r.y, r.w, r.h], [0, 60, 640, 360]);
});

test("contentRect: unknown sizes", () => {
  assert.equal(contentRect(640, 480, 0, 0), null);
});

test("toNormalized: inside and outside the picture", () => {
  const r = contentRect(640, 480, 1280, 720);
  assert.deepEqual(toNormalized(320, 240, r), { nx: 0.5, ny: 0.5 });
  assert.equal(toNormalized(320, 30, r), null); // in the black bar
  assert.equal(toNormalized(10, 10, null), null);
});

test("previewBox mirrors server click_to_stream_box", () => {
  const r = { x: 0, y: 0, w: 960, h: 540 }; // half-size display of 1920x1080
  const b = previewBox(0.5, 0.5, r, 1920, 1080, 150);
  assert.deepEqual(b, { x: 442.5, y: 232.5, w: 75, h: 75 });
  const corner = previewBox(1, 1, r, 1920, 1080, 150);
  assert.deepEqual(corner, { x: 884.5, y: 464.5, w: 75, h: 75 });
});

test("trackToDisplay maps normalized box into the picture rect", () => {
  const r = { x: 100, y: 50, w: 800, h: 450 };
  assert.deepEqual(trackToDisplay({ x: 0.25, y: 0.5, w: 0.1, h: 0.2 }, r), { x: 300, y: 275, w: 80, h: 90 });
});

test("previewBox odd box size matches server rounding", () => {
  const r = { x: 0, y: 0, w: 1920, h: 1080 };
  assert.equal(previewBox(0.5, 0.5, r, 1920, 1080, 151).x, 885);
  assert.equal(previewBox(0.5, 0.5, r, 1920, 1080, 149).x, 886);
});

import { detectionAt, detectionToDisplay } from "../../app/static/video_geometry.js";

test("detectionAt picks the smallest box containing the point", () => {
  const big = { x0: 0, y0: 0, x1: 0.8, y1: 0.8 };
  const small = { x0: 0.4, y0: 0.4, x1: 0.6, y1: 0.6 };
  assert.equal(detectionAt([big, small], 0.5, 0.5), small);
  assert.equal(detectionAt([big, small], 0.1, 0.1), big);
  assert.equal(detectionAt([big, small], 0.9, 0.9), null);
  assert.equal(detectionAt([], 0.5, 0.5), null);
});

test("detectionToDisplay maps corner box to picture rect", () => {
  const r = { x: 100, y: 50, w: 800, h: 450 };
  assert.deepEqual(detectionToDisplay({ x0: 0.25, y0: 0.5, x1: 0.5, y1: 1 }, r), { x: 300, y: 275, w: 200, h: 225 });
});
