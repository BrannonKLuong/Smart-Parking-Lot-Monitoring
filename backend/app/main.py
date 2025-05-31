# backend/app/main.py (Merged Version)
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

# AWS SDK for Python
import boto3

# Database and Spot Logic
# Ensure db.py defines: engine (as db_engine), ParkingSpotConfig, VacancyEvent, DeviceToken, SessionLocal, init_db, Base
from .db import engine as db_engine, ParkingSpotConfig, VacancyEvent, DeviceToken, SessionLocal, init_db
from . import spot_logic # Using the new spot_logic.py
from sqlmodel import Session, select

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, initialize_app as firebase_initialize_app

# Object Detection Model
# Ensure inference.cv_model defines detect
try:
    from ..inference.cv_model import detect
except ImportError:
    from inference.cv_model import detect # Fallback for local execution
    print("WARN: Imported 'detect' from local 'inference' module. Ensure Docker structure matches.")


# --- Configuration & Globals ---
logging.basicConfig(level=logging.INFO) # Consider logging.DEBUG for more verbose output if needed
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL not set, using default SQLite for local dev.")
    DATABASE_URL = "sqlite:///./test.db" # Ensure your db_engine uses this

FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED")
FCM_VACANCY_DELAY_SECONDS = int(os.getenv("FCM_VACANCY_DELAY_SECONDS", "5"))
VIDEO_PROCESSING_FPS = int(os.getenv("VIDEO_PROCESSING_FPS", "15")) # Target FPS for processing
VIDEO_SOURCE_TYPE_ENV = os.getenv("VIDEO_SOURCE_TYPE", "FILE").upper()
VIDEO_SOURCE = os.getenv("VIDEO_SOURCE") # Value for FILE, KVS_STREAM, WEBCAM_INDEX etc.

DB_INIT_MAX_RETRIES = 15
DB_INIT_RETRY_DELAY = 5

# --- Firebase Initialization ---
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
ROOT_DIR = BACKEND_DIR.parent

app = FastAPI(debug=True) # Set debug=False for production

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models (from Codebase 2 for new spot APIs) ---
class SpotConfigIn(BaseModel):
    id: str
    x: int
    y: int
    w: int
    h: int

class SpotsUpdateRequest(BaseModel):
    spots: List[SpotConfigIn]

class TokenRegistration(BaseModel): # From Codebase 1
    token: str
    platform: str = "android"


# --- Global States (from Codebase 1, initialized robustly as per Codebase 2 startup) ---
video_processing_active = False
video_capture_global: Optional[cv2.VideoCapture] = None
previous_spot_states_global: Dict[str, bool] = {}
latest_frame_with_all_overlays: Optional[Any] = None # Stores the latest processed frame
frame_access_lock = asyncio.Lock() # For safe access to latest_frame_with_all_overlays
empty_start: Dict[str, Optional[datetime]] = {} # Tracks when a spot became empty
notified: Dict[str, bool] = {} # Tracks if FCM notification sent for a vacant spot
event_queue: Optional[asyncio.Queue] = None # For broadcasting spot updates via WebSocket
video_frame_queue: asyncio.Queue = asyncio.Queue(maxsize=10) # For incoming frames from UI


# --- WebSocket Connection Manager for Spot Updates (from Codebase 1) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        logger.info(f"WebSocket connection established for spot updates: {websocket.client}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info(f"WebSocket connection closed for spot updates: {websocket.client}")

    async def broadcast(self, data: Dict[str, Any]):
        message_str = json.dumps(data, default=str)
        connections_to_remove = []
        async with self._lock: # Operate on a copy for safe iteration
            current_connections = list(self.active_connections)

        for connection in current_connections:
            try:
                await connection.send_text(message_str)
            except Exception as e:
                logger.error(f"Error broadcasting to WebSocket {connection.client}: {e}. Marking for removal.")
                connections_to_remove.append(connection)
        
        if connections_to_remove:
            async with self._lock:
                for ws_to_remove in connections_to_remove:
                    if ws_to_remove in self.active_connections:
                        self.active_connections.remove(ws_to_remove)
manager = ConnectionManager()


# --- Event Queue Logic (from Codebase 1, used by video_processor and spot save API) ---
def enqueue_event(event_data: Dict[str, Any]):
    global event_queue
    if event_queue is not None:
        try:
            event_queue.put_nowait(event_data)
        except asyncio.QueueFull:
            logger.warning(f"Event queue full. Dropping event: {event_data.get('type')}")
        except Exception as e:
            logger.error(f"Error enqueuing event: {e}")
    else:
        logger.warning("Event queue not initialized. Cannot enqueue event.")

async def event_processor_task():
    global event_queue
    if event_queue is None:
        logger.error("Event queue not initialized in event_processor_task. Exiting task.")
        return
        
    logger.info("Event processor task started for broadcasting spot updates.")
    while True:
        try:
            message_data = await event_queue.get()
            await manager.broadcast(message_data)
            event_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Event processor task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in event processor task: {e}", exc_info=True)
            await asyncio.sleep(1)


# --- Video Source Handling (from Codebase 1) ---
def get_kvs_hls_url(stream_name_or_arn, region_name=os.getenv("AWS_REGION", "us-east-2")):
    try:
        logger.info(f"Attempting to get HLS URL for KVS stream: {stream_name_or_arn} in region {region_name}")
        kvs_client = boto3.client('kinesisvideo', region_name=region_name)
        endpoint_params = {'StreamName': stream_name_or_arn, 'APIName': 'GET_HLS_STREAMING_SESSION_URL'}
        hls_params = {'StreamName': stream_name_or_arn, 'PlaybackMode': 'LIVE'}
        
        data_endpoint_response = kvs_client.get_data_endpoint(**endpoint_params)
        data_endpoint = data_endpoint_response['DataEndpoint']
        logger.info(f"KVS Data Endpoint: {data_endpoint}")

        kvs_media_client = boto3.client('kinesis-video-archived-media', endpoint_url=data_endpoint, region_name=region_name)
        hls_params.update({
            'ContainerFormat': 'MPEG_TS',
            'DiscontinuityMode': 'ALWAYS',
            'DisplayFragmentTimestamp': 'ALWAYS',
            'Expires': 300 # 5 minutes
        })
        
        hls_url_response = kvs_media_client.get_hls_streaming_session_url(**hls_params)
        hls_url = hls_url_response['HLSStreamingSessionURL']
        logger.info(f"Successfully obtained KVS HLS URL.")
        return hls_url
    except Exception as e:
        logger.error(f"Error getting KVS HLS URL for '{stream_name_or_arn}': {e}", exc_info=True)
        return None

def make_capture():
    global video_capture_global
    env_video_source_value = VIDEO_SOURCE # Use the global VIDEO_SOURCE

    logger.info(f"--- make_capture called. Video Source Type: '{VIDEO_SOURCE_TYPE_ENV}', Value: '{env_video_source_value}' ---")
    
    if VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM":
        logger.info("make_capture: Mode is WEBSOCKET_STREAM. No cv2.VideoCapture created here.")
        return None

    cap = None
    source_path_for_opencv = env_video_source_value

    if VIDEO_SOURCE_TYPE_ENV == "KVS_STREAM":
        if not env_video_source_value:
            raise RuntimeError("VIDEO_SOURCE (KVS stream name) not set for KVS_STREAM type.")
        aws_region = os.getenv("AWS_REGION", "us-east-2")
        hls_url = get_kvs_hls_url(stream_name_or_arn=env_video_source_value, region_name=aws_region)
        if hls_url:
            logger.info(f"Attempting to open KVS HLS stream with OpenCV: {hls_url}")
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|analyzeduration;2000000|probesize;1000000"
            cap = cv2.VideoCapture(hls_url, cv2.CAP_FFMPEG)
            source_path_for_opencv = hls_url # For error message
        else:
            raise RuntimeError(f"Failed to get KVS HLS URL from AWS for stream: {env_video_source_value}")
    elif VIDEO_SOURCE_TYPE_ENV == "FILE":
        default_video_file = str(APP_DIR / "videos" / "test_video.mov") # Example path
        if not env_video_source_value:
            env_video_source_value = default_video_file
            logger.info(f"VIDEO_SOURCE not set for FILE type, defaulting to: {default_video_file}")
        source_path_for_opencv = env_video_source_value
        if not os.path.exists(source_path_for_opencv):
            logger.error(f"Video file not found at path: {source_path_for_opencv}")
            raise RuntimeError(f"Video file not found: {source_path_for_opencv}")
        cap = cv2.VideoCapture(source_path_for_opencv)
    elif VIDEO_SOURCE_TYPE_ENV == "WEBCAM_INDEX":
        try:
            idx = int(env_video_source_value if env_video_source_value is not None else "0")
            logger.info(f"Attempting to open webcam index: {idx}")
            cap = cv2.VideoCapture(idx)
            source_path_for_opencv = f"Webcam Index {idx}"
        except ValueError:
            raise RuntimeError(f"Invalid webcam index: {env_video_source_value}")
    elif env_video_source_value: # Other URL types
        logger.info(f"Attempting to open direct video URL: {env_video_source_value}")
        cap = cv2.VideoCapture(env_video_source_value, cv2.CAP_FFMPEG)
    else:
        raise RuntimeError(f"VIDEO_SOURCE not set or invalid VIDEO_SOURCE_TYPE: {VIDEO_SOURCE_TYPE_ENV}")

    if cap is None or not cap.isOpened():
        error_msg = f"FATAL: Could not open video source. Type='{VIDEO_SOURCE_TYPE_ENV}', Source='{source_path_for_opencv}'"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info(f"Successfully opened video source: {source_path_for_opencv}")
    video_capture_global = cap
    return cap


# --- Video Processor (from Codebase 1) ---
async def video_processor():
    global video_processing_active, video_capture_global, previous_spot_states_global
    global latest_frame_with_all_overlays, frame_access_lock, empty_start, notified, video_frame_queue

    cap = None
    is_websocket_stream_mode = (VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM")

    if not is_websocket_stream_mode:
        try:
            logger.info("video_processor: Initializing video capture via make_capture()...")
            cap = make_capture() # This sets video_capture_global
        except RuntimeError as e:
            logger.error(f"video_processor: CRITICAL - Failed to initialize video capture: {e}", exc_info=True)
            video_processing_active = False
            await manager.broadcast({"type": "video_error", "data": {"error": "Video source failed on startup", "detail": str(e)}})
            return
        logger.info("video_processor: Video capture initialized. Starting processing loop.")
    else:
        logger.info("video_processor: Mode is WEBSOCKET_STREAM. Will process frames from video_frame_queue.")

    video_processing_active = True
    VACANCY_DELAY = timedelta(seconds=FCM_VACANCY_DELAY_SECONDS)
    loop = asyncio.get_event_loop()

    # Initialize states for spots loaded at startup (already done in startup_event more robustly)
    # spot_logic.refresh_spots() called in startup_event
    # previous_spot_states_global, empty_start, notified are also init in startup_event

    frame_count = 0
    source_fps = cap.get(cv2.CAP_PROP_FPS) if cap and not is_websocket_stream_mode else 0
    
    frame_skip_interval = 0
    if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps:
        frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS)
    logger.info(f"Source FPS (if applicable): {source_fps}, Target Processing FPS: {VIDEO_PROCESSING_FPS}, Frame skip interval: {frame_skip_interval}")

    while video_processing_active:
        frame = None
        ret = False
        processing_start_time = time.perf_counter()

        if is_websocket_stream_mode:
            try:
                frame_data_url = await asyncio.wait_for(video_frame_queue.get(), timeout=1.0)
                if frame_data_url.startswith('data:image/jpeg;base64,'):
                    base64_str = frame_data_url.split(',', 1)[1]
                    img_bytes = base64.b64decode(base64_str)
                    np_arr = np.frombuffer(img_bytes, np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        ret = True
                        video_frame_queue.task_done()
                    else:
                        logger.warning("VIDEO_PROCESSOR (WebSocket): Failed to decode frame from base64.")
                else:
                    logger.warning(f"VIDEO_PROCESSOR (WebSocket): Received non-JPEG base64 data: {frame_data_url[:100]}")
            except asyncio.TimeoutError:
                await asyncio.sleep(0.01) # Wait briefly if queue is empty
                continue
            except Exception as e_ws_frame:
                logger.error(f"VIDEO_PROCESSOR (WebSocket): Error processing frame from queue: {e_ws_frame}", exc_info=True)
                await asyncio.sleep(0.1)
                continue
        else: # Traditional cv2.VideoCapture mode
            if cap is None or not cap.isOpened():
                logger.warning("video_processor: Video capture became un-opened. Attempting to re-initialize...")
                try:
                    if cap: cap.release()
                    cap = make_capture()
                    if not cap: # make_capture failed
                         video_processing_active = False; break
                    source_fps = cap.get(cv2.CAP_PROP_FPS) if cap else 0
                    if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps:
                        frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS)
                    logger.info(f"video_processor: Re-initialized video capture. New FPS: {source_fps}, Skip: {frame_skip_interval}")
                    frame_count = 0
                except RuntimeError as e:
                    logger.error(f"video_processor: Failed to re-initialize video capture: {e}. Stopping.", exc_info=True)
                    video_processing_active = False
                    await manager.broadcast({"type": "video_error", "data": {"error": "Video source lost", "detail": str(e)}})
                    break
                except Exception as e_reopen:
                     logger.error(f"video_processor: Generic error re-opening capture: {e_reopen}. Stopping.", exc_info=True)
                     video_processing_active = False; break
            
            if cap: ret, frame = cap.read()

        if not ret or frame is None:
            if not is_websocket_stream_mode and video_processing_active: # Only retry for non-websocket if still active
                logger.warning("video_processor: Failed to grab frame. Attempting to reopen video source.")
                if cap: cap.release()
                await asyncio.sleep(2) # Wait before retrying
                try:
                    cap = make_capture()
                    if not (cap and cap.isOpened()):
                        logger.error("video_processor: Failed to re-open capture after frame grab failure. Stopping.")
                        video_processing_active = False; break
                    logger.info("video_processor: Successfully re-opened capture.")
                except Exception as e:
                    logger.error(f"video_processor: Error re-opening capture: {e}. Stopping.", exc_info=True)
                    video_processing_active = False; break
            elif is_websocket_stream_mode: # If WS stream and frame is None (e.g. decode error)
                await asyncio.sleep(0.01) # Small pause
            continue
        
        frame_count += 1
        if not is_websocket_stream_mode and frame_skip_interval > 0 and frame_count % (frame_skip_interval + 1) != 0:
            await asyncio.sleep(0.001) # Minimal sleep to yield control
            continue
        
        # --- Common Frame Processing Logic ---
        yolo_results = None
        try:
            # Run detection in executor to avoid blocking the event loop
            yolo_results = await loop.run_in_executor(None, detect, frame.copy())
        except Exception as e_detect:
            logger.error(f"video_processor: Error during YOLO detection: {e_detect}", exc_info=True)
        
        # Ensure spot_logic.SPOTS is up-to-date if configurations might change frequently
        # However, refresh_spots is DB intensive. Consider if needed every frame.
        # For now, assume spots are relatively static or updated via API which calls refresh_spots.
        
        # Use spot_logic.get_spot_states to get current occupancy from detections
        # The `get_spot_states` from the new spot_logic.py will be used.
        current_detected_occupancy = spot_logic.get_spot_states(yolo_results if yolo_results else [])


        now = datetime.utcnow()
        active_spot_labels_from_config = set(spot_logic.SPOTS.keys())

        # Sync previous_spot_states_global with current config (in case spots were added/removed via API)
        # Add new spots from config to global state trackers
        for spot_id_str in active_spot_labels_from_config:
            if spot_id_str not in previous_spot_states_global:
                logger.info(f"New spot {spot_id_str} detected from config in video_processor, initializing state.")
                previous_spot_states_global[spot_id_str] = False # Assume free initially
                empty_start[spot_id_str] = now
                notified[spot_id_str] = False
        # Remove stale spots from global state trackers
        for spot_id_str in list(previous_spot_states_global.keys()):
            if spot_id_str not in active_spot_labels_from_config:
                logger.info(f"Spot {spot_id_str} removed from config, cleaning up its state in video_processor.")
                previous_spot_states_global.pop(spot_id_str, None)
                empty_start.pop(spot_id_str, None)
                notified.pop(spot_id_str, None)


        for spot_id_str in active_spot_labels_from_config: # Iterate based on current config
            was_occupied = previous_spot_states_global.get(spot_id_str, False) # Default to false if somehow missing
            is_now_occupied = current_detected_occupancy.get(spot_id_str, False)

            if was_occupied != is_now_occupied:
                logger.info(f"Spot {spot_id_str} changed: {'Free' if was_occupied else 'Occupied'} -> {'Occupied' if is_now_occupied else 'Free'}")
                event_data = {
                    "type": "spot_update",
                    "data": {"spot_id": spot_id_str, "timestamp": now.isoformat() + "Z", "status": "occupied" if is_now_occupied else "free"}
                }
                enqueue_event(event_data)
                
                if is_now_occupied:
                    empty_start[spot_id_str] = None # Spot is now occupied
                else: # Spot became free
                    empty_start[spot_id_str] = now
                    notified[spot_id_str] = False # Reset notification status

            if not is_now_occupied and empty_start.get(spot_id_str) and not notified.get(spot_id_str, False):
                if (now - empty_start[spot_id_str]) >= VACANCY_DELAY:
                    logger.info(f"Spot {spot_id_str} confirmed vacant for {VACANCY_DELAY}, attempting FCM notification.")
                    try:
                        spot_id_int = int(spot_id_str)
                        # Run blocking FCM call in executor
                        await loop.run_in_executor(None, notify_users_for_spot_vacancy, spot_id_int)
                        notified[spot_id_str] = True
                        # Log VacancyEvent to DB
                        with Session(db_engine) as session_db:
                            evt = VacancyEvent(timestamp=now, spot_id=spot_id_int, camera_id="default_camera") # Ensure VacancyEvent model is correct
                            session_db.add(evt)
                            session_db.commit()
                            logger.info(f"Logged VacancyEvent for spot {spot_id_str} (ID: {spot_id_int}).")
                    except ValueError:
                        logger.error(f"Cannot send FCM for spot {spot_id_str}: spot_id is not a valid integer.")
                    except Exception as e_fcm:
                        logger.error(f"Error during FCM notification or DB logging for spot {spot_id_str}: {e_fcm}", exc_info=True)
            
            previous_spot_states_global[spot_id_str] = is_now_occupied

        # --- Draw overlays on a copy of the frame ---
        frame_to_display = frame.copy()
        # Draw defined spots
        for spot_label_str, spot_coords in spot_logic.SPOTS.items():
            if not isinstance(spot_coords, tuple) or len(spot_coords) != 4: continue
            sx, sy, sw, sh = spot_coords
            is_occupied = previous_spot_states_global.get(spot_label_str, False)
            color = (0, 0, 255) if is_occupied else (0, 255, 0) # Red if occupied, Green if free
            cv2.rectangle(frame_to_display, (sx, sy), (sx + sw, sy + sh), color, 2)
            cv2.putText(frame_to_display, spot_label_str, (sx, sy - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # Draw detected vehicle boxes (if yolo_results and structure is as expected)
        if yolo_results:
            for res in yolo_results:
                if hasattr(res, 'boxes') and res.boxes is not None:
                    vehicle_boxes_for_drawing = res.boxes.xyxy.tolist()
                    for x1, y1, x2, y2 in vehicle_boxes_for_drawing:
                         # Optionally filter by class if drawing all detected objects
                        cv2.rectangle(frame_to_display, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 255), 1) # Yellow for detections

        async with frame_access_lock:
            latest_frame_with_all_overlays = frame_to_display
        
        # --- Sleep to maintain target FPS ---
        processing_time = time.perf_counter() - processing_start_time
        sleep_duration = (1.0 / VIDEO_PROCESSING_FPS) - processing_time
        if sleep_duration > 0:
            await asyncio.sleep(sleep_duration)
        else: # If processing took longer than target frame interval
             await asyncio.sleep(0.001) # Minimal sleep to yield control

    logger.info("video_processor: Processing loop stopped.")
    if cap:
        cap.release()
    # video_capture_global = None # Should be set by make_capture or if loop breaks
    video_processing_active = False
    await manager.broadcast({"type": "video_ended", "data": {"message": "Video processing has stopped."}})


# --- FCM Notification Logic (from Codebase 1) ---
def notify_users_for_spot_vacancy(spot_id_int: int):
    spot_label_str = str(spot_id_int)
    logger.info(f"Attempting to notify users for newly vacant spot: {spot_label_str}")
    if not firebase_app_initialized:
        logger.warning(f"Firebase not initialized. Skipping FCM notification for spot {spot_label_str}.")
        return {"message": "Firebase not initialized."}
    try:
        with Session(db_engine) as session: # Use a new session
            device_tokens_records = session.exec(select(DeviceToken)).all() # Ensure DeviceToken model
            fcm_tokens = [record.token for record in device_tokens_records if record.token]
            if not fcm_tokens:
                logger.info(f"No FCM tokens found in database for spot {spot_label_str}.")
                return {"message": "No FCM tokens."}
            
            message_title = "Parking Spot Available!"
            message_body = f"Spot {spot_label_str} is now free."
            message = firebase_admin.messaging.MulticastMessage(
                notification=firebase_admin.messaging.Notification(title=message_title, body=message_body),
                tokens=fcm_tokens,
            )
            response = firebase_admin.messaging.send_multicast(message)
            logger.info(f'{response.success_count} FCM messages were sent successfully for spot {spot_label_str}')
            if response.failure_count > 0:
                failed_tokens_details = []
                for idx, resp_detail in enumerate(response.responses):
                    if not resp_detail.success:
                        failed_tokens_details.append({"token": fcm_tokens[idx], "error": str(resp_detail.exception)})
                logger.warning(f'FCM Failures for spot {spot_label_str}: {response.failure_count}. Details: {failed_tokens_details}')
            return {"message": f"FCM sent for spot {spot_label_str}", "success_count": response.success_count, "failure_count": response.failure_count}
    except Exception as e:
        logger.error(f"Error sending FCM notification for spot {spot_label_str}: {e}", exc_info=True)
        return {"message": f"Error sending FCM for spot {spot_label_str}"}


# --- FastAPI Event Handlers ---
@app.on_event("startup")
async def startup_event():
    global event_queue, previous_spot_states_global, empty_start, notified, video_processing_active

    logger.info("MERGED: Startup event - Attempting Robust Database Initialization...")
    db_initialized_successfully = False
    for attempt in range(DB_INIT_MAX_RETRIES):
        try:
            logger.info(f"MERGED: DB init attempt {attempt + 1}/{DB_INIT_MAX_RETRIES}...")
            # Test connection before calling init_db
            with db_engine.connect() as conn_test:
                conn_test.execute(select(1)) # Use SQLModel select
            logger.info("MERGED: DB engine connection test successful.")
            
            init_db() # This should create tables if they don't exist based on db.py
            logger.info("MERGED: init_db() called.")

            # Verify table existence (optional, but good for sanity check)
            with Session(db_engine) as session_verify:
                session_verify.exec(select(ParkingSpotConfig).limit(1)).first()
            logger.info("MERGED: Verified ParkingSpotConfig table exists after init_db.")
            db_initialized_successfully = True
            logger.info("MERGED: Database initialization successful.")
            break
        except Exception as e:
            logger.error(f"MERGED: Error during DB init attempt {attempt + 1}: {e}", exc_info=True) # Log full traceback
            if attempt < DB_INIT_MAX_RETRIES - 1:
                logger.info(f"MERGED: Retrying DB init in {DB_INIT_RETRY_DELAY} seconds...")
                await asyncio.sleep(DB_INIT_RETRY_DELAY)
            else:
                logger.error("MERGED: Max DB init retries reached. DB might not be initialized.")
    
    if db_initialized_successfully:
        try:
            spot_logic.refresh_spots() # Load spots from DB into spot_logic.SPOTS
            logger.info(f"MERGED: Spots loaded by spot_logic on startup: {len(spot_logic.SPOTS)} spots.")
            # Initialize global states based on loaded spots
            now = datetime.utcnow()
            for spot_id_str_key in spot_logic.SPOTS.keys():
                s_id = str(spot_id_str_key)
                previous_spot_states_global.setdefault(s_id, False) # Assume free
                empty_start.setdefault(s_id, now)
                notified.setdefault(s_id, False)
            logger.info(f"MERGED: Global spot states initialized for {len(previous_spot_states_global)} spots.")
        except Exception as e_sl:
            logger.error(f"MERGED: Error initializing spot_logic or global states after DB init: {e_sl}", exc_info=True)
    else:
        logger.critical("MERGED: DATABASE FAILED TO INITIALIZE PROPERLY. Application state may be inconsistent.")

    event_queue = asyncio.Queue(maxsize=200)
    logger.info("MERGED: Asyncio event queue for spot updates initialized.")
    asyncio.create_task(event_processor_task())
    logger.info("MERGED: WebSocket event processor task scheduled.")

    if not video_processing_active: # Start video processing task from Codebase 1
        logger.info("MERGED: Creating video processing task on startup...")
        asyncio.create_task(video_processor())
    else:
        logger.warning("MERGED: Video processing already marked active on startup (should not happen).")
    
    logger.info("MERGED: Application startup sequence complete.")


@app.on_event("shutdown")
async def shutdown_event(): # From Codebase 1
    global video_processing_active, video_capture_global
    logger.info("MERGED: Application shutdown sequence initiated...")
    video_processing_active = False
    if video_capture_global:
        logger.info("MERGED: Releasing global video capture object.")
        video_capture_global.release()
        video_capture_global = None
    
    # Allow some time for tasks to finish
    await asyncio.sleep(0.5) # Adjust as needed
    logger.info("MERGED: Video processing signaled to stop. Application shutdown complete.")


# --- API Endpoints ---
@app.get("/")
async def root():
    logger.info("Root path / accessed (health check).")
    return {"message": "Smart Parking API (Merged Version) is running"}

# NEW Spot Configuration GET Endpoint (from Codebase 2 logic, for App.js V11.18)
@app.get("/api/spots_v10_get")
async def get_spots_config_v2():
    logger.info("GET /api/spots_v10_get (Merged) hit.")
    spots_data_response = []
    try:
        spot_logic.refresh_spots() # Ensure SPOTS dict is up-to-date from DB
        logger.info(f"GET /api/spots_v10_get: spot_logic.SPOTS refreshed. Count: {len(spot_logic.SPOTS)}")

        if not spot_logic.SPOTS:
            logger.info("GET /api/spots_v10_get: No spots in spot_logic.SPOTS. Returning empty list.")
            return {"spots": []}

        for spot_id, coords in spot_logic.SPOTS.items():
            spot_id_str = str(spot_id)
            if isinstance(coords, tuple) and len(coords) == 4:
                # Get live status from previous_spot_states_global updated by video_processor
                is_available_status = not previous_spot_states_global.get(spot_id_str, False)
                spots_data_response.append({
                    "id": spot_id_str, "x": coords[0], "y": coords[1], "w": coords[2], "h": coords[3],
                    "is_available": is_available_status
                })
            else:
                logger.warning(f"GET /api/spots_v10_get: Malformed coords for spot_id {spot_id_str}: {coords}")
        
        logger.info(f"GET /api/spots_v10_get: Successfully prepared {len(spots_data_response)} spots.")
        return {"spots": spots_data_response}

    except Exception as e:
        logger.error(f"GET /api/spots_v10_get: Critical error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching spots: {str(e)}")

# NEW Spot Configuration POST Endpoint (from Codebase 2 logic, for App.js V11.18)
@app.post("/api/nuke_test_save")
async def save_spots_config_v2(payload: SpotsUpdateRequest):
    logger.info(">>>> POST /api/nuke_test_save (Merged) EXECUTING NOW! <<<<")
    logger.info(f"Received payload for spot save: {payload.model_dump_json(indent=2)}")
    default_camera_id = "default_camera" # Or make this configurable

    with Session(db_engine) as db: # Manual session management from Codebase 2
        try:
            statement_existing = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == default_camera_id)
            existing_spot_configs_db: List[ParkingSpotConfig] = db.exec(statement_existing).all()
            existing_spots_map: Dict[str, ParkingSpotConfig] = {
                str(config.spot_label): config for config in existing_spot_configs_db if hasattr(config, 'spot_label')
            }
            logger.info(f"POST /api/nuke_test_save: Fetched {len(existing_spots_map)} existing spots from DB.")

            incoming_spot_labels = set()
            response_spots_data = [] # For the response body

            for spot_in_model in payload.spots:
                label = str(spot_in_model.id)
                incoming_spot_labels.add(label)
                spot_x, spot_y, spot_w, spot_h = spot_in_model.x, spot_in_model.y, spot_in_model.w, spot_in_model.h

                if label in existing_spots_map: # Update existing spot
                    config_to_update = existing_spots_map[label]
                    config_to_update.x_coord, config_to_update.y_coord = spot_x, spot_y
                    config_to_update.width, config_to_update.height = spot_w, spot_h
                    config_to_update.updated_at = datetime.utcnow() # Assuming your model has this
                    db.add(config_to_update)
                    logger.info(f"Updating spot: {label}")
                else: # Add new spot
                    new_config = ParkingSpotConfig(
                        spot_label=label, camera_id=default_camera_id,
                        x_coord=spot_x, y_coord=spot_y,
                        width=spot_w, height=spot_h
                        # created_at=datetime.utcnow() # Assuming your model has this
                    )
                    db.add(new_config)
                    logger.info(f"Adding new spot: {label}")
                response_spots_data.append(spot_in_model.model_dump()) # Use Pydantic model's dict

            # Delete spots not in the incoming payload
            for existing_label_in_db, config_to_delete in existing_spots_map.items():
                if existing_label_in_db not in incoming_spot_labels:
                    logger.info(f"Deleting spot from DB: {existing_label_in_db}")
                    db.delete(config_to_delete)
            
            db.commit()
            logger.info("POST /api/nuke_test_save: Spot configuration saved successfully to database.")
            
            # Refresh spot_logic.SPOTS with new DB data
            spot_logic.refresh_spots()
            
            # Update global states (previous_spot_states_global, empty_start, notified)
            # This is critical for the video_processor to be aware of new/deleted spots
            current_db_spot_ids = set(spot_logic.SPOTS.keys()) # Get currently configured spot IDs
            now_for_new_spots = datetime.utcnow()

            # Remove spots from global states that are no longer in DB
            for sid_key_global in list(previous_spot_states_global.keys()):
                if str(sid_key_global) not in current_db_spot_ids:
                    previous_spot_states_global.pop(str(sid_key_global), None)
                    empty_start.pop(str(sid_key_global), None)
                    notified.pop(str(sid_key_global), None)
                    logger.info(f"Removed spot {sid_key_global} from global runtime states.")
            
            # Add new spots from DB to global states (initializing as 'free')
            for sid_key_db in current_db_spot_ids:
                s_id_db_str = str(sid_key_db)
                if s_id_db_str not in previous_spot_states_global:
                    previous_spot_states_global[s_id_db_str] = False # Assume new spot is free
                    empty_start[s_id_db_str] = now_for_new_spots
                    notified[s_id_db_str] = False
                    logger.info(f"Added new spot {s_id_db_str} to global runtime states (as free).")

            # Broadcast that the spot configuration has changed
            # The event payload should match what frontend expects for spots_config_updated
            current_spots_for_event = []
            for spot_label_event, spot_coords_event in spot_logic.SPOTS.items():
                 if isinstance(spot_coords_event, tuple) and len(spot_coords_event) == 4:
                    current_spots_for_event.append({
                        "id": spot_label_event, "x": spot_coords_event[0], "y": spot_coords_event[1],
                        "w": spot_coords_event[2], "h": spot_coords_event[3]
                    })
            enqueue_event({"type": "spots_config_updated", "data": {"spots": current_spots_for_event}})
            
            return {"message": "Spots saved to DB successfully!", "spots": response_spots_data}

        except ValidationError as ve:
            logger.error(f"POST /api/nuke_test_save Pydantic Validation Error: {ve.errors()}", exc_info=True)
            db.rollback()
            raise HTTPException(status_code=422, detail=ve.errors())
        except Exception as e:
            db.rollback()
            logger.error(f"POST /api/nuke_test_save internal server error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Internal server error while saving spots: {str(e)}")

# MJPEG Video Stream Endpoint (from Codebase 1)
@app.get("/webcam_feed")
async def mjpeg_webcam_feed():
    logger.info("Client connected to /webcam_feed (MJPEG).")
    async def generate_mjpeg_frames():
        global latest_frame_with_all_overlays, frame_access_lock
        while True:
            frame_to_send = None
            async with frame_access_lock:
                if latest_frame_with_all_overlays is not None:
                    frame_to_send = latest_frame_with_all_overlays.copy()
            
            if frame_to_send is not None:
                try:
                    flag, encodedImage = cv2.imencode(".jpg", frame_to_send)
                    if not flag:
                        logger.warning("MJPEG: Could not encode frame as JPG.")
                        await asyncio.sleep(0.1)
                        continue
                    yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + 
                           bytearray(encodedImage) + b'\r\n')
                except Exception as e_encode:
                    logger.error(f"Error encoding frame for MJPEG: {e_encode}", exc_info=True) # Log full error
                    # If an error occurs, break or continue carefully
                    await asyncio.sleep(0.1) # Prevent rapid error loops
                    continue # Or break, depending on desired behavior
            else:
                # If no frame is available, send a placeholder or just wait.
                # logger.debug("MJPEG: No frame available to send.") # Can be too verbose
                pass
            await asyncio.sleep(1.0 / 25) # Target ~25 FPS for MJPEG, adjust as needed
    return StreamingResponse(generate_mjpeg_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

# WebSocket for Spot Status Updates (from Codebase 1)
@app.websocket("/ws/spots")
async def websocket_spots_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current state of all spots upon connection
        current_statuses_for_ws = {}
        spot_logic.refresh_spots() # Ensure we have the latest spot list
        for label, spot_coords_ws in spot_logic.SPOTS.items():
            if isinstance(spot_coords_ws, tuple) and len(spot_coords_ws) == 4:
                occupied = previous_spot_states_global.get(label, False)
                current_statuses_for_ws[label] = {
                    "status": "occupied" if occupied else "free",
                    "timestamp": (empty_start.get(label) or datetime.utcnow()).isoformat() + "Z", # Provide a timestamp
                    "x": spot_coords_ws[0], "y": spot_coords_ws[1], 
                    "w": spot_coords_ws[2], "h": spot_coords_ws[3]
                }
        initial_data = {
            "type": "all_spot_statuses", # Frontend might expect this type
            "data": current_statuses_for_ws,
            "timestamp": time.time()
        }
        await websocket.send_text(json.dumps(initial_data, default=str))
        
        # Keep connection alive, listen for client messages (e.g., ping or specific requests)
        while True:
            # Use receive_text with timeout for periodic pings if client doesn't send
            try:
                 # If you expect client messages, handle them here. Otherwise, just keep alive.
                await asyncio.wait_for(websocket.receive_text(), timeout=60) 
            except asyncio.TimeoutError:
                 # Send a ping to keep connection alive if client is silent
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                logger.info(f"WebSocket client {websocket.client} disconnected from /ws/spots (explicitly).")
                break
            except Exception as e_ws_receive:
                logger.error(f"Error in /ws/spots receive loop for {websocket.client}: {e_ws_receive}", exc_info=True)
                break
    except WebSocketDisconnect:
        logger.info(f"WebSocket client {websocket.client} disconnected from /ws/spots.")
    except Exception as e:
        logger.error(f"WebSocket error for /ws/spots, client {websocket.client}: {e}", exc_info=True)
    finally:
        await manager.disconnect(websocket)


# WebSocket for Video Frame Upload from UI (from Codebase 1)
@app.websocket("/ws/video_stream_upload")
async def websocket_video_upload_endpoint(websocket: WebSocket):
    global video_frame_queue
    await websocket.accept()
    logger.info(f"Client connected to /ws/video_stream_upload: {websocket.client}")
    try:
        while True:
            data = await websocket.receive_text() # Expecting base64 encoded JPEG data URL

            if VIDEO_SOURCE_TYPE_ENV != "WEBSOCKET_STREAM":
                logger.warning(f"Received frame from {websocket.client} via WS, but backend VIDEO_SOURCE_TYPE is '{VIDEO_SOURCE_TYPE_ENV}'. Frame ignored.")
                await asyncio.sleep(0.1) # Small delay before checking next message
                continue # Ignore frame if not in correct mode

            if video_frame_queue.full():
                try:
                    # Discard oldest frame if queue is full to prevent indefinite growth
                    discarded_frame = video_frame_queue.get_nowait()
                    video_frame_queue.task_done()
                    logger.info(f"video_frame_queue was full. Discarded oldest frame to make space.")
                except asyncio.QueueEmpty:
                    pass # Should not happen if .full() was true, but good practice
            
            await video_frame_queue.put(data)
            # logger.debug(f"Frame PUT onto video_frame_queue. Queue size: {video_frame_queue.qsize()}") # Can be verbose

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from /ws/video_stream_upload: {websocket.client}")
    except Exception as e:
        logger.error(f"Error in /ws/video_stream_upload for {websocket.client}: {e}", exc_info=True)
    finally:
        logger.info(f"Closing WebSocket connection for /ws/video_stream_upload: {websocket.client}")


# FCM Token Registration Endpoint (from Codebase 1)
@app.post("/api/register_fcm_token")
async def register_fcm_token_api(payload: TokenRegistration, db: Session = Depends(SessionLocal)): # Original used SessionLocal
    logger.info(f"Attempting to register FCM token (first 20 chars): {payload.token[:20]}...")
    if not payload.token:
        raise HTTPException(status_code=400, detail="FCM token not provided")
    try:
        statement = select(DeviceToken).where(DeviceToken.token == payload.token)
        existing_token_record = db.exec(statement).first()
        if existing_token_record:
            logger.info(f"FCM token already registered: {payload.token[:20]}...")
            return {"message": "Token already registered."}
        
        new_token_record = DeviceToken(token=payload.token, platform=payload.platform)
        db.add(new_token_record)
        db.commit()
        db.refresh(new_token_record)
        logger.info(f"FCM token registered successfully: {payload.token[:20]}...")
        return {"message": "Token registered successfully."}
    except Exception as e:
        db.rollback()
        logger.error(f"Error registering FCM token {payload.token[:20]}...: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to register token.")

# Deprecated Spot API routes (optional, to catch old calls if any part of UI wasn't updated)
@app.get("/api/spots")
async def old_get_spots_config_api():
    logger.warning("Deprecated GET /api/spots hit. Please use /api/spots_v10_get.")
    # Optionally, redirect or call the new logic:
    # return await get_spots_config_v2()
    raise HTTPException(status_code=410, detail="This endpoint is deprecated. Use GET /api/spots_v10_get.")

@app.post("/api/spots")
async def old_post_spots_config_api():
    logger.warning("Deprecated POST /api/spots hit. Please use /api/nuke_test_save.")
    raise HTTPException(status_code=410, detail="This endpoint is deprecated. Use POST /api/nuke_test_save.")


if __name__ == "__main__":
    logger.info("Starting Uvicorn server for local development (Merged Version)...")
    # Example: Set for webcam streaming mode for local dev
    # os.environ["VIDEO_SOURCE_TYPE"] = "WEBSOCKET_STREAM"
    # os.environ["VIDEO_SOURCE_TYPE"] = "FILE" # Or FILE for testing with a local video
    # os.environ["VIDEO_SOURCE"] = "/path/to/your/test_video.mp4"
    # os.environ["DATABASE_URL"] = "sqlite:///./test_merged.db" # Use a distinct DB for testing
    
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)