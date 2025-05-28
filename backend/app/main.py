# backend/app/main.py
import os
import cv2
import asyncio
import time
import traceback
from fastapi import FastAPI, WebSocket, HTTPException, Depends, Request, Body, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse # Added FileResponse
from fastapi.staticfiles import StaticFiles # Added StaticFiles
from typing import List, Dict, Any, Optional
import json
import logging
from datetime import datetime, timedelta # Added timedelta
from pathlib import Path # Added Path

# AWS SDK for Python
import boto3

# Database and Spot Logic
from .db import engine as db_engine, ParkingSpotConfig, VacancyEvent, DeviceToken, SessionLocal, init_db, Base # init_db was create_db_and_tables
from . import spot_logic # Uses SPOTS from DB
from sqlmodel import Session, select # Added select for direct DB queries if needed

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, initialize_app as firebase_initialize_app # Renamed to avoid conflict

# Object Detection Model
try:
    from ..inference.cv_model import detect # Assuming inference is one level up from app
except ImportError:
    # Fallback if running locally where 'app' is the top level
    from inference.cv_model import detect
    print("WARN: Imported 'detect' from local 'inference' module. Ensure Docker structure matches.")


# --- Configuration & Globals ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL not set, using default SQLite for local dev.")
    DATABASE_URL = "sqlite:///./test.db"

FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED")
FCM_VACANCY_DELAY_SECONDS = int(os.getenv("FCM_VACANCY_DELAY_SECONDS", "5")) # Default to 5 seconds
VIDEO_PROCESSING_FPS = int(os.getenv("VIDEO_PROCESSING_FPS", "2")) # FPS for detection loop

# --- Firebase Initialization ---
firebase_app_initialized = False
if FIREBASE_CRED_PATH:
    if os.path.exists(FIREBASE_CRED_PATH):
        try:
            logger.info(f"Attempting to read Firebase cred file at: {FIREBASE_CRED_PATH}")
            # Removed direct file read here, SDK handles it.
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

# --- Paths (Primarily for local dev context, App Runner paths are in-container) ---
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent # This might be different in Docker vs Local

# --- FastAPI App Initialization ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Static Files & Root Endpoint (Adapted from local, ensure paths are correct for Docker) ---
# For App Runner, static files are typically handled by S3/CloudFront.
# This setup is more for local testing or if backend needs to serve some static content.
# The Dockerfile needs to ensure these paths are valid if used.
# Assuming 'static' is in the project root alongside 'backend' and 'ui' for local.
# In Docker, if static files are needed, they should be copied to a known location.
# For now, we'll keep the root endpoint simple for App Runner health checks.

@app.get("/")
async def root():
    logger.info("Root path / accessed (health check).")
    return {"message": "Smart Parking API is running"}

# --- WebSocket Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock() # Lock for modifying active_connections

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
        message_str = json.dumps(data, default=str) # Use default=str for datetime etc.
        
        # Iterate over a copy for safe removal
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

# --- Event Queue and Processor (Adapted from local) ---
# Using asyncio.Queue for async environment
event_queue: Optional[asyncio.Queue] = None

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
            await asyncio.sleep(1) # Avoid tight loop on continuous error

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

# --- Globals for Video Processing ---
video_processing_active = False
video_capture_global: Optional[cv2.VideoCapture] = None
previous_spot_states_global: Dict[str, bool] = {} # {spot_label_str: is_occupied_bool}
latest_frame_with_all_overlays: Optional[Any] = None # For MJPEG feed
frame_access_lock = asyncio.Lock() # To protect latest_frame_with_all_overlays
yolo_results_lock = asyncio.Lock() # To protect latest_yolo_detections_for_drawing (if used separately)
last_fcm_notification_times: Dict[str, float] = {} # {spot_label_str: timestamp}

# --- KVS Helper Function (from existing App Runner main.py) ---
def get_kvs_hls_url(stream_name_or_arn, region_name=os.getenv("AWS_REGION", "us-east-2")):
    try:
        logger.info(f"Attempting to get HLS URL for KVS stream: {stream_name_or_arn} in region {region_name}")
        kvs_client = boto3.client('kinesisvideo', region_name=region_name)
        
        endpoint_params = {'StreamName': stream_name_or_arn, 'APIName': 'GET_HLS_STREAMING_SESSION_URL'}
        hls_params = {'StreamName': stream_name_or_arn, 'PlaybackMode': 'LIVE'}
        
        data_endpoint_response = kvs_client.get_data_endpoint(**endpoint_params)
        data_endpoint = data_endpoint_response['DataEndpoint']
        logger.info(f"KVS Data Endpoint: {data_endpoint}")

        kvs_media_client = boto3.client('kinesis-video-archived-media', 
                                        endpoint_url=data_endpoint, 
                                        region_name=region_name)
        
        hls_params['ContainerFormat'] = 'MPEG_TS' 
        hls_params['DiscontinuityMode'] = 'ALWAYS' # Or 'NEVER' if stream is continuous
        hls_params['DisplayFragmentTimestamp'] = 'ALWAYS'
        hls_params['Expires'] = 300 # URL valid for 5 minutes

        hls_url_response = kvs_media_client.get_hls_streaming_session_url(**hls_params)
        hls_url = hls_url_response['HLSStreamingSessionURL']
        logger.info(f"Successfully obtained KVS HLS URL.")
        return hls_url
    except Exception as e:
        logger.error(f"Error getting KVS HLS URL for '{stream_name_or_arn}': {e}")
        logger.error(traceback.format_exc())
        return None

# --- Video Capture (from existing App Runner main.py, adapted) ---
def make_capture():
    global video_capture_global
    
    # Added explicit logging for env vars at the point of use
    env_video_source_type = os.getenv("VIDEO_SOURCE_TYPE")
    env_video_source_value = os.getenv("VIDEO_SOURCE")
    logger.info(f"--- make_capture called. Raw Env Vars: VIDEO_SOURCE_TYPE='{env_video_source_type}', VIDEO_SOURCE='{env_video_source_value}' ---")

    video_source_type = env_video_source_type.upper() if env_video_source_type else "FILE" # Default to FILE if not set
    video_source_value = env_video_source_value
    aws_region = os.getenv("AWS_REGION", "us-east-2")
    default_video_file = "/app/videos/test_video.mov"

    if video_source_type == "FILE" and not video_source_value:
        video_source_value = default_video_file
        logger.info(f"VIDEO_SOURCE not set for FILE type, defaulting to: {default_video_file}")
    elif not video_source_value and video_source_type != "WEBCAM_INDEX": # WEBCAM_INDEX can be 0
         logger.error(f"Error: VIDEO_SOURCE environment variable not set for VIDEO_SOURCE_TYPE: {video_source_type}")
         raise RuntimeError(f"VIDEO_SOURCE not set for {video_source_type}")

    logger.info(f"Attempting to set up video source. Type: {video_source_type}, Value: {video_source_value}, Region (if KVS): {aws_region}")
    cap = None
    hls_url_used = None

    if video_source_type == "KVS_STREAM":
        if not video_source_value:
            raise RuntimeError("VIDEO_SOURCE (KVS stream name) not set for KVS_STREAM type.")
        hls_url_used = get_kvs_hls_url(stream_name_or_arn=video_source_value, region_name=aws_region)
        if hls_url_used:
            logger.info(f"Attempting to open KVS HLS stream with OpenCV: {hls_url_used}")
            # Set environment variables for FFmpeg to handle HLS better if needed
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|analyzeduration;2000000|probesize;1000000" # Example options
            cap = cv2.VideoCapture(hls_url_used, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                logger.warning("Initial attempt to open HLS stream failed, retrying once after 5 seconds...")
                time.sleep(5) 
                cap = cv2.VideoCapture(hls_url_used, cv2.CAP_FFMPEG)
        else:
            logger.error(f"Could not get HLS URL for KVS stream: {video_source_value}")
            raise RuntimeError("Failed to get KVS HLS URL from AWS.")

    elif video_source_type == "FILE":
        # ... (file path logic from existing App Runner main.py) ...
        logger.info(f"Attempting to open video file: {video_source_value}")
        if not os.path.exists(video_source_value):
            logger.error(f"Video file not found at path: {video_source_value}")
            raise RuntimeError(f"Video file not found: {video_source_value}")
        cap = cv2.VideoCapture(video_source_value) # Removed CAP_FFMPEG for local files unless issues
    
    elif video_source_type == "WEBCAM_INDEX":
        try:
            idx = int(video_source_value if video_source_value is not None else "0") # Default to 0 if None
            logger.info(f"Attempting to open webcam index: {idx}")
            cap = cv2.VideoCapture(idx)
        except ValueError:
            logger.error(f"Invalid webcam index: {video_source_value}")
            raise RuntimeError(f"Invalid webcam index: {video_source_value}")
    else: # Direct URL (RTSP, HTTP, etc.)
        if not video_source_value:
            raise RuntimeError(f"VIDEO_SOURCE not set for URL type: {video_source_type}")
        logger.info(f"Attempting to open direct video URL: {video_source_value}")
        cap = cv2.VideoCapture(video_source_value, cv2.CAP_FFMPEG)
    
    if cap is None or not cap.isOpened():
        current_source_for_error = hls_url_used if video_source_type == "KVS_STREAM" and hls_url_used else video_source_value
        error_msg = f"FATAL: Could not open video source. Type='{video_source_type}', SourceValue='{video_source_value}', EffectivePathForOpenCV='{current_source_for_error}'"
        logger.error(error_msg)
        raise RuntimeError(error_msg) 
    
    logger.info(f"Successfully opened video source using: {video_source_value if video_source_type != 'KVS_STREAM' else 'KVS HLS Stream'}")
    video_capture_global = cap
    return cap


# --- Video Processing (Merged Logic) ---
async def video_processor():
    global video_processing_active, video_capture_global, previous_spot_states_global
    global latest_frame_with_all_overlays, frame_access_lock, last_fcm_notification_times
    
    cap = None
    try:
        logger.info("Video_processor: Attempting to initialize video capture...")
        cap = make_capture() # Uses the KVS-aware make_capture
        video_capture_global = cap # Ensure global is set if make_capture succeeds
    except RuntimeError as e:
        logger.error(f"Video_processor: CRITICAL - Failed to initialize video capture on startup: {e}")
        video_processing_active = False
        await manager.broadcast({"type": "video_error", "data": {"error": "Video source failed on startup", "detail": str(e)}})
        return # Stop processing if capture fails initially

    logger.info("Video_processor: Video capture initialized. Starting processing loop.")
    video_processing_active = True
    
    # State variables adapted from local version
    # prev_states is now previous_spot_states_global (string keys)
    empty_start: Dict[str, Optional[datetime]] = {} # {spot_label_str: datetime_obj_when_spot_became_empty}
    notified: Dict[str, bool] = {} # {spot_label_str: bool_if_notification_sent_for_current_vacancy}
    
    VACANCY_DELAY = timedelta(seconds=FCM_VACANCY_DELAY_SECONDS)
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"} # Make this configurable if needed

    loop = asyncio.get_event_loop()
    
    # Initial state setup
    spot_logic.refresh_spots() # Load spots from DB
    for spot_label_str in spot_logic.SPOTS.keys():
        previous_spot_states_global[spot_label_str] = False # Assume all free initially
        empty_start[spot_label_str] = datetime.utcnow()
        notified[spot_label_str] = False
    logger.info(f"Initial spot states: {previous_spot_states_global}")

    frame_count = 0
    source_fps = cap.get(cv2.CAP_PROP_FPS) if cap else 0
    frame_skip_interval = 0
    if source_fps > 0 and VIDEO_PROCESSING_FPS > 0 and VIDEO_PROCESSING_FPS < source_fps:
        frame_skip_interval = int(source_fps / VIDEO_PROCESSING_FPS)
    logger.info(f"Source FPS: {source_fps}, Target Processing FPS: {VIDEO_PROCESSING_FPS}, Frame skip interval: {frame_skip_interval}")

    while video_processing_active:
        if cap is None or not cap.isOpened():
            logger.warning("Video_processor: Video capture became un-opened. Attempting to re-initialize...")
            try:
                if cap: cap.release()
                cap = make_capture()
                video_capture_global = cap
                source_fps = cap.get(cv2.CAP_PROP_FPS) if cap else 0 # Re-get FPS
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
            logger.warning("Video_processor: Failed to grab frame or frame is None. Attempting to reopen.")
            # Re-open logic similar to above
            if cap: cap.release()
            await asyncio.sleep(2) # Wait before trying to reopen
            try:
                cap = make_capture()
                video_capture_global = cap
                if not (cap and cap.isOpened()): 
                    logger.error("Video_processor: Failed to re-open capture. Stopping."); break
                logger.info("Video_processor: Successfully re-opened capture.")
            except Exception as e: 
                logger.error(f"Video_processor: Error re-opening capture: {e}. Stopping."); break
            continue

        frame_count += 1
        if frame_skip_interval > 0 and frame_count % (frame_skip_interval + 1) != 0:
            await asyncio.sleep(0.001) 
            continue
        
        # --- Perform Detection ---
        yolo_results = None
        vehicle_boxes_for_spot_logic = [] # For spot logic
        try:
            # Run blocking detection in a thread pool executor
            yolo_results_list = await loop.run_in_executor(None, detect, frame.copy()) # detect should return a list of results

            if yolo_results_list: # Assuming detect returns a list of result objects (like Ultralytics)
                for res in yolo_results_list:
                    if hasattr(res, 'boxes') and res.boxes is not None:
                        boxes_coords = res.boxes.xyxy.tolist() # [x1, y1, x2, y2]
                        classes_indices = res.boxes.cls.tolist()
                        class_names_map = res.names # dict: {index: name}

                        for i, cls_idx_float in enumerate(classes_indices):
                            cls_idx = int(cls_idx_float)
                            if cls_idx < len(class_names_map) and class_names_map[cls_idx] in vehicle_classes:
                                vehicle_boxes_for_spot_logic.append(boxes_coords[i])
            # Store raw results if needed for drawing later by generate_frames
            # async with yolo_results_lock: latest_yolo_detections_for_drawing = yolo_results_list

        except Exception as e_detect:
            logger.error(f"Video_processor: Error during YOLO detection: {e_detect}")
            logger.error(traceback.format_exc())
            vehicle_boxes_for_spot_logic = [] # Ensure it's an empty list on error

        # --- Update Spot States (Adapted from local version) ---
        current_detected_occupancy: Dict[str, bool] = {} # {spot_label_str: is_occupied_bool}
        spot_logic.refresh_spots() # Ensure SPOTS is up-to-date from DB

        # Handle spots removed from config
        active_spot_labels = set(spot_logic.SPOTS.keys())
        for label_str in list(previous_spot_states_global.keys()):
            if label_str not in active_spot_labels:
                logger.info(f"Spot {label_str} removed from config, cleaning up state.")
                previous_spot_states_global.pop(label_str, None)
                empty_start.pop(label_str, None)
                notified.pop(label_str, None)
        
        for spot_label_str, (sx, sy, sw, sh) in spot_logic.SPOTS.items():
            # Initialize state for new spots
            if spot_label_str not in previous_spot_states_global:
                logger.info(f"New spot {spot_label_str} detected from config, initializing state.")
                previous_spot_states_global[spot_label_str] = False
                empty_start[spot_label_str] = datetime.utcnow()
                notified[spot_label_str] = False

            is_occupied_now = any(
                sx <= (bx1 + bx2) / 2 <= sx + sw and sy <= (by1 + by2) / 2 <= sy + sh
                for bx1, by1, bx2, by2 in vehicle_boxes_for_spot_logic
            )
            current_detected_occupancy[spot_label_str] = is_occupied_now

        now = datetime.utcnow()
        state_changed_for_broadcast = False

        for spot_label_str in spot_logic.SPOTS.keys(): # Iterate over currently configured spots
            was_occupied = previous_spot_states_global.get(spot_label_str, False)
            is_now_occupied = current_detected_occupancy.get(spot_label_str, False)

            if was_occupied != is_now_occupied:
                state_changed_for_broadcast = True
                logger.info(f"Spot {spot_label_str} changed: {'Free' if was_occupied else 'Occupied'} -> {'Occupied' if is_now_occupied else 'Free'}")
                event_data = {
                    "type": "spot_update",
                    "data": {
                        "spot_id": spot_label_str,
                        "timestamp": now.isoformat() + "Z",
                        "status": "occupied" if is_now_occupied else "free"
                    }
                }
                enqueue_event(event_data)

                if is_now_occupied: # Free -> Occupied
                    empty_start[spot_label_str] = None
                else: # Occupied -> Free
                    empty_start[spot_label_str] = now
                    notified[spot_label_str] = False # Reset notification status

            # FCM Notification Logic (for spots that just became free and stayed free)
            if not is_now_occupied and empty_start.get(spot_label_str) and not notified.get(spot_label_str, False):
                if (now - empty_start[spot_label_str]) >= VACANCY_DELAY:
                    logger.info(f"Spot {spot_label_str} confirmed vacant for {VACANCY_DELAY}, attempting FCM notification.")
                    try:
                        spot_id_int = int(spot_label_str) # Assuming spot_label can be cast to int for notify_all
                        # Run notify_users_for_spot_vacancy in executor as it might involve DB/network
                        await loop.run_in_executor(None, notify_users_for_spot_vacancy, spot_id_int)
                        notified[spot_label_str] = True
                        
                        # Log vacancy event to DB (can also be in notify_users_for_spot_vacancy)
                        with Session(db_engine) as session_db: # Use db_engine
                            evt = VacancyEvent(timestamp=now, spot_id=spot_id_int, camera_id="default_camera") # Use default_camera or get from config
                            session_db.add(evt)
                            session_db.commit()
                            logger.info(f"Logged VacancyEvent for spot {spot_label_str} (ID: {spot_id_int}).")

                    except ValueError:
                        logger.error(f"Cannot send FCM for spot {spot_label_str}: spot_label is not a valid integer.")
                    except Exception as e_fcm:
                        logger.error(f"Error during FCM notification for spot {spot_label_str}: {e_fcm}")
                        logger.error(traceback.format_exc())
            
            previous_spot_states_global[spot_label_str] = is_now_occupied


        # Broadcast all statuses if anything changed, or periodically
        # For simplicity, let's always broadcast current state to ensure new clients get it
        # This is already handled by individual spot_update events above if using the queue
        # If a full state broadcast is desired periodically, add it here.
        # For now, individual updates are sent.

        # --- Update frame for MJPEG stream ---
        frame_to_display = frame.copy()
        # Draw spot rectangles
        for spot_label_str_draw, (sx, sy, sw, sh) in spot_logic.SPOTS.items():
            is_occupied = previous_spot_states_global.get(spot_label_str_draw, False)
            color = (0, 0, 255) if is_occupied else (0, 255, 0)
            cv2.rectangle(frame_to_display, (sx, sy), (sx + sw, sy + sh), color, 2)
            cv2.putText(frame_to_display, spot_label_str_draw, (sx, sy - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        # Draw vehicle detection boxes
        for bx1, by1, bx2, by2 in vehicle_boxes_for_spot_logic:
            cv2.rectangle(frame_to_display, (int(bx1), int(by1)), (int(bx2), int(by2)), (0, 255, 255), 1) # Yellow

        async with frame_access_lock:
            global latest_frame_with_all_overlays
            latest_frame_with_all_overlays = frame_to_display
        
        # Control processing rate
        await asyncio.sleep(max(0.01, (1.0 / VIDEO_PROCESSING_FPS))) # Simple delay based on target FPS
            
    logger.info("Video_processor: Processing loop stopped.")
    if cap:
        cap.release()
    video_capture_global = None
    video_processing_active = False
    await manager.broadcast({"type": "video_ended", "data": {"message": "Video processing has stopped."}})


# --- Notification Logic (from existing App Runner main.py, ensure it's robust) ---
def notify_users_for_spot_vacancy(spot_id_int: int): # spot_id is int here
    spot_label_str = str(spot_id_int)
    logger.info(f"Attempting to notify users for newly vacant spot: {spot_label_str}")
    current_time = time.time()
    
    # Debounce logic is now implicitly handled by `notified[spot_label_str]` in video_processor
    # This function is called only after the delay and if not recently notified.

    if not firebase_app_initialized: # Check global flag
        logger.warning("Firebase not initialized. Skipping FCM notification for spot {spot_label_str}.")
        return {"message": "Firebase not initialized."}

    try:
        with Session(db_engine) as session: # Use db_engine
            device_tokens_records = session.exec(select(DeviceToken)).all()
            fcm_tokens = [record.token for record in device_tokens_records if record.token]

            if not fcm_tokens:
                logger.info(f"No FCM tokens found in database to send notification for spot {spot_label_str}.")
                return {"message": "No FCM tokens."}

            message_title = "Parking Spot Available!"
            message_body = f"Spot {spot_label_str} is now free."
            
            # Send to all tokens (can be inefficient for many tokens, consider topics)
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
            
            last_fcm_notification_times[spot_label_str] = current_time # Update last notification time
            return {"message": f"FCM sent for spot {spot_label_str}", "success_count": response.success_count, "failure_count": response.failure_count}

    except Exception as e:
        logger.error(f"Error sending FCM notification for spot {spot_label_str}: {e}")
        logger.error(traceback.format_exc())
        return {"message": f"Error sending FCM for spot {spot_label_str}"}


# --- FastAPI Event Handlers ---
@app.on_event("startup")
async def startup_event():
    global video_processing_active, video_capture_global, previous_spot_states_global
    global event_queue # Initialize event_queue here

    logger.info("Application startup sequence initiated...")
    init_db() # Ensure database and tables are created
    logger.info("Database initialized.")
    
    spot_logic.refresh_spots() # Load spots from DB
    logger.info(f"Spots loaded from DB: {len(spot_logic.SPOTS)} spots.")
    previous_spot_states_global = {str(spot_label): False for spot_label in spot_logic.SPOTS.keys()}
    logger.info("Initial spot states global dict initialized.")

    event_queue = asyncio.Queue(maxsize=200) # Initialize asyncio.Queue
    logger.info("Asyncio event queue initialized.")
    
    asyncio.create_task(event_processor_task()) # Start the new event processor
    logger.info("WebSocket event processor task scheduled.")

    if not video_processing_active:
        try:
            # make_capture() is called inside video_processor now
            asyncio.create_task(video_processor())
            logger.info("Video processing task created on startup.")
        except Exception as e: # Catch any error during task creation
            logger.error(f"Failed to create video_processor task on startup: {e}")
            logger.error(traceback.format_exc())
            await manager.broadcast({"type": "video_error", "data": {"error": "Video processing failed to start", "detail": str(e)}})
    else:
        logger.info("Video processing already marked active (should not happen on clean startup).")
    logger.info("Application startup complete.")


@app.on_event("shutdown")
async def shutdown_event():
    global video_processing_active, video_capture_global
    logger.info("Application shutdown sequence initiated...")
    video_processing_active = False 
    
    # Give tasks a moment to finish
    # Cancel pending tasks if needed, e.g., event_processor_task
    # For simplicity, we're relying on daemon threads or tasks ending when the loop stops.
    
    if video_capture_global:
        logger.info("Releasing global video capture object.")
        video_capture_global.release()
        video_capture_global = None
    
    # Wait briefly for the video_processor loop to exit
    await asyncio.sleep(0.5) 
    logger.info("Video processing signaled to stop. Application shutdown complete.")

# --- API Endpoints (Spots - from existing App Runner main.py, ensure consistency) ---
@app.get("/api/spots")
async def get_spots_config_api(): # Renamed to avoid conflict if any
    logger.info("GET /api/spots endpoint accessed.")
    spot_logic.refresh_spots() # Ensure latest from DB
    
    # Augment with current status
    spots_with_status = []
    for spot_label, (x, y, w, h) in spot_logic.SPOTS.items():
        is_occupied = previous_spot_states_global.get(str(spot_label), False)
        spots_with_status.append({
            "id": str(spot_label), # Assuming spot_label is the ID
            "x": x, "y": y, "w": w, "h": h,
            "is_available": not is_occupied
        })
    return {"spots": spots_with_status}


@app.post("/api/spots")
async def save_spots_config_api(request_body: Dict[str, List[Dict[str, Any]]], db: Session = Depends(SessionLocal)): # Use SessionLocal
    # Expected format: {"spots": [{"id": "1", "x": 10, "y": 20, "w": 30, "h": 40}, ...]}
    logger.info(f"POST /api/spots received data: {request_body}")
    spots_data = request_body.get("spots")
    if spots_data is None:
        raise HTTPException(status_code=400, detail="Missing 'spots' key in request body.")
    if not isinstance(spots_data, list):
        raise HTTPException(status_code=400, detail="'spots' must be a list.")

    try:
        default_camera_id = "default_camera" # Or get from request if multi-camera
        
        # Get existing spots from DB to find which to delete/update
        statement_existing = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == default_camera_id)
        existing_spot_configs = db.exec(statement_existing).all()
        existing_labels = {config.spot_label for config in existing_spot_configs}
        
        new_spot_labels = set()

        for spot_info in spots_data:
            label = str(spot_info.get("id"))
            new_spot_labels.add(label)
            x = spot_info.get("x")
            y = spot_info.get("y")
            w = spot_info.get("w")
            h = spot_info.get("h")

            if not all(isinstance(val, int) for val in [x, y, w, h]):
                db.rollback()
                raise HTTPException(status_code=400, detail=f"Invalid coordinate types for spot {label}. Must be integers.")

            existing_config = next((c for c in existing_spot_configs if c.spot_label == label), None)
            if existing_config: # Update existing
                existing_config.x_coord = x
                existing_config.y_coord = y
                existing_config.width = w
                existing_config.height = h
                db.add(existing_config)
            else: # Add new
                new_config = ParkingSpotConfig(
                    spot_label=label, camera_id=default_camera_id,
                    x_coord=x, y_coord=y, width=w, height=h
                )
                db.add(new_config)
        
        # Delete spots that were in DB but not in the new config
        for config_to_delete in existing_spot_configs:
            if config_to_delete.spot_label not in new_spot_labels:
                db.delete(config_to_delete)

        db.commit()
        logger.info("Spot configuration saved successfully to database.")
        
        spot_logic.refresh_spots() # Reload SPOTS global from DB
        
        # Re-initialize previous_spot_states_global for any new/removed spots
        current_labels_in_logic = set(spot_logic.SPOTS.keys())
        for label in list(previous_spot_states_global.keys()):
            if label not in current_labels_in_logic:
                previous_spot_states_global.pop(label, None)
        for label in current_labels_in_logic:
            if label not in previous_spot_states_global:
                 previous_spot_states_global[label] = False # Assume new spots are free

        logger.info(f"Global spot states updated after config change. Current states: {previous_spot_states_global}")
        enqueue_event({"type": "spots_config_updated", "data": spot_logic.SPOTS})
        
        return {"message": "Spot configuration saved successfully", "spots": spot_logic.SPOTS}

    except HTTPException: # Re-raise HTTP exceptions
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving spot configuration: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    # finally: # Session is managed by Depends, no need to close here.
        # db.close()


# --- MJPEG Webcam Feed ---
@app.get("/webcam_feed")
async def mjpeg_webcam_feed(): # Made async
    logger.info("Client connected to /webcam_feed.")
    async def generate_mjpeg_frames():
        global latest_frame_with_all_overlays, frame_access_lock
        while True:
            frame_to_send = None
            async with frame_access_lock:
                if latest_frame_with_all_overlays is not None:
                    frame_to_send = latest_frame_with_all_overlays.copy() # Send a copy
            
            if frame_to_send is not None:
                try:
                    flag, encodedImage = cv2.imencode(".jpg", frame_to_send)
                    if not flag:
                        logger.warning("MJPEG: Could not encode frame as JPG.")
                        await asyncio.sleep(0.1) # Avoid tight loop on encoding error
                        continue
                    yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + 
                           bytearray(encodedImage) + b'\r\n')
                except Exception as e_encode:
                    logger.error(f"Error encoding frame for MJPEG: {e_encode}")
                    # Potentially break or just skip this frame
                    await asyncio.sleep(0.1)
                    continue
            else:
                # Send a placeholder or just wait if no frame is ready
                # For now, just wait briefly.
                pass # logger.debug("MJPEG: No new frame ready, waiting.")

            await asyncio.sleep(1.0 / 20) # Target ~20 FPS for MJPEG stream, adjust as needed
                                        # This also dictates how quickly client sees updates
                                        # if video_processor updates latest_frame_with_all_overlays faster.
    return StreamingResponse(generate_mjpeg_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

# --- WebSocket Endpoint (from existing App Runner main.py, ensure consistency) ---
@app.websocket("/ws/spots") # Changed from /ws to match existing
async def websocket_spots_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current state upon connection
        current_statuses_for_ws = {
            label: {"status": "occupied" if occupied else "free", 
                    "timestamp": datetime.utcnow().isoformat() + "Z"} 
            for label, occupied in previous_spot_states_global.items()
        }
        initial_data = {
            "type": "all_spot_statuses",
            "data": current_statuses_for_ws, # Send current states
            "timestamp": time.time()
        }
        await websocket.send_text(json.dumps(initial_data, default=str))
        
        while True:
            # Keep connection alive, updates are broadcast by event_processor_task
            # Handle client messages if any, or implement ping/pong
            try:
                # Wait for a message from client or timeout for keep-alive
                await asyncio.wait_for(websocket.receive_text(), timeout=60) 
            except asyncio.TimeoutError:
                # Send a ping to keep connection alive if no message from client
                await websocket.send_text(json.dumps({"type": "ping"}))
            except WebSocketDisconnect:
                logger.info(f"WebSocket client {websocket.client} disconnected explicitly.")
                break # Exit loop on disconnect
            except Exception as e_ws_receive:
                logger.error(f"Error in WebSocket receive loop for {websocket.client}: {e_ws_receive}")
                break # Exit loop on other errors

    except WebSocketDisconnect: # Catch disconnect if it happens during connect or initial send
        logger.info(f"WebSocket client {websocket.client} disconnected.")
    except Exception as e: 
        logger.error(f"WebSocket error for {websocket.client}: {e}")
    finally:
        await manager.disconnect(websocket)


# --- FCM Token Registration (from existing App Runner main.py) ---
class TokenRegistration(BaseModel): # For request body validation
    token: str
    platform: str = "android"

@app.post("/api/register_fcm_token")
async def register_fcm_token_api(payload: TokenRegistration, db: Session = Depends(SessionLocal)): # Use SessionLocal
    logger.info(f"Attempting to register FCM token: {payload.token[:20]}...") # Log partial token
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
    # This block is for running with `python main.py` directly (local dev)
    # App Runner will use a command like `uvicorn app.main:app --host 0.0.0.0 --port 8000`
    # For local testing, ensure VIDEO_SOURCE points to a local file or webcam index.
    # And FIREBASE_CRED points to your local firebase-sa.json
    # And DATABASE_URL points to a local/accessible DB.
    logger.info("Starting Uvicorn server for local development...")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
