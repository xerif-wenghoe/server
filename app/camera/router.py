from __future__ import annotations

import collections
import os
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from PIL import Image

from .app_config import (
    CORRECTED_DIR, EXTEND_PROBABILITY_THRESHOLD, EXTEND_THRESHOLD, FLAGGED_DIR,
    IMG_SIZE, MIN_OPPOSITE_ACTION_INTERVAL_SEC, MODEL_PATH,
    OPERATING_END_HOUR, OPERATING_START_HOUR, RETRACT_PROBABILITY_THRESHOLD,
    RETRACT_THRESHOLD, SAVE_DIR, VOTE_WINDOW, load_config, local_now,
)
from .crop_manager import apply_user_crop
from .image_processing import analyze_overexposure, correct_image
from .retention import expire_fifo_images, start_retention_worker
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
