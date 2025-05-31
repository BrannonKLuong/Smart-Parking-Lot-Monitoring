# backend/app/main.py (Merged Version - with Simplified WS Test Endpoint)
import os
import cv2
import asyncio
import time
import traceback
from fastapi import FastAPI, WebSocket, HTTPException, Depends, Request, Body, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles # Make sure this is imported
from typing import List, Dict, Any, Optional
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from pydantic import BaseModel, ValidationError
import base64
import numpy as np

# AWS SDK for Python
import boto3

# Database and Spot Logic
from .db import engine as db_engine, ParkingSpotConfig, VacancyEvent, DeviceToken, SessionLocal, init_db
from . import spot_logic 
from sqlmodel import Session, select

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, initialize_app as firebase_initialize_app

# Object Detection Model
try:
    from ..inference.cv_model import detect
except ImportError:
    from inference.cv_model import detect 
    print("WARN: Imported 'detect' from local 'inference' module. Ensure Docker structure matches.")

logging.basicConfig(level=logging.INFO) 
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL not set, using default SQLite for local dev.")
    DATABASE_URL = "sqlite:///./test.db" 

FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED")
FCM_VACANCY_DELAY_SECONDS = int(os.getenv("FCM_VACANCY_DELAY_SECONDS", "5"))
VIDEO_PROCESSING_FPS = int(os.getenv("VIDEO_PROCESSING_FPS", "2")) 
VIDEO_SOURCE_TYPE_ENV = os.getenv("VIDEO_SOURCE_TYPE", "FILE").upper()
VIDEO_SOURCE = os.getenv("VIDEO_SOURCE") 

DB_INIT_MAX_RETRIES = 15
DB_INIT_RETRY_DELAY = 5

firebase_app_initialized = False
if FIREBASE_CRED_PATH:
    if os.path.exists(FIREBASE_CRED_PATH):
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CRED_PATH)
                firebase_initialize_app(cred)
                logger.info("Firebase app initialized successfully.")
                firebase_app_initialized = True
            else:
                logger.info("Firebase app already initialized.")
                firebase_app_initialized = True
        except Exception as e:
            logger.error(f"Error initializing Firebase Admin SDK: {e}", exc_info=True)
    else:
        logger.warning(f"Firebase credentials file not found at path: {FIREBASE_CRED_PATH}. FCM notifications will be disabled.")
else:
    logger.warning("FIREBASE_CRED environment variable not set. FCM notifications will be disabled.")

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent # This would be /app if WORKDIR is /app and app code is in /app/app

app = FastAPI(debug=True) 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SpotConfigIn(BaseModel):
    id: str; x: int; y: int; w: int; h: int
class SpotsUpdateRequest(BaseModel):
    spots: List[SpotConfigIn]
class TokenRegistration(BaseModel): 
    token: str; platform: str = "android"

video_processing_active = False
video_capture_global: Optional[cv2.VideoCapture] = None
previous_spot_states_global: Dict[str, bool] = {}
latest_frame_with_all_overlays: Optional[Any] = None 
frame_access_lock = asyncio.Lock() 
empty_start: Dict[str, Optional[datetime]] = {} 
notified: Dict[str, bool] = {} 
event_queue: Optional[asyncio.Queue] = None 
video_frame_queue: asyncio.Queue = asyncio.Queue(maxsize=10) 

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock: self.active_connections.append(websocket)
        logger.info(f"WebSocket connection established for spot updates: {websocket.client}")
    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections: self.active_connections.remove(websocket)
        logger.info(f"WebSocket connection closed for spot updates: {websocket.client}")
    async def broadcast(self, data: Dict[str, Any]):
        message_str = json.dumps(data, default=str)
        connections_to_remove = []
        current_connections = []
        async with self._lock: current_connections = list(self.active_connections)
        for connection in current_connections:
            try: await connection.send_text(message_str)
            except Exception as e:
                logger.error(f"Error broadcasting to WebSocket {connection.client}: {e}. Marking for removal.")
                connections_to_remove.append(connection)
        if connections_to_remove:
            async with self._lock:
                for ws_to_remove in connections_to_remove:
                    if ws_to_remove in self.active_connections: self.active_connections.remove(ws_to_remove)
manager = ConnectionManager()

def enqueue_event(event_data: Dict[str, Any]):
    global event_queue
    if event_queue is not None:
        try: event_queue.put_nowait(event_data)
        except asyncio.QueueFull: logger.warning(f"Event queue full. Dropping event: {event_data.get('type')}")
        except Exception as e: logger.error(f"Error enqueuing event: {e}")
    else: logger.warning("Event queue not initialized. Cannot enqueue event.")

async def event_processor_task():
    global event_queue
    if event_queue is None: logger.error("Event queue not initialized. Exiting task."); return
    logger.info("Event processor task started for broadcasting spot updates.")
    while True:
        try:
            message_data = await event_queue.get()
            await manager.broadcast(message_data)
            event_queue.task_done()
        except asyncio.CancelledError: logger.info("Event processor task cancelled."); break
        except Exception as e: logger.error(f"Error in event processor task: {e}", exc_info=True); await asyncio.sleep(1)

def get_kvs_hls_url(stream_name_or_arn, region_name=os.getenv("AWS_REGION", "us-east-2")):
    try:
        logger.info(f"Attempting to get HLS URL for KVS stream: {stream_name_or_arn} in region {region_name}")
        kvs_client = boto3.client('kinesisvideo', region_name=region_name)
        endpoint_params = {'StreamName': stream_name_or_arn, 'APIName': 'GET_HLS_STREAMING_SESSION_URL'}
        hls_params = {'StreamName': stream_name_or_arn, 'PlaybackMode': 'LIVE'}
        data_endpoint_response = kvs_client.get_data_endpoint(**endpoint_params)
        data_endpoint = data_endpoint_response['DataEndpoint']
        kvs_media_client = boto3.client('kinesis-video-archived-media', endpoint_url=data_endpoint, region_name=region_name)
        hls_params.update({'ContainerFormat': 'MPEG_TS', 'DiscontinuityMode': 'ALWAYS', 'DisplayFragmentTimestamp': 'ALWAYS', 'Expires': 300})
        hls_url = kvs_media_client.get_hls_streaming_session_url(**hls_params)['HLSStreamingSessionURL']
        logger.info(f"Successfully obtained KVS HLS URL.")
        return hls_url
    except Exception as e: logger.error(f"Error getting KVS HLS URL for '{stream_name_or_arn}': {e}", exc_info=True); return None

def make_capture():
    global video_capture_global
    env_video_source_value = VIDEO_SOURCE
    logger.info(f"--- make_capture called. Video Source Type: '{VIDEO_SOURCE_TYPE_ENV}', Value: '{env_video_source_value}' ---")
    if VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM": logger.info("make_capture: Mode is WEBSOCKET_STREAM."); return None
    cap = None; source_path_for_opencv = env_video_source_value
    if VIDEO_SOURCE_TYPE_ENV == "KVS_STREAM":
        if not env_video_source_value: raise RuntimeError("VIDEO_SOURCE (KVS stream name) not set.")
        aws_region = os.getenv("AWS_REGION", "us-east-2")
        hls_url = get_kvs_hls_url(stream_name_or_arn=env_video_source_value, region_name=aws_region)
        if hls_url: os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|analyzeduration;2000000|probesize;1000000"; cap = cv2.VideoCapture(hls_url, cv2.CAP_FFMPEG); source_path_for_opencv = hls_url
        else: raise RuntimeError(f"Failed to get KVS HLS URL for stream: {env_video_source_value}")
    elif VIDEO_SOURCE_TYPE_ENV == "FILE":
        # In Docker, this path will be relative to /app as per Dockerfile COPY
        default_video_file = str(APP_DIR.parent / "videos" / "test_video.mov") # APP_DIR is /app/app, so APP_DIR.parent is /app
        if not env_video_source_value: env_video_source_value = default_video_file; logger.info(f"Defaulting to: {default_video_file}")
        source_path_for_opencv = env_video_source_value
        if not os.path.exists(source_path_for_opencv): raise RuntimeError(f"Video file not found: {source_path_for_opencv}")
        cap = cv2.VideoCapture(source_path_for_opencv)
    elif VIDEO_SOURCE_TYPE_ENV == "WEBCAM_INDEX":
        try: idx = int(env_video_source_value if env_video_source_value is not None else "0"); cap = cv2.VideoCapture(idx); source_path_for_opencv = f"Webcam Index {idx}"
        except ValueError: raise RuntimeError(f"Invalid webcam index: {env_video_source_value}")
    elif env_video_source_value: cap = cv2.VideoCapture(env_video_source_value, cv2.CAP_FFMPEG)
    else: raise RuntimeError(f"VIDEO_SOURCE not set or invalid VIDEO_SOURCE_TYPE: {VIDEO_SOURCE_TYPE_ENV}")
    if cap is None or not cap.isOpened(): raise RuntimeError(f"FATAL: Could not open video source. Type='{VIDEO_SOURCE_TYPE_ENV}', Source='{source_path_for_opencv}'")
    logger.info(f"Successfully opened video source: {source_path_for_opencv}"); video_capture_global = cap; return cap

async def video_processor():
    global video_processing_active, video_capture_global, previous_spot_states_global, latest_frame_with_all_overlays, frame_access_lock, empty_start, notified, video_frame_queue
    cap = None; is_websocket_stream_mode = (VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM")
    if not is_websocket_stream_mode:
        try: logger.info("video_processor: Initializing video capture..."); cap = make_capture()
        except RuntimeError as e: logger.error(f"video_processor: CRITICAL - Failed to init video capture: {e}", exc_info=True); video_processing_active = False; await manager.broadcast({"type": "video_error", "data": {"error": "Video source failed on startup", "detail": str(e)}}); return
    else: logger.info("video_processor: Mode is WEBSOCKET_STREAM.")
    video_processing_active = True; VACANCY_DELAY = timedelta(seconds=FCM_VACANCY_DELAY_SECONDS); loop = asyncio.get_event_loop()
    frame_count = 0; source_fps = cap.get(cv2.CAP_PROP_FPS) if cap and not is_websocket_stream_mode else 0
    frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS) if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps else 0
    logger.info(f"Source FPS: {source_fps}, Target Processing FPS: {VIDEO_PROCESSING_FPS}, Skip: {frame_skip_interval}")
    while video_processing_active:
        frame = None; ret = False; processing_start_time = time.perf_counter()
        if is_websocket_stream_mode:
            try:
                frame_data_url = await asyncio.wait_for(video_frame_queue.get(), timeout=1.0)
                if frame_data_url.startswith('data:image/jpeg;base64,'):
                    img_bytes = base64.b64decode(frame_data_url.split(',', 1)[1]); np_arr = np.frombuffer(img_bytes, np.uint8); frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame is not None: ret = True; video_frame_queue.task_done()
                    else: logger.warning("WS: Failed to decode frame.")
                else: logger.warning(f"WS: Received non-JPEG base64: {frame_data_url[:100]}")
            except asyncio.TimeoutError: await asyncio.sleep(0.01); continue
            except Exception as e_ws: logger.error(f"WS: Error processing frame: {e_ws}", exc_info=True); await asyncio.sleep(0.1); continue
        else:
            if cap is None or not cap.isOpened():
                logger.warning("video_processor: Capture un-opened. Re-initializing...");
                try:
                    if cap: cap.release(); cap = make_capture()
                    if not cap: video_processing_active = False; break
                    source_fps = cap.get(cv2.CAP_PROP_FPS) if cap else 0; frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS) if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps else 0; frame_count = 0
                except Exception as e_re: logger.error(f"video_processor: Failed to re-init: {e_re}. Stopping.", exc_info=True); video_processing_active = False; await manager.broadcast({"type": "video_error", "data": {"error": "Video source lost", "detail": str(e_re)}}); break
            if cap: ret, frame = cap.read()
        if not ret or frame is None:
            if not is_websocket_stream_mode and video_processing_active:
                logger.warning("video_processor: Failed to grab frame. Reopening."); await asyncio.sleep(2)
                try:
                    if cap: cap.release(); cap = make_capture()
                    if not (cap and cap.isOpened()): logger.error("video_processor: Failed to re-open. Stopping."); video_processing_active = False; break
                except Exception as e_re2: logger.error(f"video_processor: Error re-opening: {e_re2}. Stopping.", exc_info=True); video_processing_active = False; break
            elif is_websocket_stream_mode: await asyncio.sleep(0.01)
            continue
        frame_count += 1
        if not is_websocket_stream_mode and frame_skip_interval > 0 and frame_count % (frame_skip_interval + 1) != 0: await asyncio.sleep(0.001); continue
        yolo_results = None
        try: yolo_results = await loop.run_in_executor(None, detect, frame.copy())
        except Exception as e_det: logger.error(f"video_processor: YOLO detection error: {e_det}", exc_info=True)
        current_detected_occupancy = spot_logic.get_spot_states(yolo_results if yolo_results else [])
        now = datetime.utcnow(); active_spot_labels = set(spot_logic.SPOTS.keys())
        for sid in active_spot_labels:
            if sid not in previous_spot_states_global: previous_spot_states_global[sid] = False; empty_start[sid] = now; notified[sid] = False
        for sid in list(previous_spot_states_global.keys()):
            if sid not in active_spot_labels: previous_spot_states_global.pop(sid,None); empty_start.pop(sid,None); notified.pop(sid,None)
        for spot_id in active_spot_labels:
            was_occ = previous_spot_states_global.get(spot_id, False); is_now_occ = current_detected_occupancy.get(spot_id, False)
            if was_occ != is_now_occ:
                enqueue_event({"type": "spot_update", "data": {"spot_id": spot_id, "timestamp": now.isoformat()+"Z", "status": "occupied" if is_now_occ else "free"}})
                if is_now_occ: empty_start[spot_id] = None
                else: empty_start[spot_id] = now; notified[spot_id] = False
            if not is_now_occ and empty_start.get(spot_id) and not notified.get(spot_id, False):
                if (now - empty_start[spot_id]) >= VACANCY_DELAY:
                    try:
                        spot_id_int = int(spot_id)
                        await loop.run_in_executor(None, notify_users_for_spot_vacancy, spot_id_int); notified[spot_id] = True
                        with Session(db_engine) as s_db: s_db.add(VacancyEvent(timestamp=now, spot_id=spot_id_int, camera_id="default_camera")); s_db.commit()
                    except Exception as e_fcm_db: logger.error(f"FCM/DB log error for spot {spot_id}: {e_fcm_db}", exc_info=True)
            previous_spot_states_global[spot_id] = is_now_occ
        frame_to_display = frame.copy()
        for lbl, coords in spot_logic.SPOTS.items():
            if not (isinstance(coords, tuple) and len(coords) == 4): continue
            sx,sy,sw,sh = coords; is_occ = previous_spot_states_global.get(lbl,False); color = (0,0,255) if is_occ else (0,255,0)
            cv2.rectangle(frame_to_display, (sx,sy), (sx+sw, sy+sh), color, 2); cv2.putText(frame_to_display, lbl, (sx,sy-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color,1)
        if yolo_results:
            for res in yolo_results:
                if hasattr(res, 'boxes') and res.boxes is not None:
                    v_classes = {"car","truck","bus","motorbike","bicycle"}; b_coords = res.boxes.xyxy.tolist(); c_indices = res.boxes.cls.tolist(); c_names = res.names
                    for i, box_xyxy in enumerate(b_coords):
                        if c_names.get(int(c_indices[i])) in v_classes: cv2.rectangle(frame_to_display, (int(box_xyxy[0]),int(box_xyxy[1])), (int(box_xyxy[2]),int(box_xyxy[3])), (0,255,255),1)
        async with frame_access_lock: latest_frame_with_all_overlays = frame_to_display
        current_frame_processing_time = time.perf_counter() - processing_start_time
        logger.info(f"Frame processed in {current_frame_processing_time:.4f} seconds.")
        sleep_duration = (1.0 / VIDEO_PROCESSING_FPS) - current_frame_processing_time
        if sleep_duration > 0: await asyncio.sleep(sleep_duration)
        else: await asyncio.sleep(0.001)
    logger.info("video_processor: Loop stopped."); await manager.broadcast({"type": "video_ended", "data": {"message": "Video processing stopped."}})

def notify_users_for_spot_vacancy(spot_id_int: int):
    logger.info(f"Attempting FCM for spot: {spot_id_int}")
    if not firebase_app_initialized: logger.warning("Firebase not init. Skipping FCM."); return
    try:
        with Session(db_engine) as s:
            tokens = [r.token for r in s.exec(select(DeviceToken)).all() if r.token]
            if not tokens: logger.info("No FCM tokens found."); return
            msg = firebase_admin.messaging.MulticastMessage(notification=firebase_admin.messaging.Notification(title="Parking Spot Available!", body=f"Spot {spot_id_int} is now free."), tokens=tokens)
            resp = firebase_admin.messaging.send_multicast(msg); logger.info(f'{resp.success_count} FCMs sent for spot {spot_id_int}')
            # Log failures if any (omitted for brevity but important for prod)
    except Exception as e: logger.error(f"FCM error for spot {spot_id_int}: {e}", exc_info=True)

@app.on_event("startup")
async def startup_event():
    global event_queue, previous_spot_states_global, empty_start, notified, video_processing_active
    logger.info("MERGED: Startup - Robust DB Init...")
    db_ok = False
    for attempt in range(DB_INIT_MAX_RETRIES):
        try:
            logger.info(f"DB init attempt {attempt+1}/{DB_INIT_MAX_RETRIES}...")
            with db_engine.connect() as conn: conn.execute(select(1))
            init_db(); logger.info("init_db() called.")
            with Session(db_engine) as s: s.exec(select(ParkingSpotConfig).limit(1)).first() # Verify
            db_ok = True; logger.info("DB init successful."); break
        except Exception as e:
            logger.error(f"DB init attempt {attempt+1} error: {e}", exc_info=True if attempt == DB_INIT_MAX_RETRIES -1 else False)
            if attempt < DB_INIT_MAX_RETRIES - 1: await asyncio.sleep(DB_INIT_RETRY_DELAY)
            else: logger.critical("Max DB init retries reached.")
    if db_ok:
        try:
            spot_logic.refresh_spots(); logger.info(f"Spots loaded: {len(spot_logic.SPOTS)}")
            now = datetime.utcnow()
            for sid in spot_logic.SPOTS.keys(): previous_spot_states_global.setdefault(str(sid),False); empty_start.setdefault(str(sid),now); notified.setdefault(str(sid),False)
            logger.info(f"Global spot states initialized for {len(previous_spot_states_global)} spots.")
        except Exception as e_sl: logger.error(f"Error init spot_logic/globals: {e_sl}", exc_info=True)
    else: logger.critical("DB FAILED TO INITIALIZE. App state may be inconsistent.")
    event_queue = asyncio.Queue(200); logger.info("Event queue initialized.")
    asyncio.create_task(event_processor_task()); logger.info("Event processor task scheduled.")
    if not video_processing_active: logger.info("Creating video_processor task..."); asyncio.create_task(video_processor())
    logger.info("MERGED: Startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    global video_processing_active, video_capture_global
    logger.info("MERGED: Shutdown initiated..."); video_processing_active = False
    if video_capture_global: video_capture_global.release(); video_capture_global = None
    await asyncio.sleep(0.5); logger.info("MERGED: Shutdown complete.")

@app.get("/")
async def root(): return {"message": "Smart Parking API (Merged) is running"}

@app.get("/api/spots_v10_get")
async def get_spots_config_v2():
    logger.info("GET /api/spots_v10_get hit.")
    spots_data = []
    try:
        spot_logic.refresh_spots(); logger.info(f"Refreshed spots_v10_get. Count: {len(spot_logic.SPOTS)}")
        if not spot_logic.SPOTS: return {"spots": []}
        for sid, coords in spot_logic.SPOTS.items():
            s_id_str = str(sid)
            if isinstance(coords,tuple) and len(coords)==4: spots_data.append({"id":s_id_str, "x":coords[0],"y":coords[1],"w":coords[2],"h":coords[3], "is_available": not previous_spot_states_global.get(s_id_str,False)})
            else: logger.warning(f"Malformed coords for spot {s_id_str}: {coords}")
        return {"spots": spots_data}
    except Exception as e: logger.error(f"GET /api/spots_v10_get error: {e}", exc_info=True); raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/nuke_test_save")
async def save_spots_config_v2(payload: SpotsUpdateRequest):
    logger.info(f"POST /api/nuke_test_save: {payload.model_dump_json(indent=2)}")
    default_cam_id = "default_camera"
    with Session(db_engine) as db:
        try:
            existing_cfgs = db.exec(select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == default_cam_id)).all()
            existing_map = {str(cfg.spot_label): cfg for cfg in existing_cfgs if hasattr(cfg, 'spot_label')}
            incoming_labels = set(); resp_spots = []
            for spot_in in payload.spots:
                lbl = str(spot_in.id); incoming_labels.add(lbl)
                if lbl in existing_map:
                    cfg_upd = existing_map[lbl]; cfg_upd.x_coord,cfg_upd.y_coord,cfg_upd.width,cfg_upd.height = spot_in.x,spot_in.y,spot_in.w,spot_in.h; cfg_upd.updated_at = datetime.utcnow(); db.add(cfg_upd)
                else: db.add(ParkingSpotConfig(spot_label=lbl,camera_id=default_cam_id,x_coord=spot_in.x,y_coord=spot_in.y,width=spot_in.w,height=spot_in.h))
                resp_spots.append(spot_in.model_dump())
            for ex_lbl, cfg_del in existing_map.items():
                if ex_lbl not in incoming_labels: db.delete(cfg_del)
            db.commit(); logger.info("Spots saved to DB.")
            spot_logic.refresh_spots()
            current_db_ids = set(spot_logic.SPOTS.keys()); now_new = datetime.utcnow()
            for sid_g in list(previous_spot_states_global.keys()):
                if str(sid_g) not in current_db_ids: previous_spot_states_global.pop(str(sid_g),None); empty_start.pop(str(sid_g),None); notified.pop(str(sid_g),None)
            for sid_db in current_db_ids:
                s_id_db = str(sid_db)
                if s_id_db not in previous_spot_states_global: previous_spot_states_global[s_id_db]=False; empty_start[s_id_db]=now_new; notified[s_id_db]=False
            
            # Prepare data for spots_config_updated event
            current_spots_for_event_data = [{"id": l, "x":c[0], "y":c[1], "w":c[2], "h":c[3]} for l,c in spot_logic.SPOTS.items() if isinstance(c,tuple) and len(c)==4]
            enqueue_event({"type": "spots_config_updated", "data": {"spots": current_spots_for_event_data}})
            return {"message": "Spots saved to DB successfully!", "spots": resp_spots} # Matched to App.js V11.18 expectation
        except Exception as e: db.rollback(); logger.error(f"POST /api/nuke_test_save error: {e}", exc_info=True); raise HTTPException(status_code=500, detail=str(e))

@app.get("/webcam_feed")
async def mjpeg_webcam_feed():
    async def generate_mjpeg_frames():
        global latest_frame_with_all_overlays, frame_access_lock
        while True:
            frame_to_send = None
            async with frame_access_lock:
                if latest_frame_with_all_overlays is not None: frame_to_send = latest_frame_with_all_overlays.copy()
            if frame_to_send is not None:
                try:
                    flag, enc_img = cv2.imencode(".jpg", frame_to_send)
                    if not flag: await asyncio.sleep(0.1); continue
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + bytearray(enc_img) + b'\r\n')
                except Exception as e_enc: logger.error(f"MJPEG encode error: {e_enc}", exc_info=True); await asyncio.sleep(0.1); continue
            await asyncio.sleep(1.0 / 25) # Target ~25 FPS for MJPEG stream
    return StreamingResponse(generate_mjpeg_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/spots")
async def websocket_spots_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        current_statuses = {}
        spot_logic.refresh_spots()
        for lbl, coords in spot_logic.SPOTS.items():
            if isinstance(coords,tuple) and len(coords)==4: current_statuses[lbl] = {"status":"occupied" if previous_spot_states_global.get(lbl,False) else "free", "timestamp":(empty_start.get(lbl) or datetime.utcnow()).isoformat()+"Z", "x":coords[0],"y":coords[1],"w":coords[2],"h":coords[3]}
        await websocket.send_text(json.dumps({"type":"all_spot_statuses", "data":current_statuses, "timestamp":time.time()},default=str))
        while True:
            try: await asyncio.wait_for(websocket.receive_text(), timeout=60)
            except asyncio.TimeoutError: await websocket.send_text(json.dumps({"type":"ping"}))
            except WebSocketDisconnect: logger.info(f"WS client {websocket.client} disconnected from /ws/spots."); break
            except Exception as e_ws_recv: logger.error(f"Error /ws/spots recv loop for {websocket.client}: {e_ws_recv}", exc_info=True); break
    except Exception as e: logger.error(f"WS error /ws/spots for {websocket.client}: {e}", exc_info=True)
    finally: await manager.disconnect(websocket)

# --- MODIFIED FOR TESTING - Original logic commented out ---
@app.websocket("/ws/video_stream_upload")
async def websocket_video_upload_endpoint(websocket: WebSocket): # Renamed from _test to be the active one
    global video_frame_queue # Ensure this is the queue used by video_processor
    await websocket.accept()
    logger.info(f"Client connected to /ws/video_stream_upload (PRODUCTION version): {websocket.client}")
    try:
        while True:
            data = await websocket.receive_text() # Expecting base64 encoded JPEG data URL

            if VIDEO_SOURCE_TYPE_ENV != "WEBSOCKET_STREAM":
                logger.warning(f"Received frame from {websocket.client} via WS, but backend VIDEO_SOURCE_TYPE is '{VIDEO_SOURCE_TYPE_ENV}'. Frame ignored by production endpoint.")
                # Optionally send a message back to client?
                # await websocket.send_text(json.dumps({"type": "error", "message": "Backend not in WEBSOCKET_STREAM mode."}))
                await asyncio.sleep(0.1) 
                continue 

            if video_frame_queue.full():
                try:
                    discarded_frame = video_frame_queue.get_nowait()
                    video_frame_queue.task_done()
                    # logger.info(f"video_frame_queue was full. Discarded oldest frame.") # Can be verbose
                except asyncio.QueueEmpty:
                    pass 
            
            await video_frame_queue.put(data)
            # logger.debug(f"Frame PUT onto video_frame_queue. Queue size: {video_frame_queue.qsize()}")

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from /ws/video_stream_upload (PRODUCTION version): {websocket.client}")
    except Exception as e:
        logger.error(f"Error in /ws/video_stream_upload (PRODUCTION version) for {websocket.client}: {e}", exc_info=True)
    finally:
        logger.info(f"Closing WebSocket connection for /ws/video_stream_upload (PRODUCTION version): {websocket.client}")

@app.post("/api/register_fcm_token")
async def register_fcm_token_api(payload: TokenRegistration, db: Session = Depends(SessionLocal)):
    logger.info(f"Register FCM token: {payload.token[:20]}...")
    if not payload.token: raise HTTPException(status_code=400, detail="FCM token not provided")
    try:
        if db.exec(select(DeviceToken).where(DeviceToken.token == payload.token)).first(): return {"message": "Token already registered."}
        db.add(DeviceToken(token=payload.token, platform=payload.platform)); db.commit()
        # db.refresh(new_token_record) # Not strictly needed if not using its ID right away
        return {"message": "Token registered successfully."}
    except Exception as e: db.rollback(); logger.error(f"FCM token reg error {payload.token[:20]}: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Failed to register token.")

@app.get("/api/spots")
async def old_get_spots_config_api(): raise HTTPException(status_code=410, detail="Deprecated. Use GET /api/spots_v10_get.")
@app.post("/api/spots")
async def old_post_spots_config_api(): raise HTTPException(status_code=410, detail="Deprecated. Use POST /api/nuke_test_save.")

# --- Serve React Frontend ---
# Ensure this path is correct based on your Dockerfile COPY command for the frontend build
# If WORKDIR is /app and frontend_build is at /app/frontend_build
STATIC_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend_build"

app.mount(
    "/static", # URL path for assets like CSS, JS from React build
    StaticFiles(directory= STATIC_FRONTEND_DIR / "static"), 
    name="react_static_assets"
)

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_react_app(request: Request, full_path: str):
    index_html_path = STATIC_FRONTEND_DIR / "index.html"
    api_prefixes = ("/api/", "/ws/", "/webcam_feed") # Add other backend-specific paths if any
    
    if any(request.url.path.startswith(prefix) for prefix in api_prefixes):
        # This case should ideally be hit if a specific API route wasn't found by FastAPI router.
        # However, with path parameters, this check helps clarify intent.
        logger.warning(f"Path '{request.url.path}' matches API prefix but no specific route found. Returning 404 for API.")
        raise HTTPException(status_code=404, detail="API Endpoint not found.")

    if not index_html_path.exists():
        logger.error(f"Frontend index.html not found at {index_html_path}")
        raise HTTPException(status_code=500, detail="Frontend not found (index.html missing).")
    
    return FileResponse(index_html_path)


if __name__ == "__main__":
    logger.info("Starting Uvicorn server for local development (Merged Version)...")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)