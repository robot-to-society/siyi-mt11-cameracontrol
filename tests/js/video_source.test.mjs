import { test } from "node:test";
import assert from "node:assert/strict";

import { SOURCE_LABELS, playerAction, sourceLabel, sourceMessage } from "../../app/static/video_source.js";

test("playerAction: first snapshot starts the player when a source exists", () => {
  assert.equal(playerAction(null, "mt11"), "start");
  assert.equal(playerAction(null, "android"), "start");
});

test("playerAction: nothing to do while the source is unchanged", () => {
  assert.equal(playerAction("mt11", "mt11"), "none");
  assert.equal(playerAction("none", "none"), "none");
  assert.equal(playerAction(null, "none"), "stop");
});

test("playerAction: switching sources restarts, losing every source stops", () => {
  assert.equal(playerAction("mt11", "android"), "restart");
  assert.equal(playerAction("android", "mt11"), "restart");
  assert.equal(playerAction("mt11", "none"), "stop");
  assert.equal(playerAction("none", "android"), "start");
});

test("labels cover every source", () => {
  assert.deepEqual(Object.keys(SOURCE_LABELS).sort(), ["android", "mt11", "none"]);
  assert.equal(SOURCE_LABELS.none[1], "bad");
});

test("sourceMessage explains the scan when nothing is available", () => {
  assert.match(sourceMessage("none", 15), /15/);
  assert.match(sourceMessage("android", 15), /Android/);
  assert.equal(sourceMessage("mt11", 15), null);
});

test("sourceLabel shows SCANNING only before the first scan", () => {
  assert.deepEqual(sourceLabel("none", true), ["SCANNING", "warn"]);
  assert.deepEqual(sourceLabel("none", false), ["NO SOURCE", "bad"]);
  assert.deepEqual(sourceLabel("mt11", true), ["MT11", "ok"]);
  assert.deepEqual(sourceLabel("hdmi", false), ["HDMI", ""]);
});
