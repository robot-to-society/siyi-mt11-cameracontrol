"""Video streaming (WHEP proxy to MediaMTX), click-to-track AI and main-stream encoding APIs."""

import asyncio
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.ai_tracking import (
    BOX_PX_DEFAULT,
    BOX_PX_MAX,
    BOX_PX_MIN,
    ENCODING_PRESETS,
    click_to_stream_box,
    find_preset,
    normalized_to_stream_point,
    pick_detection,
)
from app.camera_protocol import CameraState
from app.thermal import thermal_overlay

logger = logging.getLogger(__name__)

# MediaMTX WebRTC (WHEP) endpoint, reached from the Pi itself only
WHEP_UPSTREAM = os.environ.get("MT11_WHEP_URL", "http://127.0.0.1:8889/mt11/whep")
MAX_SDP_BYTES = 64 * 1024
# MediaMTX (sourceOnDemand) answers only after the RTSP source is up (default start timeout 10 s)
WHEP_TIMEOUT_S = 15.0
TRACK_MAX_AGE_S = 1.0
SSE_INTERVAL_S = 0.1
SSE_KEEPALIVE_S = 2.0
LEGACY_CENTER_BOX_PX = 200
# Ctrl+click is tested against detection frames this recent (covers LTE video delay)
DETECTION_LOOKBACK_S = 1.0
# Boxes shown in the overlay must be fresh
DETECTION_SHOW_MAX_AGE_S = 0.5


class TrackPointPayload(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    box_px: int = Field(default=BOX_PX_DEFAULT, ge=BOX_PX_MIN, le=BOX_PX_MAX)


class TrackDetectionPayload(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class ThermalPointPayload(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class AiTrackingPayload(BaseModel):
    enable: bool


class EncodingPayload(BaseModel):
    preset: str = Field(min_length=1, max_length=32)


def ai_snapshot(state: CameraState, now: float) -> dict:
    """What the video overlay needs, as plain JSON-able data."""
    track = state.track
    fresh = track is not None and now - track.received_at <= TRACK_MAX_AGE_S
    return {
        "track": {**asdict(track), "age_s": round(now - track.received_at, 2)} if fresh else None,
        "ai_select_result": state.ai_select_result,
        "ai_select_age_s": round(now - state.ai_select_at, 1) if state.ai_select_at else None,
        "ai_mode_result": state.ai_mode_result,
        "stream": {"width": state.stream_width, "height": state.stream_height},
        "video_mode": state.video_mode_name,
        "detections": _latest_detections(state, now),
        "thermal": thermal_overlay(
            state.thermal_frame, state.thermal_point, state.stream_width, state.stream_height, now,
            state.video_mode_name,
        ),
    }


def _latest_detections(state: CameraState, now: float) -> list:
    history = state.detection_history
    if not history or now - history[-1].received_at > DETECTION_SHOW_MAX_AGE_S:
        return []
    return [
        {"x0": d.x0, "y0": d.y0, "x1": d.x1, "y1": d.y1, "score": round(d.score, 2), "class_name": d.class_name}
        for d in history[-1].detections
    ]


def _post_sdp(offer: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(
        WHEP_UPSTREAM, data=offer, headers={"Content-Type": "application/sdp"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=WHEP_TIMEOUT_S) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _cancel_quietly(camera: Any) -> None:
    try:
        camera.ai_cancel_tracking()
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI tracking cancel failed: %s", exc)


def cancel_ai_tracking_async(camera: Any) -> None:
    """Fire-and-forget cancel (camera TCP may block when disconnected)."""
    threading.Thread(target=_cancel_quietly, args=(camera,), daemon=True).start()


def create_video_router(get_camera: Callable[[], Any], get_roi: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    def check_trackable(state: CameraState) -> None:
        if state.video_mode_name != "rgb":
            raise HTTPException(status_code=409, detail="AI tracking is available in RGB mode only")
        if state.stream_width <= 1 or state.stream_height <= 1:
            raise HTTPException(status_code=503, detail="stream resolution unknown (waiting for 0x20)")

    def start_tracking(nx: float, ny: float, box_px: int) -> dict:
        camera = get_camera()
        state = camera.state
        check_trackable(state)
        box = click_to_stream_box(nx, ny, state.stream_width, state.stream_height, box_px)
        get_roi().stop()  # the camera's tracker drives the gimbal from now on
        try:
            camera.set_ai_mode(True)
            time.sleep(0.1)
            camera.ai_select_box(box)
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"camera command failed: {exc}") from exc
        return {"ok": True, "box": asdict(box), "stream": {"width": state.stream_width, "height": state.stream_height}}

    @router.post("/api/ai/track-point")
    def api_track_point(payload: TrackPointPayload) -> dict:
        return start_tracking(payload.x, payload.y, payload.box_px)

    @router.post("/api/ai/track-detection")
    def api_track_detection(payload: TrackDetectionPayload) -> dict:
        """Ctrl+click: select the camera-detected object under the click (0x56 point)."""
        camera = get_camera()
        state = camera.state
        check_trackable(state)
        now = time.monotonic()
        frames = [f for f in state.detection_history if now - f.received_at <= DETECTION_LOOKBACK_S]
        if frames:
            detection = pick_detection(frames, payload.x, payload.y)
            if detection is None:
                raise HTTPException(status_code=404, detail="no detected object at that position")
            mode = "detection"
            cx, cy = detection.center
        else:
            # No 0x5F data (e.g. firmware without candidate push): send the click itself as a
            # point selection and let the camera pick the object it detects there.
            detection, mode = None, "point_at_click"
            cx, cy = payload.x, payload.y
        x, y = normalized_to_stream_point(cx, cy, state.stream_width, state.stream_height)
        get_roi().stop()
        try:
            camera.set_ai_mode(True)
            time.sleep(0.1)
            camera.ai_select_point(x, y)
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"camera command failed: {exc}") from exc
        return {
            "ok": True,
            "mode": mode,
            "detection": (
                {"class_name": detection.class_name, "score": round(detection.score, 2)} if detection else None
            ),
            "point": {"x": x, "y": y},
        }

    @router.post("/api/ai/tracking")
    def api_ai_tracking(payload: AiTrackingPayload) -> dict:
        """Legacy button: track a 200px box at the frame centre / cancel."""
        if payload.enable:
            return start_tracking(0.5, 0.5, LEGACY_CENTER_BOX_PX)
        return api_ai_cancel()

    @router.post("/api/thermal/point")
    def api_thermal_point(payload: ThermalPointPayload) -> dict:
        """Alt+click: measure the temperature at a point (0x12); the reading arrives via SSE."""
        camera = get_camera()
        state = camera.state
        if state.video_mode_name != "thermal":
            raise HTTPException(status_code=409, detail="point temperature is available in thermal view only")
        if state.stream_width <= 1 or state.stream_height <= 1:
            raise HTTPException(status_code=503, detail="stream resolution unknown (waiting for 0x20)")
        x, y = normalized_to_stream_point(payload.x, payload.y, state.stream_width, state.stream_height)
        try:
            camera.request_point_temperature(x, y)
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"camera command failed: {exc}") from exc
        return {"ok": True, "point": {"x": x, "y": y}}

    @router.post("/api/ai/cancel")
    def api_ai_cancel() -> dict:
        try:
            get_camera().ai_cancel_tracking()
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"camera command failed: {exc}") from exc
        return {"ok": True}

    @router.get("/api/ai/events")
    async def api_ai_events(request: Request) -> StreamingResponse:
        async def stream():
            last_text, last_sent = None, 0.0
            while not await request.is_disconnected():
                now = time.monotonic()
                text = json.dumps(ai_snapshot(get_camera().state, now))
                if text != last_text or now - last_sent >= SSE_KEEPALIVE_S:
                    yield f"data: {text}\n\n"
                    last_text, last_sent = text, now
                await asyncio.sleep(SSE_INTERVAL_S)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    @router.get("/api/debug/rx")
    def api_debug_rx() -> dict:
        """What the camera has sent us (to diagnose missing 0x5F / 0x50 pushes)."""
        camera = get_camera()
        state = camera.state
        return {
            **camera.rx_debug(),
            "detection_frames": len(state.detection_history),
            "firmware": state.firmware,
            "ai_mode_result": state.ai_mode_result,
            "ai_select_result": state.ai_select_result,
        }

    @router.get("/api/video/encoding")
    def api_get_encoding() -> dict:
        state = get_camera().state
        return {
            "current": asdict(state.encoding) if state.encoding else None,
            "last_set_ok": state.encoding_set_ok,
            "presets": [{"key": p.key, "label": p.label} for p in ENCODING_PRESETS],
        }

    @router.post("/api/video/encoding")
    def api_set_encoding(payload: EncodingPayload) -> dict:
        try:
            preset = find_preset(payload.preset)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=f"unknown preset: {payload.preset}") from exc
        camera = get_camera()
        try:
            camera.set_encoding_params(preset)
            time.sleep(0.3)
            camera.request_encoding_params()
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"camera command failed: {exc}") from exc
        return {"ok": True, "preset": preset.key}

    async def read_limited_body(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_SDP_BYTES:
            raise HTTPException(status_code=413, detail="SDP offer too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_SDP_BYTES:
                raise HTTPException(status_code=413, detail="SDP offer too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @router.post("/api/video/whep")
    async def api_whep(request: Request) -> Response:
        if not request.headers.get("content-type", "").startswith("application/sdp"):
            raise HTTPException(status_code=415, detail="Content-Type must be application/sdp")
        offer = await read_limited_body(request)
        try:
            status, answer = await run_in_threadpool(_post_sdp, offer)
        except (TimeoutError, socket.timeout) as exc:
            raise HTTPException(status_code=504, detail="video server did not answer (camera stream not ready?)") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise HTTPException(status_code=504, detail="video server did not answer") from exc
            logger.warning("WHEP upstream unreachable: %s", exc)
            raise HTTPException(status_code=502, detail="video server (MediaMTX) unreachable") from exc
        except OSError as exc:
            logger.warning("WHEP upstream error: %s", exc)
            raise HTTPException(status_code=502, detail="video server (MediaMTX) unreachable") from exc
        return Response(content=answer, status_code=status, media_type="application/sdp")

    return router
