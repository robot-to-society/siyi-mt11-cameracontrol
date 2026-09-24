import json
import os
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel

from app.camera_protocol import THERMAL_GAIN_RANGE_C, CameraClient
from app.mavlink_source import MavlinkSource
from app.roi_config import RoiConfig, load_roi_config, save_roi_config, to_control_settings
from app.roi_controller import RoiController, RoiTarget
from app.time_sync import TimeSync
from app.video_routes import cancel_ai_tracking_async, create_video_router


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "joystick_config.json"
ROI_CONFIG_PATH = BASE_DIR / "roi_config.json"
# Joystick speed below this (|yaw| or |pitch|, -100..100) is treated as stick noise while ROI is active
ROI_OVERRIDE_MIN_SPEED = 5.0

DEFAULT_CONFIG: dict = {
    "enabled": False,
    "max_pan_speed": 100.0,
    "max_tilt_speed": 100.0,
    "zoom_step_hz": 2.0,
    "axis_mappings": [
        {"axis_id": 0, "function": "pan",      "deadzone": 0.01, "invert": False, "scale": 1.0},
        {"axis_id": 1, "function": "tilt",     "deadzone": 0.01, "invert": True,  "scale": 1.0},
        {"axis_id": 3, "function": "zoom_abs", "deadzone": 0.02, "invert": False, "scale": 1.0},
    ],
    "button_mappings": [
        {"button_id": 0, "function": "shutter"},
        {"button_id": 1, "function": "thermal_toggle"},
        {"button_id": 3, "function": "center_gimbal"},
    ],
}

app = FastAPI(title="MT11 Camera Control UI")
camera = CameraClient(host="192.168.144.25", port=37260)
roi_config = load_roi_config(ROI_CONFIG_PATH)
mavlink = MavlinkSource(url=roi_config.mavlink_url)
roi = RoiController(
    camera,
    mavlink,
    settings=to_control_settings(roi_config),
)


# Camera clock follows GPS time from the FC (photo timestamps)
time_sync = TimeSync(camera, mavlink)


def _apply_roi_targets(config: RoiConfig) -> None:
    roi.set_targets(RoiTarget(**t.model_dump()) for t in config.targets)


_apply_roi_targets(roi_config)


class CameraIpPayload(BaseModel):
    ip: str


class VideoModePayload(BaseModel):
    mode: str


class ZoomSetPayload(BaseModel):
    zoom: float


class GimbalModePayload(BaseModel):
    mode: Literal["lock", "follow", "fpv"]


class GimbalSpeedPayload(BaseModel):
    yaw: float = 0.0
    pitch: float = 0.0


class ZoomSpeedPayload(BaseModel):
    direction: int = 0


class FocusPayload(BaseModel):
    direction: int = 0  # 1=far, 0=stop, -1=near


class ThermalGainPayload(BaseModel):
    gain: int  # 0=Low, 1=High


class ThermalPalettePayload(BaseModel):
    palette: int  # 0-11



class RoiStartPayload(BaseModel):
    target_id: str


def background_status_loop() -> None:
    tick = 0
    while True:
        try:
            camera.request_status()
            camera.request_zoom_level()
            if tick % 10 == 0:
                camera.request_video_mode()
            if tick % 5 == 0:
                camera.request_gimbal_mode()  # 0x19 lock / follow / fpv
            if tick % 10 == 0:
                camera.request_tf_card_info()
                camera.request_thermal_gain()  # 0x37 -> shown with its temperature range  # 0x49 SD card status / free space
            if tick % 60 == 0:
                camera.request_firmware_version()  # 0.0.0 during the camera's ~30 s boot
            if tick % 30 == 0 or camera.state.stream_width == 0:
                camera.request_encoding_params()  # every second until the resolution is known
            if tick % 5 == 0:
                camera.set_track_stream(True)  # 0x50 push; re-enabled after reconnect/reboot
                camera.set_candidate_push(True)  # 0x5F detection boxes for Ctrl+click
            tick += 1
        except Exception:  # noqa: BLE001
            try:
                camera.reconnect()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.0)


@app.on_event("startup")
def startup_event() -> None:
    try:
        camera.connect()
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=background_status_loop, daemon=True).start()
    mavlink.start()
    roi.start_background()
    time_sync.start_background()


@app.on_event("shutdown")
def shutdown_event() -> None:
    """Never leave the gimbal turning: ROI rate control keeps the last 0x07 speed otherwise."""
    roi.stop()
    roi.shutdown()
    try:
        camera.set_gimbal_speed(0, 0)
    except Exception:  # noqa: BLE001
        pass


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def get_status() -> dict:
    record_map = {
        0: "idle",
        1: "recording",
        2: "no_tf_card",
        3: "tf_card_error",
    }
    return {
        "ip": camera.host,
        "connected": camera.state.connected,
        "record_sta": camera.state.record_sta,
        "record_text": record_map.get(camera.state.record_sta, "unknown"),
        "zoom_current": camera.state.zoom_current,
        "zoom_max": camera.state.zoom_max,
        "zoom_ready": True,
        "video_mode": camera.state.video_mode_name,
        "video_mode_main": camera.state.video_mode_main,
        "video_mode_sub": camera.state.video_mode_sub,
        "stream_width": camera.state.stream_width,
        "stream_height": camera.state.stream_height,
        "last_feedback": camera.state.last_feedback,
        "last_error": camera.state.last_error,
        "updated_at": camera.state.updated_at,
        "roi": _roi_status(),
        "vehicle": _vehicle_status(),
        "time_sync": _time_sync_status(),
        "tf_card": _tf_card_status(),
        "gimbal_mode": camera.state.gimbal_mode,
        "thermal": _thermal_status(),
    }


def _thermal_status():
    gain = camera.state.thermal_gain
    if gain is None:
        return None
    low, high = THERMAL_GAIN_RANGE_C.get(gain, (None, None))
    return {"gain": gain, "range_min_c": low, "range_max_c": high}


def _tf_card_status():
    info = camera.state.tf_card
    if info is None:
        return None
    ratio = info.free_ratio
    return {
        "status": info.status,
        "filesystem": info.filesystem,
        "total_gb": info.total_gb,
        "free_gb": info.free_gb,
        "free_percent": round(ratio * 100, 1) if ratio is not None else None,
    }


def _time_sync_status() -> dict:
    st = time_sync.status()
    return {
        "synced": bool(st.last_sync_ok),
        "age_s": round(time.monotonic() - st.last_sync_at, 1) if st.last_sync_at is not None else None,
        "offset_ms": st.offset_ms,
        "rtt_ms": st.rtt_ms,
        "error": st.error,
        "camera_ack": camera.state.utc_set_ok,
    }


@app.post("/api/time/sync")
def api_time_sync() -> dict:
    time_sync.request_sync()  # performed by the background loop within ~1 s
    return {"ok": True}


def _roi_status() -> dict:
    status = roi.status()
    return {
        "active_target_id": status.active_target_id,
        "bearing_deg": status.bearing_deg,
        "elevation_deg": status.elevation_deg,
        "distance_m": status.distance_m,
        "yaw_cmd_deg": status.yaw_cmd_deg,
        "pitch_cmd_deg": status.pitch_cmd_deg,
        "mode": status.mode,
        "gimbal_yaw_deg": status.gimbal_yaw_deg,
        "gimbal_pitch_deg": status.gimbal_pitch_deg,
        "speed_yaw": status.speed_yaw,
        "speed_pitch": status.speed_pitch,
        "last_error": status.last_error,
    }


def _vehicle_status() -> dict:
    state = mavlink.latest()
    base = {"mavlink_connected": mavlink.connected, "mavlink_error": mavlink.last_error}
    if state is None:
        return {**base, "has_position": False}
    return {
        **base,
        "has_position": True,
        "lat": state.lat,
        "lon": state.lon,
        "alt_msl": state.alt_msl,
        "heading_deg": state.heading_deg,
        "age_s": round(time.monotonic() - state.received_at, 2),
    }


@app.post("/api/photo")
def trigger_photo() -> dict:
    try:
        camera.trigger_photo()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/record/start")
def start_record() -> dict:
    try:
        camera.request_status()
        time.sleep(0.12)
        camera.start_record()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/record/stop")
def stop_record() -> dict:
    try:
        camera.request_status()
        time.sleep(0.12)
        camera.stop_record()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/camera/ip")
def set_camera_ip(payload: CameraIpPayload) -> dict:
    ip = payload.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")
    camera.configure_host(ip)
    return {"ok": True, "ip": ip}


@app.post("/api/zoom/inc")
def zoom_inc() -> dict:
    try:
        camera.request_zoom_level()
        time.sleep(0.1)
        camera.zoom_in_step(1.0)
        time.sleep(0.12)
        camera.request_zoom_level()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/zoom/dec")
def zoom_dec() -> dict:
    try:
        camera.request_zoom_level()
        time.sleep(0.1)
        camera.zoom_out_step(1.0)
        time.sleep(0.12)
        camera.request_zoom_level()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/zoom/set")
def zoom_set(payload: ZoomSetPayload) -> dict:
    try:
        if payload.zoom < 1.0:
            raise HTTPException(status_code=400, detail="zoom must be >= 1.0")
        camera.set_absolute_zoom(payload.zoom)
        time.sleep(0.12)
        camera.request_zoom_level()
        return {"ok": True, "zoom": payload.zoom}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/video-mode")
def set_video_mode(payload: VideoModePayload) -> dict:
    try:
        camera.set_video_mode_preset(payload.mode)
        time.sleep(0.12)
        camera.request_video_mode()
        return {"ok": True, "mode": payload.mode}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/gimbal/speed")
def api_gimbal_speed(payload: GimbalSpeedPayload) -> dict:
    try:
        if roi.status().active_target_id is not None:
            magnitude = max(abs(payload.yaw), abs(payload.pitch))
            if 0 < magnitude < ROI_OVERRIDE_MIN_SPEED:
                return {"ok": True, "ignored": "roi_active"}
            roi.stop()  # deliberate manual input (or explicit 0/0 stop) overrides ROI
        camera.set_gimbal_speed(payload.yaw, payload.pitch)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/gimbal/center")
def api_gimbal_center() -> dict:
    try:
        roi.stop()
        camera.center_gimbal()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc



@app.post("/api/gimbal/mode")
def api_gimbal_mode(payload: GimbalModePayload) -> dict:
    # ROI computes angles for Follow (yaw body-relative, pitch horizon) and re-asserts it
    if roi.status().active_target_id is not None and payload.mode != "follow":
        raise HTTPException(status_code=409, detail="ROI中はFollow固定です（ROIを停止してから変更してください）")
    try:
        camera.set_gimbal_mode_name(payload.mode)
        time.sleep(0.1)
        camera.request_gimbal_mode()
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"camera command failed: {exc}") from exc
    return {"ok": True, "mode": payload.mode}


@app.post("/api/zoom/speed")
def api_zoom_speed(payload: ZoomSpeedPayload) -> dict:
    try:
        camera.zoom_speed(payload.direction)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/focus")
def api_focus(payload: FocusPayload) -> dict:
    try:
        camera.manual_focus(payload.direction)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/thermal/gain")
def api_thermal_gain(payload: ThermalGainPayload) -> dict:
    try:
        camera.set_thermal_gain(payload.gain)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/thermal/palette")
def api_thermal_palette(payload: ThermalPalettePayload) -> dict:
    try:
        camera.set_thermal_palette(payload.palette)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/joystick/config")
def api_get_joystick_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return DEFAULT_CONFIG


@app.post("/api/joystick/config")
def api_set_joystick_config(payload: dict = Body(...)) -> dict:
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, CONFIG_PATH)
    return {"ok": True}


@app.get("/api/roi/config")
def api_get_roi_config() -> dict:
    return roi_config.model_dump()


@app.post("/api/roi/config")
def api_set_roi_config(payload: RoiConfig) -> dict:
    global roi_config
    try:
        save_roi_config(ROI_CONFIG_PATH, payload)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to save ROI config: {exc}") from exc
    previous_url = roi_config.mavlink_url
    roi_config = payload
    roi.configure(to_control_settings(payload))
    _apply_roi_targets(payload)
    if payload.mavlink_url != previous_url:
        mavlink.restart(payload.mavlink_url)
    return {"ok": True}


@app.post("/api/roi/start")
def api_roi_start(payload: RoiStartPayload) -> dict:
    try:
        roi.start(payload.target_id)
        cancel_ai_tracking_async(camera)  # camera tracker and ROI must not both drive the gimbal
        return {"ok": True, "target_id": payload.target_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown ROI target: {payload.target_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/roi/stop")
def api_roi_stop() -> dict:
    roi.stop()
    return {"ok": True}


# Video / AI tracking APIs (lambdas so tests can swap camera/roi)
app.include_router(create_video_router(lambda: camera, lambda: roi))

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
