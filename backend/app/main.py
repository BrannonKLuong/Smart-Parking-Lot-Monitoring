# backend/app/main.py (Full Logic, KVS Removed, Deferred Init)
import os
import cv2
import asyncio
import time
import traceback
from fastapi import FastAPI, WebSocket, HTTPException, Depends, Request, Body, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from typing import List, Dict, Any, Optional
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from pydantic import BaseModel, ValidationError
import base64
import numpy as np

# AWS SDK for Python (boto3 is kept for any other potential AWS interactions, but KVS calls removed)
import boto3 

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, initialize_app as firebase_initialize_app

# SQLModel and DB components (will be fully imported in initialize_application_dependencies)
from sqlmodel import Session, select 

# --- Basic Logging First ---
logging.basicConfig(level=logging.INFO) 
logger = logging.getLogger(__name__)
logger.info("MAIN.PY: Python script execution started. Basic logging configured.")

# --- FastAPI App Definition (Early) ---
app = FastAPI(debug=True) 
logger.info("MAIN.PY: FastAPI app instance created.")

# --- Environment Variable Loading (Early) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
    logger.info("MAIN.PY: python-dotenv load_dotenv() called.")
except ImportError:
    logger.info("MAIN.PY: python-dotenv not found, skipping load_dotenv(). Relying on system env vars.")

DATABASE_URL_ENV = os.getenv("DATABASE_URL")
FIREBASE_CRED_PATH_ENV = os.getenv("FIREBASE_CRED") 
FCM_VACANCY_DELAY_SECONDS_ENV = int(os.getenv("FCM_VACANCY_DELAY_SECONDS", "5"))
VIDEO_PROCESSING_FPS_ENV = int(os.getenv("VIDEO_PROCESSING_FPS", "5")) 
VIDEO_SOURCE_TYPE_ENV_VAL = os.getenv("VIDEO_SOURCE_TYPE", "WEBSOCKET_STREAM").upper()
VIDEO_SOURCE_ENV = os.getenv("VIDEO_SOURCE")
LOG_LEVEL_ENV = os.getenv("LOG_LEVEL", "INFO").upper()
AWS_REGION_ENV = os.getenv("AWS_REGION", "us-east-2") # Retained for general AWS SDK use if any

if LOG_LEVEL_ENV == "DEBUG":
    logging.getLogger().setLevel(logging.DEBUG)
    for handler in logging.getLogger().handlers: handler.setLevel(logging.DEBUG)
    logger.info("MAIN.PY: Log level set to DEBUG.")
else:
    logger.info(f"MAIN.PY: Log level set to {LOG_LEVEL_ENV}.")

logger.info(f"APP_RUNNER_ENV_CHECK: DATABASE_URL is {'SET' if DATABASE_URL_ENV else 'NOT SET'}")
logger.info(f"APP_RUNNER_ENV_CHECK: FIREBASE_CRED_PATH (from env var FIREBASE_CRED) is '{FIREBASE_CRED_PATH_ENV}'")
logger.info(f"APP_RUNNER_ENV_CHECK: VIDEO_SOURCE_TYPE is '{VIDEO_SOURCE_TYPE_ENV_VAL}'")
logger.info(f"APP_RUNNER_ENV_CHECK: VIDEO_SOURCE is '{VIDEO_SOURCE_ENV}'")

DB_INIT_MAX_RETRIES = 15
DB_INIT_RETRY_DELAY = 5

# --- Health Check Endpoint (Very Early) ---
@app.get("/")
async def root_health_check():
    logger.info("HEALTH_CHECK: Root path / accessed and responding OK.")
    return {"message": "Smart Parking API (Deferred Init - KVS Removed) is running and healthy"}

# --- Module Placeholders ---
db_module = None
spot_logic_module = None
cv_model_module = None 
ParkingSpotConfig_model = None
VacancyEvent_model = None
DeviceToken_model = None
SessionLocal_db = None 
db_engine_global = None
firebase_app_initialized_flag = False

# --- Pydantic Models ---
class SpotConfigIn(BaseModel): id: str; x: int; y: int; w: int; h: int
class SpotsUpdateRequest(BaseModel): spots: List[SpotConfigIn]
class TokenRegistration(BaseModel): token: str; platform: str = "android"

# --- Global States ---
video_processing_active = False
video_capture_global: Optional[cv2.VideoCapture] = None
previous_spot_states_global: Dict[str, bool] = {}
latest_frame_with_all_overlays: Optional[Any] = None 
frame_access_lock = asyncio.Lock() 
empty_start: Dict[str, Optional[datetime]] = {} 
notified: Dict[str, bool] = {} 
event_queue: Optional[asyncio.Queue] = None 
video_frame_queue: asyncio.Queue = asyncio.Queue(maxsize=10) 

APP_DIR_PATH = Path(__file__).resolve().parent 

# --- Connection Manager ---
class ConnectionManager:
    def __init__(self): 
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()
    async def connect(self, ws: WebSocket): 
        await ws.accept()
        async with self._lock: 
            self.active_connections.append(ws)
        logger.info(f"WS-Spots: Connected {ws.client}")
    async def disconnect(self, ws: WebSocket): 
        async with self._lock: 
            if ws in self.active_connections:
                self.active_connections.remove(ws)
        logger.info(f"WS-Spots: Disconnected {ws.client}")
    async def broadcast(self, data: Dict[str,Any]): 
        msg = json.dumps(data,default=str)
        bad_conns=[]; cur_conns=[]
        async with self._lock: cur_conns=list(self.active_connections)
        for c in cur_conns: 
            try: await c.send_text(msg)
            except Exception: bad_conns.append(c)
        if bad_conns: 
            async with self._lock:
                for bc in bad_conns: 
                    if bc in self.active_connections: self.active_connections.remove(bc)
manager = ConnectionManager()
logger.info("MAIN.PY: ConnectionManager initialized.")

def enqueue_event(event_data: Dict[str, Any]):
    global event_queue
    if event_queue: 
        try: event_queue.put_nowait(event_data)
        except asyncio.QueueFull: logger.warning(f"Event queue full. Dropping: {event_data.get('type')}")
    else: logger.warning("Event queue not init for enqueue_event.")

# --- Deferred Initialization ---
async def initialize_application_dependencies():
    global db_module, spot_logic_module, cv_model_module, firebase_app_initialized_flag
    global ParkingSpotConfig_model, VacancyEvent_model, DeviceToken_model, SessionLocal_db, db_engine_global
    global event_queue, video_processing_active
    global previous_spot_states_global, empty_start, notified

    logger.info("INIT_DEPS: Starting asynchronous initialization...")
    try:
        from . import db as app_db
        from . import spot_logic as app_spot_logic
        try: from inference import cv_model as app_cv_model 
        except ImportError: from ..inference import cv_model as app_cv_model 
        
        db_module = app_db
        spot_logic_module = app_spot_logic
        cv_model_module = app_cv_model
        ParkingSpotConfig_model = db_module.ParkingSpotConfig
        VacancyEvent_model = db_module.VacancyEvent
        DeviceToken_model = db_module.DeviceToken
        SessionLocal_db = db_module.SessionLocal 
        db_engine_global = db_module.engine 
        logger.info("INIT_DEPS: Successfully imported db.py, spot_logic.py, cv_model.py.")
    except ImportError as e:
        logger.critical(f"INIT_DEPS: CRITICAL - Failed to import core modules: {e}", exc_info=True); return

    if FIREBASE_CRED_PATH_ENV:
        if os.path.exists(FIREBASE_CRED_PATH_ENV):
            try:
                if not firebase_admin._apps:
                    cred = credentials.Certificate(FIREBASE_CRED_PATH_ENV)
                    firebase_initialize_app(cred)
                    logger.info("INIT_DEPS: Firebase app initialized successfully.")
                    firebase_app_initialized_flag = True
                else: logger.info("INIT_DEPS: Firebase app already initialized."); firebase_app_initialized_flag = True
            except Exception as e: logger.error(f"INIT_DEPS: Error initializing Firebase with {FIREBASE_CRED_PATH_ENV}: {e}", exc_info=True)
        else: logger.warning(f"INIT_DEPS: Firebase credentials file NOT FOUND at: {FIREBASE_CRED_PATH_ENV}.")
    else: logger.warning("INIT_DEPS: FIREBASE_CRED_PATH_ENV (from FIREBASE_CRED) is not set. FCM disabled.")

    db_tables_initialized = False
    if db_engine_global:
        for attempt in range(DB_INIT_MAX_RETRIES):
            try:
                logger.info(f"INIT_DEPS: DB table init attempt {attempt + 1}/{DB_INIT_MAX_RETRIES}...")
                with db_engine_global.connect() as conn_test: conn_test.execute(select(1)) 
                db_module.init_db() 
                with Session(db_engine_global) as session_v: session_v.exec(select(ParkingSpotConfig_model).limit(1)).first()
                logger.info("INIT_DEPS: Verified ParkingSpotConfig table exists."); db_tables_initialized = True; break
            except Exception as e:
                logger.error(f"INIT_DEPS: Error DB table init attempt {attempt + 1}: {e}", exc_info=True)
                if attempt < DB_INIT_MAX_RETRIES - 1: await asyncio.sleep(DB_INIT_RETRY_DELAY)
                else: logger.error("INIT_DEPS: Max DB table init retries reached.")
    else: logger.critical("INIT_DEPS: db_engine_global not available. Cannot initialize tables.")
    
    if db_tables_initialized:
        try:
            spot_logic_module.refresh_spots() 
            logger.info(f"INIT_DEPS: Spots loaded: {len(spot_logic_module.SPOTS)} spots.")
            now = datetime.utcnow()
            for spot_id_key in spot_logic_module.SPOTS.keys():
                s_id = str(spot_id_key)
                previous_spot_states_global.setdefault(s_id, False); empty_start.setdefault(s_id, now); notified.setdefault(s_id, False)
            logger.info(f"INIT_DEPS: Global spot states initialized for {len(previous_spot_states_global)} spots.")
        except Exception as e_sl: logger.error(f"INIT_DEPS: Error initializing spot_logic/globals: {e_sl}", exc_info=True)
    else: logger.critical("INIT_DEPS: DATABASE TABLES FAILED TO INITIALIZE. Spot logic may fail.")

    event_queue = asyncio.Queue(maxsize=200); logger.info("INIT_DEPS: Event queue initialized.")
    asyncio.create_task(event_processor_task_deferred()); logger.info("INIT_DEPS: event_processor_task_deferred scheduled.")
    if not video_processing_active: logger.info("INIT_DEPS: Creating video_processor_deferred task..."); asyncio.create_task(video_processor_deferred())
    logger.info("INIT_DEPS: Asynchronous initialization complete.")

@app.on_event("startup")
async def startup_event_deferred_call():
    logger.info("STARTUP_EVENT: FastAPI app started. Scheduling deferred initialization.")
    asyncio.create_task(initialize_application_dependencies())

@app.on_event("shutdown")
async def shutdown_event_deferred():
    global video_processing_active, video_capture_global
    logger.info("SHUTDOWN_EVENT: Initiated..."); video_processing_active = False
    if video_capture_global: video_capture_global.release(); video_capture_global = None
    await asyncio.sleep(1.0); logger.info("SHUTDOWN_EVENT: Complete.")

# --- Background Task Definitions ---
async def event_processor_task_deferred():
    global event_queue, manager 
    if not event_queue: logger.error("event_processor_task: Queue not ready."); return
    logger.info("BACKGROUND_TASK: Event processor started.")
    while True:
        try:
            message_data = await event_queue.get(); await manager.broadcast(message_data); event_queue.task_done()
        except asyncio.CancelledError: logger.info("BACKGROUND_TASK: Event processor cancelled."); break
        except Exception as e: logger.error(f"BACKGROUND_TASK: Error in event_processor: {e}",exc_info=True); await asyncio.sleep(1)

def make_capture_deferred(): 
    global video_capture_global, APP_DIR_PATH
    logger.info(f"MAKE_CAPTURE: Type: {VIDEO_SOURCE_TYPE_ENV_VAL}, Source: {VIDEO_SOURCE_ENV}")
    
    if VIDEO_SOURCE_TYPE_ENV_VAL == "WEBSOCKET_STREAM":
        logger.info("MAKE_CAPTURE: Mode is WEBSOCKET_STREAM. No cv2.VideoCapture needed by make_capture.")
        return None
    
    cap = None; source_to_open = VIDEO_SOURCE_ENV
    if VIDEO_SOURCE_TYPE_ENV_VAL == "FILE":
        default_file_path = str(APP_DIR_PATH.parent / "videos" / "test_video.mov") 
        if not source_to_open: source_to_open = default_file_path; logger.info(f"MAKE_CAPTURE: Defaulting to {source_to_open}")
        if not os.path.exists(source_to_open): raise RuntimeError(f"Video file not found: {source_to_open}")
        cap = cv2.VideoCapture(source_to_open)
    elif VIDEO_SOURCE_TYPE_ENV_VAL == "WEBCAM_INDEX":
        try: idx = int(source_to_open if source_to_open is not None else "0"); cap = cv2.VideoCapture(idx); source_to_open = f"Webcam Index {idx}"
        except ValueError: raise RuntimeError(f"Invalid webcam index: {source_to_open}")
    elif source_to_open: cap = cv2.VideoCapture(source_to_open, cv2.CAP_FFMPEG)
    else: raise RuntimeError(f"VIDEO_SOURCE not set for {VIDEO_SOURCE_TYPE_ENV_VAL} or unhandled type.")
    if cap is None or not cap.isOpened(): raise RuntimeError(f"MAKE_CAPTURE: FATAL - Could not open: {source_to_open}")
    logger.info(f"MAKE_CAPTURE: Successfully opened: {source_to_open}"); video_capture_global = cap; return cap

async def video_processor_deferred():
    global video_processing_active, video_capture_global, latest_frame_with_all_overlays, previous_spot_states_global, empty_start, notified, video_frame_queue
    global spot_logic_module, cv_model_module, firebase_app_initialized_flag, db_engine_global, VacancyEvent_model

    if not spot_logic_module or not cv_model_module or not hasattr(cv_model_module, 'detect'):
        logger.error("VIDEO_PROCESSOR: Core modules (spot_logic, cv_model.detect) not initialized. Exiting.")
        video_processing_active = False; return
    
    logger.info("VIDEO_PROCESSOR: Task starting...")
    cap = None; is_websocket_stream_mode = (VIDEO_SOURCE_TYPE_ENV_VAL == "WEBSOCKET_STREAM")

    if not is_websocket_stream_mode:
        try: 
            logger.info("VIDEO_PROCESSOR: Non-WebSocket mode, calling make_capture_deferred().")
            cap = make_capture_deferred()
            if cap is None and VIDEO_SOURCE_TYPE_ENV_VAL not in ["WEBSOCKET_STREAM"]:
                 raise RuntimeError(f"make_capture_deferred returned None for non-WebSocket type {VIDEO_SOURCE_TYPE_ENV_VAL}")
        except RuntimeError as e: 
            logger.error(f"VIDEO_PROCESSOR: Failed to init capture via make_capture_deferred: {e}", exc_info=True)
            video_processing_active = False; return 
    
    video_processing_active = True
    VACANCY_DELAY = timedelta(seconds=FCM_VACANCY_DELAY_SECONDS_ENV)
    loop = asyncio.get_event_loop()
    target_fps = VIDEO_PROCESSING_FPS_ENV
    logger.info(f"VIDEO_PROCESSOR: Loop starting. Mode: {'WebSocket' if is_websocket_stream_mode else 'Capture'}. Target FPS: {target_fps}")
    
    frame_count = 0
    source_fps_from_cap = cap.get(cv2.CAP_PROP_FPS) if cap and not is_websocket_stream_mode else 0
    frame_skip_interval = int(source_fps_from_cap / target_fps) if source_fps_from_cap > 0 and target_fps > 0 and target_fps < source_fps_from_cap else 0
    
    while video_processing_active:
        frame = None; ret = False; processing_start_time = time.perf_counter()
        if is_websocket_stream_mode:
            try:
                frame_data_url = await asyncio.wait_for(video_frame_queue.get(), timeout=1.0)
                if frame_data_url.startswith('data:image/jpeg;base64,'):
                    img_bytes = base64.b64decode(frame_data_url.split(',',1)[1]); arr = np.frombuffer(img_bytes, np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None: ret=True; video_frame_queue.task_done()
                    else: logger.warning("VP: WS frame decode failed.")
                else: logger.warning(f"VP: WS received non-JPEG base64: {frame_data_url[:50]}")
            except asyncio.TimeoutError: await asyncio.sleep(0.01); continue
            except Exception as e: logger.error(f"VP: WS frame processing error: {e}", exc_info=True); await asyncio.sleep(0.1); continue
        else: 
            if cap and cap.isOpened(): 
                frame_count +=1
                if frame_skip_interval > 0 and frame_count % (frame_skip_interval + 1) != 0:
                    await asyncio.sleep(0.001); continue 
                ret, frame = cap.read()
            else: 
                logger.warning("VP: Capture not open or lost in loop. Attempting to reopen..."); await asyncio.sleep(1)
                try: 
                    if cap: cap.release()
                    cap = make_capture_deferred() 
                    if not cap: logger.error("VP: Failed to reopen capture. Stopping loop."); video_processing_active = False; break
                    frame_count = 0 
                    source_fps_from_cap = cap.get(cv2.CAP_PROP_FPS) if cap else 0 
                    frame_skip_interval = int(source_fps_from_cap / target_fps) if source_fps_from_cap > 0 and target_fps > 0 and target_fps < source_fps_from_cap else 0
                except Exception as e_recap: logger.error(f"VP: Error reopening capture in loop: {e_recap}"); video_processing_active = False; break
                continue
        
        if not ret or frame is None: 
            if not is_websocket_stream_mode and cap and not cap.isOpened(): 
                logger.info("VP: Video source ended or disconnected (e.g. end of file). Stopping processor.")
                video_processing_active = False; break 
            logger.warning("VP: Failed to get frame or frame is None."); await asyncio.sleep(0.05); continue

        yolo_results = []
        try: yolo_results = await loop.run_in_executor(None, cv_model_module.detect, frame.copy())
        except Exception as e: logger.error(f"VP: detect error: {e}", exc_info=True)
        
        current_occupancy = spot_logic_module.get_spot_states(yolo_results)
        now = datetime.utcnow(); active_spot_labels = set(spot_logic_module.SPOTS.keys())

        for sid in active_spot_labels: 
            if sid not in previous_spot_states_global: 
                previous_spot_states_global[sid] = False; empty_start[sid] = now; notified[sid] = False
        for sid_g in list(previous_spot_states_global.keys()): 
            if sid_g not in active_spot_labels: 
                previous_spot_states_global.pop(sid_g,None); empty_start.pop(sid_g,None); notified.pop(sid_g,None)

        for spot_id in active_spot_labels:
            was_occ = previous_spot_states_global.get(spot_id, False)
            is_now_occ = current_occupancy.get(spot_id, False)
            if was_occ != is_now_occ:
                logger.info(f"VP: Spot {spot_id} changed: {'Free' if was_occ else 'Occupied'} -> {'Occupied' if is_now_occ else 'Free'}")
                enqueue_event({"type": "spot_update", "data": {"spot_id": spot_id, "timestamp": now.isoformat()+"Z", "status": "occupied" if is_now_occ else "free"}})
                if is_now_occ: empty_start[spot_id] = None
                else: empty_start[spot_id] = now; notified[spot_id] = False
            if not is_now_occ and empty_start.get(spot_id) and not notified.get(spot_id, False):
                if (now - empty_start[spot_id]) >= VACANCY_DELAY: 
                    logger.info(f"VP: Spot {spot_id} confirmed vacant for {VACANCY_DELAY}, attempting FCM.")
                    try:
                        spot_id_int = int(spot_id)
                        await loop.run_in_executor(None, notify_users_for_spot_vacancy_deferred, spot_id_int); notified[spot_id] = True
                        if db_engine_global and VacancyEvent_model: 
                            with Session(db_engine_global) as s_db: 
                                s_db.add(VacancyEvent_model(timestamp=now, spot_id=spot_id_int, camera_id="default_camera")); s_db.commit()
                                logger.info(f"VP: Logged VacancyEvent for {spot_id}.")
                    except Exception as e_fcm_db: logger.error(f"VP: FCM/DB log error for spot {spot_id}: {e_fcm_db}", exc_info=True)
            previous_spot_states_global[spot_id] = is_now_occ
        
        frame_to_display = frame.copy() 
        for lbl, coords in spot_logic_module.SPOTS.items():
            if not (isinstance(coords, tuple) and len(coords) == 4): continue
            sx,sy,sw,sh = coords; is_occ = previous_spot_states_global.get(lbl,False); color = (0,0,255) if is_occ else (0,255,0)
            cv2.rectangle(frame_to_display, (sx,sy), (sx+sw, sy+sh), color, 2); cv2.putText(frame_to_display, lbl, (sx,sy-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color,1)
        if yolo_results: 
            for res in yolo_results: 
                if hasattr(res, 'boxes') and res.boxes is not None: 
                    vehicle_classes_for_drawing = {"car","truck","bus","motorbike","bicycle"}
                    box_coords_list = res.boxes.xyxy.tolist()  
                    class_indices = res.boxes.cls.tolist()    
                    class_names_map = res.names               
                    for i, box_xyxy in enumerate(box_coords_list):
                        class_idx = int(class_indices[i])
                        detected_class_name = class_names_map.get(class_idx)
                        if detected_class_name and detected_class_name in vehicle_classes_for_drawing: 
                            cv2.rectangle(frame_to_display, (int(box_xyxy[0]),int(box_xyxy[1])), (int(box_xyxy[2]),int(box_xyxy[3])), (0,255,255),1)
        
        async with frame_access_lock: latest_frame_with_all_overlays = frame_to_display
        
        current_frame_processing_time = time.perf_counter() - processing_start_time
        logger.info(f"VP: Frame processed in {current_frame_processing_time:.4f}s.")
        sleep_for = (1.0/target_fps) - current_frame_processing_time
        if sleep_for > 0: await asyncio.sleep(sleep_for)
        else: await asyncio.sleep(0.001) 
    
    logger.info("VIDEO_PROCESSOR: Loop ended.")
    if cap: cap.release()

def notify_users_for_spot_vacancy_deferred(spot_id_int: int):
    global firebase_app_initialized_flag, db_engine_global, DeviceToken_model 
    if not firebase_app_initialized_flag: logger.warning("FCM: Firebase not init."); return
    if not db_engine_global or not DeviceToken_model : logger.error("FCM: DB components not ready."); return
    logger.info(f"FCM: Notifying for spot {spot_id_int}")
    try:
        with Session(db_engine_global) as s:
            tokens = [r.token for r in s.exec(select(DeviceToken_model)).all() if r.token]
            if not tokens: logger.info("FCM: No tokens."); return
            msg = firebase_admin.messaging.MulticastMessage(notification=firebase_admin.messaging.Notification(title="Parking Spot Available!", body=f"Spot {spot_id_int} is now free."), tokens=tokens)
            resp = firebase_admin.messaging.send_multicast(msg); 
            logger.info(f'FCM: {resp.success_count} sent for spot {spot_id_int}')
            if resp.failure_count > 0: 
                failed_responses = [r for r in resp.responses if not r.success]
                logger.warning(f"FCM: {resp.failure_count} messages failed. Details: {failed_responses}")
    except Exception as e: logger.error(f"FCM error for spot {spot_id_int}: {e}", exc_info=True)

# --- API Endpoints ---
def get_db_session_dependency(): 
    if not SessionLocal_db:
        logger.error("API_DEPENDENCY: SessionLocal_db not initialized yet!")
        raise HTTPException(status_code=503, detail="DB session not available. Service initializing.")
    db = SessionLocal_db()
    try:
        yield db
    finally:
        db.close()

@app.get("/api/spots_v10_get")
async def get_spots_config_v2_deferred(db: Session = Depends(get_db_session_dependency)): 
    if not spot_logic_module: raise HTTPException(status_code=503, detail="Service logic not ready")
    logger.info("API: GET /api/spots_v10_get hit.")
    spots_data_response = []
    try:
        spot_logic_module.refresh_spots() 
        for spot_id, coords in spot_logic_module.SPOTS.items():
            is_available = not previous_spot_states_global.get(str(spot_id), False)
            spots_data_response.append({"id": str(spot_id), "x": coords[0], "y": coords[1], "w": coords[2], "h": coords[3], "is_available": is_available})
        return {"spots": spots_data_response}
    except Exception as e: 
        logger.error(f"API GET /api/spots_v10_get error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching spots: {str(e)}")

@app.post("/api/nuke_test_save")
async def save_spots_config_v2_deferred(payload: SpotsUpdateRequest, db: Session = Depends(get_db_session_dependency)):
    if not spot_logic_module or not ParkingSpotConfig_model: 
        raise HTTPException(status_code=503, detail="Service logic/models not ready for save")
    logger.info(f"API: POST /api/nuke_test_save: {payload.model_dump_json(indent=1)}")
    default_camera_id = "default_camera"
    try:
        statement_existing = select(ParkingSpotConfig_model).where(ParkingSpotConfig_model.camera_id == default_camera_id)
        existing_spot_configs_db = db.exec(statement_existing).all()
        existing_spots_map = {str(config.spot_label): config for config in existing_spot_configs_db}
        
        incoming_spot_labels = set()
        response_spots_data = []
        for spot_in_model in payload.spots:
            label = str(spot_in_model.id)
            incoming_spot_labels.add(label)
            if label in existing_spots_map:
                config_to_update = existing_spots_map[label]
                config_to_update.x_coord, config_to_update.y_coord = spot_in_model.x, spot_in_model.y
                config_to_update.width, config_to_update.height = spot_in_model.w, spot_in_model.h
                config_to_update.updated_at = datetime.utcnow()
                db.add(config_to_update)
            else:
                new_config = ParkingSpotConfig_model(spot_label=label, camera_id=default_camera_id, 
                                                x_coord=spot_in_model.x, y_coord=spot_in_model.y, 
                                                width=spot_in_model.w, height=spot_in_model.h,
                                                created_at=datetime.utcnow()) 
                db.add(new_config)
            response_spots_data.append(spot_in_model.model_dump())

        for existing_label, config_to_delete in existing_spots_map.items():
            if existing_label not in incoming_spot_labels:
                db.delete(config_to_delete)
        db.commit()
        logger.info("API: Spots saved to DB.")
        
        spot_logic_module.refresh_spots() 
        
        current_db_spot_ids = set(spot_logic_module.SPOTS.keys()); now_new = datetime.utcnow()
        for sid_g in list(previous_spot_states_global.keys()):
            if str(sid_g) not in current_db_spot_ids: 
                previous_spot_states_global.pop(str(sid_g),None);empty_start.pop(str(sid_g),None);notified.pop(str(sid_g),None)
        for sid_db in current_db_spot_ids:
            s_id_db = str(sid_db)
            if s_id_db not in previous_spot_states_global: 
                previous_spot_states_global[s_id_db]=False;empty_start[s_id_db]=now_new;notified[s_id_db]=False
        
        spots_event_data = [{"id":l,"x":c[0],"y":c[1],"w":c[2],"h":c[3]} for l,c in spot_logic_module.SPOTS.items()]
        enqueue_event({"type":"spots_config_updated", "data":{"spots":spots_event_data}})
        return {"message": "Spots saved to DB successfully!", "spots": response_spots_data}
    except Exception as e: 
        db.rollback(); logger.error(f"API POST /api/nuke_test_save error: {e}", exc_info=True); raise HTTPException(status_code=500, detail=str(e))

@app.get("/webcam_feed")
async def mjpeg_webcam_feed_deferred():
    if not cv_model_module: raise HTTPException(status_code=503, detail="Video components not ready")
    async def generate_mjpeg_frames(): 
        global latest_frame_with_all_overlays, frame_access_lock
        while True:
            frame_to_send = None; 
            async with frame_access_lock:
                if latest_frame_with_all_overlays is not None: frame_to_send = latest_frame_with_all_overlays.copy()
            if frame_to_send is not None:
                try:
                    flag, enc_img = cv2.imencode(".jpg", frame_to_send);
                    if not flag: await asyncio.sleep(0.1); continue
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + enc_img.tobytes() + b'\r\n')
                except Exception as e: logger.error(f"MJPEG error: {e}"); await asyncio.sleep(0.1); continue
            await asyncio.sleep(1.0 / VIDEO_PROCESSING_FPS_ENV) 
    return StreamingResponse(generate_mjpeg_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/spots")
async def websocket_spots_endpoint_deferred(websocket: WebSocket):
    if not spot_logic_module: await websocket.close(code=1011, reason="Spot logic not ready"); return
    await manager.connect(websocket) 
    try:
        current_statuses = {}
        spot_logic_module.refresh_spots() 
        for lbl, coords in spot_logic_module.SPOTS.items():
            if isinstance(coords,tuple) and len(coords)==4: 
                current_statuses[lbl] = {
                    "status":"occupied" if previous_spot_states_global.get(lbl,False) else "free", 
                    "timestamp":(empty_start.get(lbl) or datetime.utcnow()).isoformat()+"Z", 
                    "x":coords[0],"y":coords[1],"w":coords[2],"h":coords[3]
                }
        await websocket.send_text(json.dumps({"type":"all_spot_statuses", "data":current_statuses, "timestamp":time.time()},default=str))
        while True: 
            try: 
                await asyncio.wait_for(websocket.receive_text(), timeout=60) 
            except asyncio.TimeoutError: 
                await websocket.send_text(json.dumps({"type":"ping"}))
    except WebSocketDisconnect: logger.info(f"WS-Spots: Client {websocket.client} disconnected")
    except Exception as e: logger.error(f"WS-Spots: Error for {websocket.client}: {e}", exc_info=True)
    finally: await manager.disconnect(websocket)

@app.websocket("/ws/video_stream_upload")
async def websocket_video_upload_endpoint_deferred(websocket: WebSocket):
    global video_frame_queue 
    await websocket.accept()
    logger.info(f"WS-Upload: Client connected {websocket.client}")
    try:
        while True: 
            data = await websocket.receive_text()
            if VIDEO_SOURCE_TYPE_ENV_VAL != "WEBSOCKET_STREAM": 
                logger.warning("WS-Upload: Frame recv but backend not in WEBSOCKET_STREAM mode."); continue
            if video_frame_queue.full(): 
                try: 
                    video_frame_queue.get_nowait(); video_frame_queue.task_done() 
                    logger.info("WS-Upload: video_frame_queue was full, discarded oldest frame.")
                except asyncio.QueueEmpty: pass
            await video_frame_queue.put(data)
    except WebSocketDisconnect: logger.info(f"WS-Upload: Client disconnected {websocket.client}")
    except Exception as e: logger.error(f"WS-Upload: Error for {websocket.client}: {e}", exc_info=True)

@app.post("/api/register_fcm_token")
async def register_fcm_token_api_deferred(payload: TokenRegistration, db: Session = Depends(get_db_session_dependency)):
    if not DeviceToken_model: raise HTTPException(status_code=503, detail="DB components not ready for FCM token.")
    logger.info(f"API: Register FCM token: {payload.token[:20]}...")
    try:
        existing_token = db.exec(select(DeviceToken_model).where(DeviceToken_model.token == payload.token)).first()
        if existing_token: 
            return {"message": "Token already registered."}
        new_token = DeviceToken_model(token=payload.token, platform=payload.platform)
        db.add(new_token); db.commit(); db.refresh(new_token) 
        return {"message": "Token registered successfully."}
    except Exception as e: 
        db.rollback(); logger.error(f"FCM token reg error: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Failed to register FCM token.")

# --- Static File Serving ---
STATIC_FRONTEND_DIR = APP_DIR_PATH.parent / "frontend_build" 
if STATIC_FRONTEND_DIR.exists() and (STATIC_FRONTEND_DIR / "index.html").exists():
    logger.info(f"MAIN.PY: Serving static files from: {STATIC_FRONTEND_DIR}")
    app.mount("/static", StaticFiles(directory= STATIC_FRONTEND_DIR / "static"), name="react_static_assets")
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_react_app_deferred(request: Request, full_path: str):
        index_html_path = STATIC_FRONTEND_DIR / "index.html"
        return FileResponse(index_html_path)
else:
    logger.warning(f"MAIN.PY: Static frontend directory or index.html not found at {STATIC_FRONTEND_DIR}. SPA serving disabled.")

app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
