import json
import math
import os
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import socket as _socket

from pymavlink.dialects.v20 import ardupilotmega as _ap_mavlink

from app.camera_protocol import CameraClient


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "joystick_config.json"

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

MAV_HOST = "127.0.0.1"
MAV_PORT = 14555
_mav_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
mav_conn = _ap_mavlink.MAVLink(_mav_sock, srcSystem=255, srcComponent=0, use_native=False)


class CameraIpPayload(BaseModel):
    ip: str


class VideoModePayload(BaseModel):
    mode: str


class ZoomSetPayload(BaseModel):
    zoom: float


class GimbalSpeedPayload(BaseModel):
    yaw: float = 0.0
    pitch: float = 0.0


class ZoomSpeedPayload(BaseModel):
    direction: int = 0


class GimbalRatePayload(BaseModel):
    pitch_rate: float   # deg/s, 正 = 上向き
    yaw_rate: float     # deg/s, 正 = 右向き


def background_status_loop() -> None:
    tick = 0
    while True:
        try:
            camera.request_status()
            camera.request_zoom_level()
            if tick % 10 == 0:
                camera.request_video_mode()
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
        "last_feedback": camera.state.last_feedback,
        "last_error": camera.state.last_error,
        "updated_at": camera.state.updated_at,
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
        camera.set_gimbal_speed(payload.yaw, payload.pitch)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/gimbal/center")
def api_gimbal_center() -> dict:
    try:
        camera.center_gimbal()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/gimbal/rate")
def set_gimbal_rate(payload: GimbalRatePayload) -> dict:
    try:
        pitch_rad = math.radians(payload.pitch_rate)
        yaw_rad   = math.radians(payload.yaw_rate)
        msg = mav_conn.gimbal_manager_set_pitchyaw_encode(
            target_system=1,
            target_component=1,
            flags=0,
            gimbal_device_id=0,
            pitch=float("nan"),
            yaw=float("nan"),
            pitch_rate=pitch_rad,
            yaw_rate=yaw_rad,
        )
        _mav_sock.sendto(msg.pack(mav_conn), (MAV_HOST, MAV_PORT))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/zoom/speed")
def api_zoom_speed(payload: ZoomSpeedPayload) -> dict:
    try:
        camera.zoom_speed(payload.direction)
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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
