import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.camera_protocol import CameraClient


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="MT11 Camera Control UI")
camera = CameraClient(host="192.168.144.25", port=37260)


class CameraIpPayload(BaseModel):
    ip: str


class VideoModePayload(BaseModel):
    mode: str


class ZoomSetPayload(BaseModel):
    zoom: float


def background_status_loop() -> None:
    tick = 0
    while True:
        try:
            camera.request_status()
            camera.request_zoom_level()
            if camera.state.zoom_max is None or tick % 10 == 0:
                camera.request_zoom_range()
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
        "zoom_ready": camera.state.zoom_max is not None,
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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
