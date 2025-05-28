# backend/app/main.py
import os
import cv2
import asyncio
import time
import traceback
from fastapi import FastAPI, WebSocket, HTTPException, Depends, Request, Body, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse 
from fastapi.staticfiles import StaticFiles 
from typing import List, Dict, Any, Optional
import json
import logging
from datetime import datetime, timedelta 
from pathlib import Path 
from pydantic import BaseModel, Field as PydanticField 
import base64 # Added for decoding base64 frames
import numpy as np # Added for OpenCV frame conversion

# AWS SDK for Python
import boto3

# Database and Spot Logic
from .db import engine as db_engine, ParkingSpotConfig, VacancyEvent, DeviceToken, SessionLocal, init_db, Base 
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


# --- Configuration & Globals ---
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
VIDEO_SOURCE_TYPE_ENV = os.getenv("VIDEO_SOURCE_TYPE", "FILE").upper() # Get video source type once

# --- Firebase Initialization ---
firebase_app_initialized = False
if FIREBASE_CRED_PATH:
    if os.path.exists(FIREBASE_CRED_PATH):
        try:
            logger.info(f"Attempting to read Firebase cred file at: {FIREBASE_CRED_PATH}")
            try:
                with open(FIREBASE_CRED_PATH, 'r', encoding='utf-8') as f:
                    content_sample = f.read(200) 
                    logger.info(f"First 200 chars of Firebase cred file (raw, repr): {repr(content_sample)}")
                    logger.info(f"First 200 chars of Firebase cred file (decoded): {content_sample}")
            except Exception as e_read:
                logger.error(f"Error reading Firebase cred file for debugging: {e_read}")

            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CRED_PATH)
                firebase_initialize_app(cred)
                logger.info("Firebase app initialized successfully.")
                firebase_app_initialized = True
            else:
                logger.info("Firebase app already initialized.")
                firebase_app_initialized = True
        except Exception as e:
            logger.error(f"Error initializing Firebase Admin SDK: {e}")
            logger.error("Full traceback for Firebase initialization error:")
            logger.error(traceback.format_exc()) 
    else:
        logger.warning(f"Firebase credentials file not found at path: {FIREBASE_CRED_PATH}. FCM notifications will be disabled.")
else:
    logger.warning("FIREBASE_CRED environment variable not set. FCM notifications will be disabled.")

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent 

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models for API Spot Configuration ---
class SpotConfigIn(BaseModel):
    id: str 
    x: int
    y: int
    w: int
    h: int

class SpotsUpdateRequest(BaseModel):
    spots: List[SpotConfigIn]

@app.get("/")
async def root():
    logger.info("Root path / accessed (health check).")
    return {"message": "Smart Parking API is running"}

# --- WebSocket Connection Manager for Spot Updates ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock() 

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        logger.info(f"WebSocket connection established: {websocket.client}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info(f"WebSocket connection closed: {websocket.client}")

    async def broadcast(self, data: Dict[str, Any]):
        message_str = json.dumps(data, default=str) 
        
        connections_to_remove = []
        async with self._lock:
            current_connections = list(self.active_connections)

        for connection in current_connections:
            try:
                await connection.send_text(message_str)
            except Exception as e:
                logger.error(f"Error broadcasting to WebSocket {connection.client}: {e}. Marking for removal.")
                connections_to_remove.append(connection)
        
        if connections_to_remove:
            async with self._lock:
                for ws in connections_to_remove:
                    if ws in self.active_connections:
                        self.active_connections.remove(ws)
manager = ConnectionManager()
event_queue: Optional[asyncio.Queue] = None

# --- Queue for incoming video frames from WebSocket ---
video_frame_queue: asyncio.Queue = asyncio.Queue(maxsize=3) # Max 100 frames in queue

async def event_processor_task():
    global event_queue
    if event_queue is None:
        logger.error("Event queue not initialized in event_processor_task. Exiting task.")
        return
        
    logger.info("Event processor task started.")
    while True:
        try:
            message_data = await event_queue.get()
            await manager.broadcast(message_data)
            event_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Event processor task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in event processor task: {e}")
            await asyncio.sleep(1) 

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

video_processing_active = False
video_capture_global: Optional[cv2.VideoCapture] = None
previous_spot_states_global: Dict[str, bool] = {} 
latest_frame_with_all_overlays: Optional[Any] = None 
frame_access_lock = asyncio.Lock() 
yolo_results_lock = asyncio.Lock() # Although not explicitly used in detect, good for future if detect becomes async
last_fcm_notification_times: Dict[str, float] = {} 
empty_start: Dict[str, Optional[datetime]] = {} 
notified: Dict[str, bool] = {} 

def get_kvs_hls_url(stream_name_or_arn, region_name=os.getenv("AWS_REGION", "us-east-2")):
    # (Existing KVS HLS URL fetching logic - unchanged)
    try:
        logger.info(f"Attempting to get HLS URL for KVS stream: {stream_name_or_arn} in region {region_name}")
        kvs_client = boto3.client('kinesisvideo', region_name=region_name)
        endpoint_params = {'StreamName': stream_name_or_arn, 'APIName': 'GET_HLS_STREAMING_SESSION_URL'}
        hls_params = {'StreamName': stream_name_or_arn, 'PlaybackMode': 'LIVE'}
        data_endpoint_response = kvs_client.get_data_endpoint(**endpoint_params)
        data_endpoint = data_endpoint_response['DataEndpoint']
        logger.info(f"KVS Data Endpoint: {data_endpoint}")
        kvs_media_client = boto3.client('kinesis-video-archived-media', endpoint_url=data_endpoint, region_name=region_name)
        hls_params['ContainerFormat'] = 'MPEG_TS' 
        hls_params['DiscontinuityMode'] = 'ALWAYS' 
        hls_params['DisplayFragmentTimestamp'] = 'ALWAYS'
        hls_params['Expires'] = 300 
        hls_url_response = kvs_media_client.get_hls_streaming_session_url(**hls_params)
        hls_url = hls_url_response['HLSStreamingSessionURL']
        logger.info(f"Successfully obtained KVS HLS URL.")
        return hls_url
    except Exception as e:
        logger.error(f"Error getting KVS HLS URL for '{stream_name_or_arn}': {e}")
        logger.error(traceback.format_exc())
        return None

def make_capture():
    # (Existing make_capture logic - largely unchanged, used if VIDEO_SOURCE_TYPE is not WEBSOCKET_STREAM)
    global video_capture_global
    # VIDEO_SOURCE_TYPE_ENV is now a global, set at startup
    env_video_source_value = os.getenv("VIDEO_SOURCE")
    logger.info(f"--- make_capture called. Video Source Type (from env): '{VIDEO_SOURCE_TYPE_ENV}', Value: '{env_video_source_value}' ---")
    
    # This function is now only relevant if not using WEBSOCKET_STREAM
    if VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM":
        logger.info("make_capture: VIDEO_SOURCE_TYPE is WEBSOCKET_STREAM. No cv2.VideoCapture will be created here.")
        return None

    video_source_value = env_video_source_value
    aws_region = os.getenv("AWS_REGION", "us-east-2")
    default_video_file = "/app/videos/test_video.mov" 

    if VIDEO_SOURCE_TYPE_ENV == "FILE" and not video_source_value:
        video_source_value = default_video_file
        logger.info(f"VIDEO_SOURCE not set for FILE type, defaulting to: {default_video_file}")
    elif not video_source_value and VIDEO_SOURCE_TYPE_ENV not in ["WEBCAM_INDEX", "WEBSOCKET_STREAM"]: 
         logger.error(f"Error: VIDEO_SOURCE environment variable not set for VIDEO_SOURCE_TYPE: {VIDEO_SOURCE_TYPE_ENV}")
         raise RuntimeError(f"VIDEO_SOURCE not set for {VIDEO_SOURCE_TYPE_ENV}")

    logger.info(f"Attempting to set up video source. Type: {VIDEO_SOURCE_TYPE_ENV}, Value: {video_source_value}, Region (if KVS): {aws_region}")
    cap = None
    hls_url_used = None

    if VIDEO_SOURCE_TYPE_ENV == "KVS_STREAM":
        if not video_source_value:
            raise RuntimeError("VIDEO_SOURCE (KVS stream name) not set for KVS_STREAM type.")
        hls_url_used = get_kvs_hls_url(stream_name_or_arn=video_source_value, region_name=aws_region)
        if hls_url_used:
            logger.info(f"Attempting to open KVS HLS stream with OpenCV: {hls_url_used}")
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|analyzeduration;2000000|probesize;1000000" 
            cap = cv2.VideoCapture(hls_url_used, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                logger.warning("Initial attempt to open HLS stream failed, retrying once after 5 seconds...")
                time.sleep(5) 
                cap = cv2.VideoCapture(hls_url_used, cv2.CAP_FFMPEG)
        else:
            logger.error(f"Could not get HLS URL for KVS stream: {video_source_value}")
            raise RuntimeError("Failed to get KVS HLS URL from AWS.")
    elif VIDEO_SOURCE_TYPE_ENV == "FILE":
        logger.info(f"Attempting to open video file: {video_source_value}")
        if not os.path.exists(video_source_value): 
            logger.error(f"Video file not found at path: {video_source_value}")
            raise RuntimeError(f"Video file not found: {video_source_value}")
        cap = cv2.VideoCapture(video_source_value) 
    elif VIDEO_SOURCE_TYPE_ENV == "WEBCAM_INDEX":
        try:
            idx = int(video_source_value if video_source_value is not None else "0") 
            logger.info(f"Attempting to open webcam index: {idx}")
            cap = cv2.VideoCapture(idx)
        except ValueError:
            logger.error(f"Invalid webcam index: {video_source_value}")
            raise RuntimeError(f"Invalid webcam index: {video_source_value}")
    elif VIDEO_SOURCE_TYPE_ENV not in ["WEBSOCKET_STREAM"]: # Other URL types
        if not video_source_value:
            raise RuntimeError(f"VIDEO_SOURCE not set for URL type: {VIDEO_SOURCE_TYPE_ENV}")
        logger.info(f"Attempting to open direct video URL: {video_source_value}")
        cap = cv2.VideoCapture(video_source_value, cv2.CAP_FFMPEG)
    
    if VIDEO_SOURCE_TYPE_ENV != "WEBSOCKET_STREAM":
        if cap is None or not cap.isOpened():
            current_source_for_error = hls_url_used if VIDEO_SOURCE_TYPE_ENV == "KVS_STREAM" and hls_url_used else video_source_value
            error_msg = f"FATAL: Could not open video source. Type='{VIDEO_SOURCE_TYPE_ENV}', SourceValue='{video_source_value}', EffectivePathForOpenCV='{current_source_for_error}'"
            logger.error(error_msg)
            raise RuntimeError(error_msg) 
        logger.info(f"Successfully opened video source using: {video_source_value if VIDEO_SOURCE_TYPE_ENV != 'KVS_STREAM' else 'KVS HLS Stream'}")
        video_capture_global = cap
    return cap


async def video_processor():
    global video_processing_active, video_capture_global, previous_spot_states_global
    global latest_frame_with_all_overlays, frame_access_lock, last_fcm_notification_times
    global empty_start, notified, video_frame_queue

    cap = None # For cv2.VideoCapture if used
    
    # Determine processing mode based on environment variable
    is_websocket_stream_mode = (VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM")

    if not is_websocket_stream_mode:
        try:
            logger.info("Video_processor: Initializing video capture via make_capture()...")
            cap = make_capture() 
            video_capture_global = cap 
        except RuntimeError as e:
            logger.error(f"Video_processor: CRITICAL - Failed to initialize video capture on startup: {e}")
            video_processing_active = False
            if 'manager' in globals():
                 await manager.broadcast({"type": "video_error", "data": {"error": "Video source failed on startup", "detail": str(e)}})
            return 
        logger.info("Video_processor: Video capture initialized via make_capture(). Starting processing loop.")
    else:
        logger.info("Video_processor: Mode is WEBSOCKET_STREAM. Will process frames from video_frame_queue.")

    video_processing_active = True
    VACANCY_DELAY = timedelta(seconds=FCM_VACANCY_DELAY_SECONDS)
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"} 
    loop = asyncio.get_event_loop()
    spot_logic.refresh_spots() 

    for spot_label_str in spot_logic.SPOTS.keys():
        if spot_label_str not in previous_spot_states_global: 
            previous_spot_states_global[spot_label_str] = False 
        if spot_label_str not in empty_start:
            empty_start[spot_label_str] = datetime.utcnow()
        if spot_label_str not in notified:
            notified[spot_label_str] = False
    logger.info(f"Initial spot states after refresh: {previous_spot_states_global}")
    
    frame_count = 0
    source_fps = 0
    if cap: # Only if using cv2.VideoCapture
        source_fps = cap.get(cv2.CAP_PROP_FPS) 
    
    frame_skip_interval = 0
    if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps:
        frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS)
    logger.info(f"Source FPS (if applicable): {source_fps}, Target Processing FPS: {VIDEO_PROCESSING_FPS}, Frame skip interval (if applicable): {frame_skip_interval}")

    while video_processing_active:
        frame = None
        ret = False

        if is_websocket_stream_mode: # This is VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM"
            try:
                logger.info("VIDEO_PROCESSOR: Attempting to GET frame from video_frame_queue...") # <--- ADD THIS
                frame_data_url = await asyncio.wait_for(video_frame_queue.get(), timeout=1.0)
                logger.info(f"VIDEO_PROCESSOR: GOT frame_data_url from queue (first 100 chars): {frame_data_url[:100]}") # <--- ADD THIS

                if frame_data_url.startswith('data:image/jpeg;base64,'):
                    base64_str = frame_data_url.split(',', 1)[1]
                    img_bytes = base64.b64decode(base64_str)
                    np_arr = np.frombuffer(img_bytes, np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        ret = True
                        video_frame_queue.task_done()
                        logger.info("VIDEO_PROCESSOR: Frame successfully decoded from base64.") # <--- ADD THIS
                    else:
                        logger.warning("VIDEO_PROCESSOR (WebSocket): Failed to decode frame from base64.")
                # ... rest of the try block ...
            except asyncio.TimeoutError:
                logger.debug("VIDEO_PROCESSOR (WebSocket): Timeout getting frame from queue. Queue might be empty.") # <--- Change to INFO if you want to see this often
                await asyncio.sleep(0.01) 
                continue 
            except Exception as e_ws_frame:
                logger.error(f"VIDEO_PROCESSOR (WebSocket): Error processing frame from queue: {e_ws_frame}")
                await asyncio.sleep(0.1)
                continue
        else: # Traditional cv2.VideoCapture mode
            if cap is None or not cap.isOpened():
                logger.warning("Video_processor: Video capture became un-opened. Attempting to re-initialize...")
                try:
                    if cap: cap.release()
                    cap = make_capture()
                    video_capture_global = cap
                    source_fps = cap.get(cv2.CAP_PROP_FPS) if cap else 0 
                    if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps:
                        frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS)
                    logger.info(f"Video_processor: Re-initialized video capture. New Source FPS: {source_fps}, Frame skip: {frame_skip_interval}")
                    frame_count = 0 
                except RuntimeError as e:
                    logger.error(f"Video_processor: Failed to re-initialize video capture: {e}. Stopping processing.")
                    video_processing_active = False
                    await manager.broadcast({"type": "video_error", "data": {"error": "Video source lost", "detail": str(e)}})
                    break
                except Exception as e_generic_reopen:
                    logger.error(f"Video_processor: Generic error re-opening capture: {e_generic_reopen}. Stopping.")
                    video_processing_active = False
                    break
            
            ret, frame = cap.read()

        if not ret or frame is None:
            if not is_websocket_stream_mode: # Only log/retry for non-websocket mode here
                logger.warning("Video_processor: Failed to grab frame or frame is None. Attempting to reopen.")
                if cap: cap.release()
                await asyncio.sleep(2) 
                try:
                    cap = make_capture()
                    video_capture_global = cap
                    if not (cap and cap.isOpened()): 
                        logger.error("Video_processor: Failed to re-open capture. Stopping."); break
                    logger.info("Video_processor: Successfully re-opened capture.")
                except Exception as e: 
                    logger.error(f"Video_processor: Error re-opening capture: {e}. Stopping."); break
            else: # For websocket mode, if frame is None after decode, just continue
                await asyncio.sleep(0.01)
            continue
        
        frame_count += 1
        # Frame skipping logic (only for non-websocket mode, or if FPS is set for websocket)
        if not is_websocket_stream_mode and frame_skip_interval > 0 and frame_count % (frame_skip_interval + 1) != 0:
            await asyncio.sleep(0.001) 
            continue
        
        # --- Common Frame Processing Logic ---
        yolo_results_list = None
        vehicle_boxes_for_spot_logic = [] 
        try:
            # Run detection in executor to avoid blocking the event loop
            yolo_results_list = await loop.run_in_executor(None, detect, frame.copy()) 
            if yolo_results_list: 
                for res in yolo_results_list:
                    if hasattr(res, 'boxes') and res.boxes is not None:
                        boxes_coords = res.boxes.xyxy.tolist() 
                        classes_indices = res.boxes.cls.tolist()
                        class_names_map = res.names 
                        for i, cls_idx_float in enumerate(classes_indices):
                            cls_idx = int(cls_idx_float)
                            if cls_idx in class_names_map and class_names_map[cls_idx] in vehicle_classes:
                                vehicle_boxes_for_spot_logic.append(boxes_coords[i])
                            # (Optional: logging for out-of-bounds class indices, if needed)
        except Exception as e_detect:
            logger.error(f"Video_processor: Error during YOLO detection: {e_detect}")
            logger.error(traceback.format_exc())
            vehicle_boxes_for_spot_logic = [] 

        current_detected_occupancy: Dict[str, bool] = {} 
        spot_logic.refresh_spots() # Ensure SPOTS dict is up-to-date
        active_spot_labels = set(spot_logic.SPOTS.keys())

        # (Spot state update logic - largely unchanged)
        for label_str in list(previous_spot_states_global.keys()):
            if label_str not in active_spot_labels:
                logger.info(f"Spot {label_str} removed from config, cleaning up its state in video_processor.")
                previous_spot_states_global.pop(label_str, None)
                empty_start.pop(label_str, None)
                notified.pop(label_str, None)
        for spot_label_str in active_spot_labels:
            if spot_label_str not in previous_spot_states_global:
                logger.info(f"New spot {spot_label_str} detected from config in video_processor, initializing state.")
                previous_spot_states_global[spot_label_str] = False 
                empty_start[spot_label_str] = datetime.utcnow()
                notified[spot_label_str] = False
        
        for spot_label_str, spot_coords in spot_logic.SPOTS.items():
            if not isinstance(spot_coords, tuple) or len(spot_coords) != 4:
                logger.error(f"Invalid spot_coords for {spot_label_str}: {spot_coords}. Skipping this spot in occupancy check.")
                continue
            sx, sy, sw, sh = spot_coords
            is_occupied_now = any(
                sx <= (bx1 + bx2) / 2 <= sx + sw and sy <= (by1 + by2) / 2 <= sy + sh
                for bx1, by1, bx2, by2 in vehicle_boxes_for_spot_logic
            )
            current_detected_occupancy[spot_label_str] = is_occupied_now
        
        now = datetime.utcnow()
        for spot_label_str in spot_logic.SPOTS.keys(): 
            was_occupied = previous_spot_states_global.get(spot_label_str, False) 
            is_now_occupied = current_detected_occupancy.get(spot_label_str, False) 

            if was_occupied != is_now_occupied:
                logger.info(f"Spot {spot_label_str} changed: {'Free' if was_occupied else 'Occupied'} -> {'Occupied' if is_now_occupied else 'Free'}")
                event_data = { "type": "spot_update", "data": { "spot_id": spot_label_str, "timestamp": now.isoformat() + "Z", "status": "occupied" if is_now_occupied else "free" } }
                enqueue_event(event_data)
                if is_now_occupied: 
                    empty_start[spot_label_str] = None
                else: 
                    empty_start[spot_label_str] = now
                    notified[spot_label_str] = False 
            
            if not is_now_occupied and empty_start.get(spot_label_str) and not notified.get(spot_label_str, False):
                if (now - empty_start[spot_label_str]) >= VACANCY_DELAY:
                    logger.info(f"Spot {spot_label_str} confirmed vacant for {VACANCY_DELAY}, attempting FCM notification.")
                    try:
                        spot_id_int = int(spot_label_str) 
                        await loop.run_in_executor(None, notify_users_for_spot_vacancy, spot_id_int)
                        notified[spot_label_str] = True 
                        with Session(db_engine) as session_db: 
                            evt = VacancyEvent(timestamp=now, spot_id=spot_id_int, camera_id="default_camera") 
                            session_db.add(evt)
                            session_db.commit()
                            logger.info(f"Logged VacancyEvent for spot {spot_label_str} (ID: {spot_id_int}).")
                    except ValueError:
                        logger.error(f"Cannot send FCM for spot {spot_label_str}: spot_label is not a valid integer.")
                    except Exception as e_fcm:
                        logger.error(f"Error during FCM notification for spot {spot_label_str}: {e_fcm}")
                        logger.error(traceback.format_exc())
            previous_spot_states_global[spot_label_str] = is_now_occupied 
        
        frame_to_display = frame.copy() # Make a copy for drawing
        # (Drawing logic - unchanged)
        for spot_label_str_draw, spot_coords_draw in spot_logic.SPOTS.items():
            if not isinstance(spot_coords_draw, tuple) or len(spot_coords_draw) != 4:
                continue 
            sx_draw, sy_draw, sw_draw, sh_draw = spot_coords_draw
            is_occupied = previous_spot_states_global.get(spot_label_str_draw, False)
            color = (0, 0, 255) if is_occupied else (0, 255, 0)
            cv2.rectangle(frame_to_display, (sx_draw, sy_draw), (sx_draw + sw_draw, sy_draw + sh_draw), color, 2)
            cv2.putText(frame_to_display, spot_label_str_draw, (sx_draw, sy_draw - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        for bx1, by1, bx2, by2 in vehicle_boxes_for_spot_logic:
            cv2.rectangle(frame_to_display, (int(bx1), int(by1)), (int(bx2), int(by2)), (0, 255, 255), 1) 
        
        async with frame_access_lock:
            latest_frame_with_all_overlays = frame_to_display
        
        # Sleep duration depends on mode
        if is_websocket_stream_mode:
            # For WebSocket, processing speed is dictated by incoming frames + VIDEO_PROCESSING_FPS
            # If VIDEO_PROCESSING_FPS is high, this sleep might be very short or zero.
            # If frames come faster than VIDEO_PROCESSING_FPS, queue might build up.
            # If frames come slower, it processes as they arrive.
            await asyncio.sleep(max(0.001, (1.0 / VIDEO_PROCESSING_FPS) - 0.01)) # Small adjustment for processing time
        else: # cv2.VideoCapture mode
            await asyncio.sleep(max(0.01, (1.0 / VIDEO_PROCESSING_FPS))) 

    logger.info("Video_processor: Processing loop stopped.")
    if cap:
        cap.release()
    video_capture_global = None
    video_processing_active = False
    if 'manager' in globals(): 
        await manager.broadcast({"type": "video_ended", "data": {"message": "Video processing has stopped."}})

def notify_users_for_spot_vacancy(spot_id_int: int): 
    # (Existing FCM notification logic - unchanged)
    spot_label_str = str(spot_id_int)
    logger.info(f"Attempting to notify users for newly vacant spot: {spot_label_str}")
    current_time = time.time()
    if not firebase_app_initialized: 
        logger.warning(f"Firebase not initialized. Skipping FCM notification for spot {spot_label_str}.") 
        return {"message": "Firebase not initialized."}
    try:
        with Session(db_engine) as session: 
            device_tokens_records = session.exec(select(DeviceToken)).all()
            fcm_tokens = [record.token for record in device_tokens_records if record.token]
            if not fcm_tokens:
                logger.info(f"No FCM tokens found in database to send notification for spot {spot_label_str}.")
                return {"message": "No FCM tokens."}
            message_title = "Parking Spot Available!"
            message_body = f"Spot {spot_label_str} is now free."
            message = firebase_admin.messaging.MulticastMessage( notification=firebase_admin.messaging.Notification(title=message_title, body=message_body), tokens=fcm_tokens,)
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
        logger.error(f"Error sending FCM notification for spot {spot_label_str}: {e}")
        logger.error(traceback.format_exc())
        return {"message": f"Error sending FCM for spot {spot_label_str}"}

@app.on_event("startup")
async def startup_event():
    # (Existing startup logic - largely unchanged)
    global video_processing_active, video_capture_global, previous_spot_states_global
    global event_queue, empty_start, notified 
    logger.info("Application startup sequence initiated...")
    init_db() 
    logger.info("Database initialized.")
    spot_logic.refresh_spots() 
    logger.info(f"Spots loaded from DB: {len(spot_logic.SPOTS)} spots.")
    for spot_label_str in spot_logic.SPOTS.keys():
        previous_spot_states_global[spot_label_str] = False 
        empty_start[spot_label_str] = datetime.utcnow()
        notified[spot_label_str] = False
    logger.info("Initial global spot state dictionaries (previous_spot_states_global, empty_start, notified) initialized.")
    event_queue = asyncio.Queue(maxsize=200) 
    logger.info("Asyncio event queue initialized.")
    asyncio.create_task(event_processor_task()) 
    logger.info("WebSocket event processor task scheduled.")
    if not video_processing_active:
        try:
            asyncio.create_task(video_processor())
            logger.info("Video processing task created on startup.")
        except Exception as e: 
            logger.error(f"Failed to create video_processor task on startup: {e}")
            logger.error(traceback.format_exc())
            if 'manager' in globals(): 
                await manager.broadcast({"type": "video_error", "data": {"error": "Video processing failed to start", "detail": str(e)}})
    else:
        logger.info("Video processing already marked active (should not happen on clean startup).")
    logger.info("Application startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    # (Existing shutdown logic - unchanged)
    global video_processing_active, video_capture_global
    logger.info("Application shutdown sequence initiated...")
    video_processing_active = False 
    if video_capture_global:
        logger.info("Releasing global video capture object.")
        video_capture_global.release()
        video_capture_global = None
    # Ensure video_frame_queue is emptied or handled if needed, though tasks should cancel
    logger.info("Signaling video_frame_queue consumers to stop (implicitly by video_processing_active=False)")
    await asyncio.sleep(0.5) 
    logger.info("Video processing signaled to stop. Application shutdown complete.")

@app.get("/api/spots")
async def get_spots_config_api(): 
    # (Existing API spots GET logic - unchanged)
    logger.info("GET /api/spots endpoint accessed.")
    spot_logic.refresh_spots() 
    spots_with_status = []
    for spot_label, spot_coords in spot_logic.SPOTS.items(): 
        if not isinstance(spot_coords, tuple) or len(spot_coords) != 4:
            logger.warning(f"Skipping spot {spot_label} due to invalid coordinate data: {spot_coords}")
            continue
        x, y, w, h = spot_coords
        is_occupied = previous_spot_states_global.get(str(spot_label), False)
        spots_with_status.append({ "id": str(spot_label), "x": x, "y": y, "w": w, "h": h, "is_available": not is_occupied })
    return {"spots": spots_with_status}

@app.post("/api/spots")
async def save_spots_config_api(payload: SpotsUpdateRequest, db: Session = Depends(SessionLocal)): 
    # (Existing API spots POST logic - unchanged)
    global previous_spot_states_global 
    logger.info("--- save_spots_config_api V3 EXECUTING (Pydantic version) ---")
    global previous_spot_states_global 
    logger.info(f"POST /api/spots received data (validated by Pydantic): {payload.dict()}") 
    try:
        default_camera_id = "default_camera" 
        statement_existing = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == default_camera_id)
        existing_spot_configs_db = db.exec(statement_existing).all()
        existing_spots_map = {config.spot_label: config for config in existing_spot_configs_db}
        incoming_spot_labels = {spot.id for spot in payload.spots} 
        for spot_in in payload.spots: 
            label = spot_in.id 
            if label in existing_spots_map: 
                config_to_update = existing_spots_map[label]
                config_to_update.x_coord = spot_in.x
                config_to_update.y_coord = spot_in.y
                config_to_update.width = spot_in.w
                config_to_update.height = spot_in.h
                db.add(config_to_update)
                logger.info(f"Updating spot: {label}")
            else: 
                new_config = ParkingSpotConfig(
                    spot_label=label, camera_id=default_camera_id,
                    x_coord=spot_in.x, y_coord=spot_in.y, 
                    width=spot_in.w, height=spot_in.h
                )
                db.add(new_config)
                logger.info(f"Adding new spot: {label}")
        for existing_label_in_db, config_to_delete in existing_spots_map.items():
            if existing_label_in_db not in incoming_spot_labels:
                logger.info(f"Deleting spot from DB: {existing_label_in_db}")
                db.delete(config_to_delete)
        db.commit()
        logger.info("Spot configuration saved successfully to database.")
        spot_logic.refresh_spots() 
        current_db_spot_labels = set(spot_logic.SPOTS.keys())
        for label_in_global_state in list(previous_spot_states_global.keys()):
            if label_in_global_state not in current_db_spot_labels:
                logger.info(f"Removing spot {label_in_global_state} from previous_spot_states_global.")
                previous_spot_states_global.pop(label_in_global_state, None)
        for label_from_db in current_db_spot_labels:
            if label_from_db not in previous_spot_states_global:
                 logger.info(f"Adding new spot {label_from_db} to previous_spot_states_global as free.")
                 previous_spot_states_global[label_from_db] = False 
        logger.info(f"Global previous_spot_states_global updated after config change. Current states: {previous_spot_states_global}")
        current_spots_for_event = []
        for spot_label, spot_coords_event in spot_logic.SPOTS.items():
            if isinstance(spot_coords_event, tuple) and len(spot_coords_event) == 4:
                 current_spots_for_event.append({
                     "id": spot_label, "x": spot_coords_event[0], "y": spot_coords_event[1],
                     "w": spot_coords_event[2], "h": spot_coords_event[3]
                 })
        enqueue_event({"type": "spots_config_updated", "data": {"spots": current_spots_for_event}}) 
        return {"message": "Spot configuration saved successfully", "spots": current_spots_for_event}
    except HTTPException: 
        raise
    except Exception as e: 
        db.rollback()
        logger.error(f"Error saving spot configuration: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/webcam_feed")
async def mjpeg_webcam_feed(): 
    # (Existing MJPEG feed logic - unchanged)
    logger.info("Client connected to /webcam_feed.")
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
                    yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encodedImage) + b'\r\n')
                except Exception as e_encode:
                    logger.error(f"Error encoding frame for MJPEG: {e_encode}")
                    await asyncio.sleep(0.1)
                    continue
            else:
                pass # logger.debug("MJPEG: No frame available to send.") # Potentially too verbose
            await asyncio.sleep(1.0 / 20) # Target ~20 FPS for MJPEG stream
    return StreamingResponse(generate_mjpeg_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/spots") 
async def websocket_spots_endpoint(websocket: WebSocket):
    # (Existing WebSocket for spot status - unchanged)
    await manager.connect(websocket)
    try:
        current_statuses_for_ws = {}
        spot_logic.refresh_spots() 
        for label, spot_coords_ws in spot_logic.SPOTS.items():
            if isinstance(spot_coords_ws, tuple) and len(spot_coords_ws) == 4:
                occupied = previous_spot_states_global.get(label, False)
                current_statuses_for_ws[label] = { "status": "occupied" if occupied else "free", "timestamp": datetime.utcnow().isoformat() + "Z", "x": spot_coords_ws[0], "y": spot_coords_ws[1], "w": spot_coords_ws[2], "h": spot_coords_ws[3] }
        initial_data = { "type": "all_spot_statuses", "data": current_statuses_for_ws, "timestamp": time.time() }
        await websocket.send_text(json.dumps(initial_data, default=str))
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=60) 
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                logger.info(f"WebSocket client {websocket.client} disconnected explicitly.")
                break 
            except Exception as e_ws_receive:
                logger.error(f"Error in WebSocket receive loop for {websocket.client}: {e_ws_receive}")
                break 
    except WebSocketDisconnect: 
        logger.info(f"WebSocket client {websocket.client} disconnected.")
    except Exception as e: 
        logger.error(f"WebSocket error for {websocket.client}: {e}")
    finally:
        await manager.disconnect(websocket)

# --- NEW: WebSocket Endpoint for Video Frame Upload ---
@app.websocket("/ws/video_stream_upload")
async def websocket_video_upload_endpoint(websocket: WebSocket):
    global video_frame_queue
    await websocket.accept()
    logger.info(f"Client connected to /ws/video_stream_upload: {websocket.client}") # You already have this
    try:
        while True:
            data = await websocket.receive_text() # Expecting base64 encoded JPEG data URL
            logger.info(f"DATA RECEIVED on /ws/video_stream_upload (first 100 chars): {data[:100]}") # <--- ADD THIS

            if not video_processing_active and VIDEO_SOURCE_TYPE_ENV != "WEBSOCKET_STREAM":
                logger.warning(f"Received frame from {websocket.client} but video processing is not active or not in WebSocket mode. Ignoring.")
                await asyncio.sleep(0.1) 
                continue

            if VIDEO_SOURCE_TYPE_ENV == "WEBSOCKET_STREAM":
                try:
                    if video_frame_queue.full(): # If you added the queue throttling logic
                        try:
                            discarded_frame_data_url = video_frame_queue.get_nowait()
                            video_frame_queue.task_done() 
                            logger.info(f"video_frame_queue was full. Discarded oldest frame.") # <--- CHANGE logger.debug TO logger.info
                        except asyncio.QueueEmpty:
                            pass 

                    await video_frame_queue.put(data)
                    logger.info(f"Frame PUT onto video_frame_queue. Queue size: {video_frame_queue.qsize()}") # <--- ADD THIS
                except Exception as e_queue_put:
                    logger.error(f"Error PUTTING frame onto queue from {websocket.client}: {e_queue_put}")
            else:
                logger.info(f"Received frame from {websocket.client}, but VIDEO_SOURCE_TYPE is not WEBSOCKET_STREAM. Frame ignored.")


    except WebSocketDisconnect:
        logger.info(f"Client disconnected from /ws/video_stream_upload: {websocket.client}")
    except Exception as e:
        logger.error(f"Error in /ws/video_stream_upload for {websocket.client}: {e}")
        logger.error(traceback.format_exc())
    finally:
        logger.info(f"Closing WebSocket connection for /ws/video_stream_upload: {websocket.client}")
        # No need to call manager.disconnect here as this is a separate purpose WebSocket

class TokenRegistration(BaseModel): 
    # (Existing FCM token registration model - unchanged)
    token: str
    platform: str = "android"

@app.post("/api/register_fcm_token")
async def register_fcm_token_api(payload: TokenRegistration, db: Session = Depends(SessionLocal)): 
    # (Existing FCM token registration API - unchanged)
    logger.info(f"Attempting to register FCM token: {payload.token[:20]}...") 
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
        logger.error(f"Error registering FCM token {payload.token[:20]}...: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to register token.")

if __name__ == "__main__":
    logger.info("Starting Uvicorn server for local development...")
    # Set VIDEO_SOURCE_TYPE for local testing if desired
    # os.environ["VIDEO_SOURCE_TYPE"] = "WEBSOCKET_STREAM" 
    # os.environ["VIDEO_SOURCE_TYPE"] = "FILE"
    # os.environ["VIDEO_SOURCE"] = "your_video_file.mp4" 
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
