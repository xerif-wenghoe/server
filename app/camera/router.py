from __future__ import annotations

import collections
import os
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse, RedirectResponse
from PIL import Image

from .app_config import (
    CORRECTED_DIR, EXTEND_PROBABILITY_THRESHOLD, EXTEND_THRESHOLD, FLAGGED_DIR,
    IMG_SIZE, MIN_OPPOSITE_ACTION_INTERVAL_SEC, MODEL_PATH, CROP_TEST_DIR,
    OPERATING_END_HOUR, OPERATING_START_HOUR, RETRACT_PROBABILITY_THRESHOLD,
    RETRACT_THRESHOLD, SAVE_DIR, VOTE_WINDOW, load_config, local_now, load_crop_config, save_crop_config,
)
from .crop_manager import apply_user_crop, validate_crop
from .image_processing import analyze_overexposure, correct_image
from .retention import expire_fifo_images, start_retention_worker
from .web_ui import crop_page_html
from ..motor import motor_manager

router = APIRouter()
model = None
tf_module = None
model_lock = threading.Lock()
state_lock = threading.Lock()
prediction_history = collections.deque(maxlen=VOTE_WINDOW)
rack_state = {
    "position": "sheltered", "last_action": "hold", "last_changed": None,
    "last_changed_epoch": None, "vote_counts": {"extend": 0, "retract": 0, "uncertain": 0},
    "last_prediction": None, "last_probability": None, "total_captures": 0,
    "pending_action": "hold",
}
start_retention_worker()


def load_prediction_model():
    """Original Keras model is lazy-loaded so FastAPI itself can boot first."""
    global model, tf_module
    if model is None:
        with model_lock:
            if model is None:
                if not os.path.exists(MODEL_PATH):
                    raise FileNotFoundError(f"Model not found at {MODEL_PATH}. Set RACK_MODEL_PATH.")
                import tensorflow as tf
                tf_module = tf
                model = tf.keras.models.load_model(MODEL_PATH, compile=False)
                print(f"[CAMERA MODEL] Loaded: {MODEL_PATH}")
    return model


def get_adaptive_config(cfg: dict) -> dict:
    result = cfg.copy(); hour = local_now().hour
    if 6 <= hour < 9:
        result.update(ae_level=1, gainceiling=5, brightness=0)
    elif 9 <= hour < 16:
        result.update(ae_level=-1, gainceiling=2, brightness=-1)
    elif 16 <= hour < 18:
        result.update(ae_level=0, gainceiling=4, brightness=0)
    elif 18 <= hour < 20:
        result.update(ae_level=2, gainceiling=6, brightness=2, wb_mode=2)
    else:
        result.update(ae_level=2, gainceiling=6, brightness=2)
    return result


def predict_image(image: Image.Image):
    image = image.convert("RGB").resize(IMG_SIZE, Image.Resampling.BILINEAR)
    batch = np.asarray(image, dtype=np.float32)[None, ...]
    load_prediction_model()
    batch = tf_module.keras.applications.mobilenet_v2.preprocess_input(batch)
    with model_lock:
        probability = float(model.predict(batch, verbose=0)[0][0])
    if probability >= RETRACT_PROBABILITY_THRESHOLD: return "retract", probability
    if probability <= EXTEND_PROBABILITY_THRESHOLD: return "extend", probability
    return "uncertain", probability


def set_position(action: str, reason: str, bypass_hysteresis=False):
    target = "sheltered" if action == "retract" else "outside"
    if rack_state["position"] == target:
        rack_state["pending_action"] = "hold"; return f"{reason} Already {target}."
    last_changed = rack_state.get("last_changed_epoch")
    if not bypass_hysteresis and last_changed is not None and time.time()-last_changed < MIN_OPPOSITE_ACTION_INTERVAL_SEC:
        rack_state["pending_action"] = "hold"; return f"{reason} Opposite movement blocked by two-minute hysteresis."
    rack_state.update(position=target, last_action=action,
                      last_changed=local_now().isoformat(timespec="seconds"),
                      last_changed_epoch=time.time(), pending_action=action)
    return reason


def build_response(reason):
    return {"action": rack_state["pending_action"], "reason": reason,
            "position": rack_state["position"], "vote_counts": rack_state["vote_counts"],
            "window": list(prediction_history), "window_size": len(prediction_history),
            "last_prediction": rack_state["last_prediction"],
            "probability_retract": rack_state["last_probability"],
            "timestamp": local_now().isoformat(timespec="seconds")}


def make_rack_decision(prediction, probability):
    with state_lock:
        rack_state["last_prediction"] = prediction
        rack_state["last_probability"] = round(probability, 6)
        rack_state["total_captures"] += 1
        rack_state["pending_action"] = "hold"
        hour = local_now().hour
        if not OPERATING_START_HOUR <= hour < OPERATING_END_HOUR:
            prediction_history.clear()
            return build_response(set_position("retract", "Outside 08:00–18:00 operating hours.", True))
        prediction_history.append(prediction)
        counts = collections.Counter(prediction_history)
        rack_state["vote_counts"] = {k: counts.get(k,0) for k in ("extend","retract","uncertain")}
        if len(prediction_history) < VOTE_WINDOW:
            return build_response(f"Building decision window: {len(prediction_history)}/{VOTE_WINDOW}.")
        retract_votes, extend_votes = counts.get("retract",0), counts.get("extend",0)
        if retract_votes >= RETRACT_THRESHOLD:
            reason=set_position("retract", f"Camera window: {retract_votes}/5 confident retract votes.")
        elif extend_votes >= EXTEND_THRESHOLD:
            reason=set_position("extend", f"Camera window: {extend_votes}/5 confident extend votes.")
        else: reason="No safe majority; retaining the current rack position."
        response=build_response(reason); response["completed_window"]=list(prediction_history); return response


def process_image_bytes(data: bytes):
    expire_fifo_images()
    timestamp=local_now().strftime("%Y%m%d_%H%M%S_%f")
    filename=f"weather_{timestamp}.jpg"
    filepath=Path(SAVE_DIR)/filename; filepath.write_bytes(data)
    prediction, probability = "uncertain", 0.5
    try:
        img=Image.open(filepath).convert("RGB")
        img=apply_user_crop(img)
        total_frac, blob_frac, flagged=analyze_overexposure(img)
        img_fixed=correct_image(img)
        img_fixed.save(Path(CORRECTED_DIR)/filename, quality=90)
        if flagged: img.save(Path(FLAGGED_DIR)/filename, quality=90)
        prediction, probability=predict_image(img_fixed)
        print(f"[CAMERA] {filename}: {prediction}; P(retract)={probability:.4f}; overexposure={total_frac:.1%}/{blob_frac:.1%}")
    except Exception as exc:
        print(f"[CAMERA] Processing/inference failed; holding safely: {exc}")
    return make_rack_decision(prediction, probability)



# -----------------------------------------------------------------------------
# Original ESP CAM live crop-calibration workflow, preserved under /camera/*
# -----------------------------------------------------------------------------
device_lock = threading.Lock()
test_capture = {
    "request_id": None, "status": "idle", "filename": None, "requested_at": None
}

@router.post("/camera/crop/request")
async def request_device_test_capture():
    request_id = local_now().strftime("%Y%m%d%H%M%S%f")
    with device_lock:
        test_capture.update(request_id=request_id, status="requested", filename=None,
                            requested_at=local_now().isoformat(timespec="seconds"))
    return {"request_id": request_id, "status": "requested"}

@router.get("/camera/device/instructions")
async def device_instructions():
    with device_lock:
        if test_capture["status"] == "requested":
            return {"action": "capture_test", "request_id": test_capture["request_id"]}
    return {"action": "none"}

@router.post("/camera/crop/device-upload")
async def device_crop_upload(request_id: str = Form(...), image: UploadFile = File(...)):
    if not request_id.isdigit() or len(request_id) > 24:
        raise HTTPException(400, "Invalid request ID.")
    with device_lock:
        if request_id != test_capture.get("request_id"):
            raise HTTPException(409, "Capture request is no longer active.")
    filename = f"device_test_{request_id}.jpg"
    path = Path(CROP_TEST_DIR) / filename
    data = await image.read()
    try:
        from io import BytesIO
        im = Image.open(BytesIO(data)).convert("RGB")
        if im.width < 400 or im.height < 400:
            raise HTTPException(400, "Camera image must be at least 400 × 400.")
        im.save(path, quality=92)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"Invalid camera image: {exc}")
    with device_lock:
        test_capture.update(status="ready", filename=filename)
    expire_fifo_images()
    return {"status": "ready", "request_id": request_id}

@router.get("/camera/crop/status/{request_id}")
async def crop_capture_status(request_id: str):
    with device_lock:
        if request_id != test_capture.get("request_id"):
            raise HTTPException(404, "Unknown capture request")
        response = {"status": test_capture["status"]}
        if test_capture["status"] == "ready":
            response["image_url"] = f"/camera/crop/preview/{test_capture['filename']}"
            response["filename"] = test_capture["filename"]
        return response

@router.get("/camera/crop", response_class=HTMLResponse)
async def crop_setup_page():
    return HTMLResponse(crop_page_html(load_crop_config(), base_path="/camera"))

@router.post("/camera/crop", response_class=HTMLResponse)
async def crop_setup_save(
    source_filename: str = Form(...), rotation: float = Form(0),
    crop_x: float = Form(...), crop_y: float = Form(...),
    crop_width: float = Form(...), crop_height: float = Form(...),
):
    message, error, preview_name = "", "", None
    try:
        if (not source_filename.startswith("device_test_") or not source_filename.endswith(".jpg")
                or Path(source_filename).name != source_filename):
            raise ValueError("Capture a test image from the ESP32-CAM first.")
        image = Image.open(Path(CROP_TEST_DIR) / source_filename).convert("RGB")
        x, y, width, height = map(lambda v: int(float(v)), (crop_x, crop_y, crop_width, crop_height))
        rotated = validate_crop(image, rotation, x, y, width, height)
        cfg = {"enabled": True, "rotation": round(rotation,1), "x": x, "y": y,
               "width": width, "height": height, "source_width": rotated.width,
               "source_height": rotated.height}
        save_crop_config(cfg)
        preview_name = f"crop_preview_{local_now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        rotated.crop((x,y,x+width,y+height)).save(Path(CROP_TEST_DIR)/preview_name, quality=92)
        expire_fifo_images()
        message = "Crop saved. Future camera images will use this region."
    except (ValueError, TypeError, OSError) as exc:
        error = str(exc)
    return HTMLResponse(crop_page_html(load_crop_config(), message, error, preview_name, base_path="/camera"))

@router.post("/camera/crop/disable")
async def crop_disable():
    cfg = load_crop_config(); cfg["enabled"] = False; save_crop_config(cfg)
    return RedirectResponse(url="/camera/crop", status_code=303)

@router.get("/camera/crop/preview/{filename}")
async def crop_preview(filename: str):
    safe = Path(filename).name
    path = Path(CROP_TEST_DIR) / safe
    if not path.is_file(): raise HTTPException(404, "Image not found")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/camera", response_class=PlainTextResponse)
async def camera_upload(
    image: UploadFile = File(...),
    system_id: str = Form(default=""),
):
    """ESP #2 HTTP upload endpoint. Returns extend/retract/hold as plain text."""
    if image.content_type and not image.content_type.startswith("image/"):
        raise HTTPException(415, "Expected an image upload")
    data = await image.read()
    if not data: raise HTTPException(400, "Empty image")
    decision = await run_in_threadpool(process_image_bytes, data)
    action = decision.get("action") or "hold"

    # Central server now talks directly to always-awake ESP #3 over WS /motor.
    # Mapping: retract -> run recorded forward path; extend -> reverse recorded path.
    if system_id and action == "retract":
        await motor_manager.send_for_system(system_id, "MOVE_SHELTER")
    elif system_id and action == "extend":
        await motor_manager.send_for_system(system_id, "MOVE_OUTSIDE")
    return PlainTextResponse(action)


@router.get("/camera/config")
async def camera_config():
    return get_adaptive_config(load_config())


@router.get("/camera/status")
async def camera_status():
    with state_lock: return build_response("Current camera decision state.")


@router.get("/camera/latest")
async def camera_latest():
    images=sorted(Path(CORRECTED_DIR).glob("*.jpg"), reverse=True)
    if not images: raise HTTPException(404, "No images yet")
    return FileResponse(images[0], media_type="image/jpeg")
