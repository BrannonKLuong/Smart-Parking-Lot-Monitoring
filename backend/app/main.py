# backend/app/main.py
import os
import cv2
import asyncio
import time # Added for sleep in make_capture retry
import traceback # For detailed error logging in KVS HLS URL fetching
from fastapi import FastAPI, WebSocket, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from typing import List, Dict, Any
import json # Keep for potential use
import logging
#
# AWS SDK for Python
import boto3

# Database and Spot Logic
from .db import engine as db_engine, ParkingSpotConfig, VacancyEvent, DeviceToken, SessionLocal, init_db, Base
from . import spot_logic
from sqlmodel import Session, select

# Firebase Admin SDK (ensure FIREBASE_CRED env var is set to the path of your serviceAccountKey.json)
import firebase_admin
from firebase_admin import credentials, messaging

# --- Configuration & Globals ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
from dotenv import load_dotenv
load_dotenv() # Load from .env file if present (useful for local dev)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL not set, using default SQLite.")
    DATABASE_URL = "sqlite:///./test.db" # Fallback for local dev if .env is missing

FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED")
FCM_VACANCY_DELAY_SECONDS = int(os.getenv("FCM_VACANCY_DELAY_SECONDS", "30")) # Default to 30 seconds

# --- Firebase Initialization ---
try:
    if FIREBASE_CRED_PATH and os.path.exists(FIREBASE_CRED_PATH):
        if not firebase_admin._apps: # Check if already initialized
            cred = credentials.Certificate(FIREBASE_CRED_PATH)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase app initialized successfully.")
        else:
            logger.info("Firebase app already initialized.")
    else:
        logger.warning(f"Firebase credentials not found at path: {FIREBASE_CRED_PATH}. FCM notifications will be disabled.")
        FIREBASE_CRED_PATH = None # Disable FCM
except Exception as e:
    logger.error(f"Error initializing Firebase Admin SDK: {e}")
    FIREBASE_CRED_PATH = None # Disable FCM

# --- Database Initialization ---
# Moved init_db to be callable, will be called at startup
# The global 'engine' from db.py is used by spot_logic and other parts

# --- WebSocket Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connection established: {websocket.client}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket connection closed: {websocket.client}")

    async def broadcast(self, data: Dict[str, Any]):
        # Using json.dumps to ensure proper JSON string format for complex data
        message = json.dumps(data) 
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.error(f"Error broadcasting to WebSocket {connection.client}: {e}")
                # Potentially remove broken connections here
                # self.active_connections.remove(connection) # Be careful with modifying list while iterating

manager = ConnectionManager()

# --- FastAPI App Initialization ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows all origins
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods
    allow_headers=["*"], # Allows all headers
)

# Global state for video processing
video_processing_active = False
video_capture_global = None
previous_spot_states_global: Dict[str, bool] = {}
last_fcm_notification_times: Dict[str, float] = {} # {spot_label: timestamp}


# --- KVS Helper Function ---
def get_kvs_hls_url(stream_name_or_arn, region_name=os.getenv("AWS_REGION", "us-east-2")):
    """
    Retrieves an HLS streaming session URL for a given Kinesis Video Stream.
    """
    try:
        logger.info(f"Attempting to get HLS URL for KVS stream: {stream_name_or_arn} in region {region_name}")
        # Ensure Boto3 client uses credentials from the App Runner instance role
        kvs_client = boto3.client('kinesisvideo', region_name=region_name)
        
        if stream_name_or_arn.startswith("arn:aws:kinesisvideo:"):
            endpoint_params = {'StreamARN': stream_name_or_arn, 'APIName': 'GET_HLS_STREAMING_SESSION_URL'}
            hls_params = {'StreamARN': stream_name_or_arn, 'PlaybackMode': 'LIVE'}
        else: # Assume it's a stream name
            endpoint_params = {'StreamName': stream_name_or_arn, 'APIName': 'GET_HLS_STREAMING_SESSION_URL'}
            hls_params = {'StreamName': stream_name_or_arn, 'PlaybackMode': 'LIVE'}

        data_endpoint_response = kvs_client.get_data_endpoint(**endpoint_params)
        data_endpoint = data_endpoint_response['DataEndpoint']
        logger.info(f"KVS Data Endpoint: {data_endpoint}")

        kvs_media_client = boto3.client('kinesis-video-archived-media', 
                                        endpoint_url=data_endpoint, 
                                        region_name=region_name)
        
        hls_params['ContainerFormat'] = 'MPEG_TS' 
        hls_params['DiscontinuityMode'] = 'ALWAYS'
        hls_params['Expires'] = 3600 # URL valid for 1 hour

        hls_url_response = kvs_media_client.get_hls_streaming_session_url(**hls_params)
        hls_url = hls_url_response['HLSStreamingSessionURL']
        logger.info(f"Successfully obtained KVS HLS URL.") # Sensitive URL itself not logged
        return hls_url
    except Exception as e:
        logger.error(f"Error getting KVS HLS URL for '{stream_name_or_arn}': {e}")
        logger.error(traceback.format_exc()) # Print full traceback for debugging
        return None

# --- Video Capture and Processing ---
def make_capture():
    logger.info("--- make_capture called, VIDEO_SOURCE_TYPE from env: %s ---", os.getenv("VIDEO_SOURCE_TYPE"))
    global video_capture_global
    video_source_type = os.getenv("VIDEO_SOURCE_TYPE", "FILE").upper()
    video_source_value = os.getenv("VIDEO_SOURCE") 
    aws_region = os.getenv("AWS_REGION", "us-east-2")

    default_video_file = "/app/videos/test_video.mov" # Default if no source specified for FILE type

    if video_source_type == "FILE" and not video_source_value:
        video_source_value = default_video_file
        logger.info(f"VIDEO_SOURCE not set, defaulting to: {default_video_file}")
    elif not video_source_value:
         logger.error(f"Error: VIDEO_SOURCE environment variable not set for VIDEO_SOURCE_TYPE: {video_source_type}")
         raise RuntimeError(f"VIDEO_SOURCE not set for {video_source_type}")

    logger.info(f"Attempting to set up video source. Type: {video_source_type}, Value: {video_source_value}, Region (if KVS): {aws_region}")
    cap = None
    hls_url_used = None # For logging in case of failure

    if video_source_type == "KVS_STREAM":
        hls_url_used = get_kvs_hls_url(stream_name_or_arn=video_source_value, region_name=aws_region)
        if hls_url_used:
            logger.info(f"Attempting to open KVS HLS stream with OpenCV.")
            cap = cv2.VideoCapture(hls_url_used, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                logger.warning("Initial attempt to open HLS stream failed, retrying once after 5 seconds...")
                time.sleep(5) 
                cap = cv2.VideoCapture(hls_url_used, cv2.CAP_FFMPEG)
        else:
            logger.error(f"Could not get HLS URL for KVS stream: {video_source_value}")
            raise RuntimeError("Failed to get KVS HLS URL from AWS.")

    elif video_source_type == "FILE":
        # Adjust path for Docker if it's relative and doesn't start with /app/
        if not os.path.isabs(video_source_value) and not video_source_value.startswith("/app/"):
             video_source_value_adjusted = os.path.join("/app", video_source_value)
             # Use adjusted path only if it exists or if original relative one doesn't
             if os.path.exists(video_source_value_adjusted) or not os.path.exists(video_source_value):
                video_source_value = video_source_value_adjusted
        
        logger.info(f"Attempting to open video file: {video_source_value}")
        if not os.path.exists(video_source_value):
            logger.error(f"Video file not found at path: {video_source_value}")
            raise RuntimeError(f"Video file not found: {video_source_value}")
        cap = cv2.VideoCapture(video_source_value, cv2.CAP_FFMPEG)
    
    elif video_source_type == "WEBCAM_INDEX":
        try:
            idx = int(video_source_value)
            logger.info(f"Attempting to open webcam index: {idx}")
            cap = cv2.VideoCapture(idx)
        except ValueError:
            logger.error(f"Invalid webcam index: {video_source_value}")
            raise RuntimeError(f"Invalid webcam index: {video_source_value}")
    else: # Direct URL (RTSP, HTTP, etc.)
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

async def video_processor():
    global video_processing_active, video_capture_global, previous_spot_states_global, last_fcm_notification_times
    
    if video_capture_global is None or not video_capture_global.isOpened():
        try:
            logger.info("Video capture not open, attempting to initialize in video_processor...")
            video_capture_global = make_capture()
        except RuntimeError as e:
            logger.error(f"Failed to initialize video capture in video_processor: {e}")
            video_processing_active = False
            # Broadcast error state
            error_data = {"error": "Video source failed", "detail": str(e)}
            await manager.broadcast({"type": "video_error", "data": error_data})
            return # Stop processing if capture fails

    logger.info("Video processing loop started.")
    video_processing_active = True
    fps = video_capture_global.get(cv2.CAP_PROP_FPS)
    processing_fps = int(os.getenv("VIDEO_PROCESSING_FPS", "5")) # Process at 5 FPS by default
    frame_skip = 0
    if fps > 0 and processing_fps > 0 and processing_fps < fps :
        frame_skip = int(fps / processing_fps)
    logger.info(f"Source FPS: {fps}, Target Processing FPS: {processing_fps}, Frame skip: {frame_skip}")
    
    frame_count = 0
    while video_processing_active:
        if video_capture_global is None or not video_capture_global.isOpened():
            logger.warning("Video capture became un-opened. Attempting to re-initialize...")
            try:
                video_capture_global = make_capture() # Try to re-open
                fps = video_capture_global.get(cv2.CAP_PROP_FPS) # Re-get FPS
                if fps > 0 and processing_fps > 0 and processing_fps < fps :
                    frame_skip = int(fps / processing_fps)
                logger.info(f"Re-initialized video capture. New Source FPS: {fps}, Frame skip: {frame_skip}")
                frame_count = 0 # Reset frame count
            except RuntimeError as e:
                logger.error(f"Failed to re-initialize video capture: {e}. Stopping video processing.")
                video_processing_active = False
                error_data = {"error": "Video source lost and failed to recover", "detail": str(e)}
                await manager.broadcast({"type": "video_error", "data": error_data})
                break # Exit the loop

        ret, frame = video_capture_global.read()
        if not ret:
            logger.warning("Failed to grab frame or end of stream. Attempting to reopen capture.")
            if video_capture_global:
                video_capture_global.release()
            try:
                video_capture_global = make_capture() # Try to re-open
                fps = video_capture_global.get(cv2.CAP_PROP_FPS) # Re-get FPS
                if fps > 0 and processing_fps > 0 and processing_fps < fps :
                    frame_skip = int(fps / processing_fps)
                logger.info(f"Re-opened video capture. New Source FPS: {fps}, Frame skip: {frame_skip}")
                frame_count = 0 # Reset frame count
                continue # Try to read the next frame
            except RuntimeError as e:
                logger.error(f"Failed to re-open video capture: {e}. Stopping video processing.")
                video_processing_active = False
                error_data = {"error": "Video source lost", "detail": str(e)}
                await manager.broadcast({"type": "video_error", "data": error_data})
                break # Exit the loop

        frame_count += 1
        if frame_skip > 0 and frame_count % (frame_skip + 1) != 0:
            await asyncio.sleep(0.001) # Small sleep to prevent tight loop on high FPS source
            continue

     
        # TODO: Add actual YOLOv8 processing here if you integrate a model directly
        # For now, using placeholder logic for spot states based on spot_logic
        
        # Using spot_logic to get current states (assuming detections would be passed here)
        # Placeholder: In a real scenario, 'detections' would come from your YOLO model.
        # For now, this example won't change spot states unless spot_logic is modified
        # or we simulate detections. Let's assume spot_logic.get_spot_states can take None
        # if no detections are available from the current frame (which it does)
        current_spot_states = spot_logic.get_spot_states(detections=None) # Pass None if no YOLO yet

        # Vacancy detection logic (moved from main app to here)
        # This part is simplified and might not work as expected without actual detections
        # that change 'current_spot_states'.
        # For a robust system, this should use real detection results.
        
        # For now, to test FCM and DB logging, we can simulate state changes if needed.
        # For example:
        # import random
        # for spot in current_spot_states:
        #     if random.random() < 0.01: # Simulate a 1% chance of flipping state per processing cycle
        #         current_spot_states[spot] = not current_spot_states.get(spot, False)

        newly_vacant_spot_ids = spot_logic.detect_vacancies(
            previous_spot_states_global, 
            current_spot_states,
            broadcast_func=lambda data: asyncio.create_task(manager.broadcast({"type": "spot_update", "data": data})),
            notify_func=notify_users_for_spot_vacancy # Pass the actual notification function
        )
        
        previous_spot_states_global = current_spot_states.copy()

        # Broadcast all current spot statuses (even if unchanged)
        # This allows new clients to get the current state immediately
        # And also provides a heartbeat / regular update
        all_statuses_data = {
            "type": "all_spot_statuses",
            "data": current_spot_states,
            "timestamp": time.time() # Add a timestamp
        }
        await manager.broadcast(all_statuses_data)
        
        # Delay to control processing rate
        # The actual processing rate will also depend on how long get_spot_states takes
        if fps > 0 and processing_fps > 0 :
            await asyncio.sleep(1.0 / processing_fps)
        else: # Default if FPS is unknown or processing_fps is 0 (e.g. single image)
            await asyncio.sleep(0.2) # 5 FPS equivalent
            
    logger.info("Video processing loop stopped.")
    if video_capture_global:
        video_capture_global.release()
        video_capture_global = None
    video_processing_active = False
    # Broadcast video ended state
    await manager.broadcast({"type": "video_ended", "data": {"message": "Video processing has stopped."}})

async def generate_frames():
    global video_capture_global
    logger.info("MJPEG frame generation started.")
    
    while video_processing_active and video_capture_global and video_capture_global.isOpened():
        ret, frame = video_capture_global.read() # Read from the global capture object
        if not ret:
            logger.warning("MJPEG: Failed to grab frame for streaming.")
            # If video_processor also fails to reopen, it will set video_processing_active to False
            # Add a small delay to prevent tight loop on error
            await asyncio.sleep(0.1)
            continue

        # Draw rectangles on the frame based on current spot_logic.SPOTS
        for spot_label, (x, y, w, h) in spot_logic.SPOTS.items():
            is_occupied = previous_spot_states_global.get(str(spot_label), False) # Ensure spot_label is string
            color = (0, 0, 255) if is_occupied else (0, 255, 0) # Red if occupied, Green if free
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(frame, str(spot_label), (x, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        
        try:
            (flag, encodedImage) = cv2.imencode(".jpg", frame)
            if not flag:
                logger.warning("MJPEG: Could not encode frame as JPG.")
                continue
            yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + 
                   bytearray(encodedImage) + b'\r\n')
        except Exception as e:
            logger.error(f"Error encoding frame for MJPEG stream: {e}")
            # If there's an error, break the loop to stop the stream for this client
            break
        await asyncio.sleep(1.0 / 15) # Stream at approx 15 FPS for MJPEG
        
    logger.info("MJPEG frame generation stopped.")

# --- Notification Logic ---
def notify_users_for_spot_vacancy(spot_id: Any):
    # spot_id here is actually the spot_label from spot_logic
    spot_label_str = str(spot_id) # Ensure it's a string
    logger.info(f"Attempting to notify users for newly vacant spot: {spot_label_str}")

    # Debounce: Check if a notification for this spot was sent recently
    current_time = time.time()
    last_notified_time = last_fcm_notification_times.get(spot_label_str, 0)
    
    if (current_time - last_notified_time) < FCM_VACANCY_DELAY_SECONDS:
        logger.info(f"Spot {spot_label_str} became vacant recently. Notification already sent or within debounce period. Skipping.")
        return {"message": f"Notification for spot {spot_label_str} debounced."}

    if not FIREBASE_CRED_PATH:
        logger.warning("Firebase not initialized. Skipping FCM notification.")
        return {"message": "Firebase not initialized."}

    try:
        with Session(db_engine) as session:
            statement = select(DeviceToken)
            tokens = session.exec(statement).all()
            fcm_tokens = [token_record.token for token_record in tokens if token_record.token]

            if not fcm_tokens:
                logger.info("No FCM tokens found in database to send notification.")
                return {"message": "No FCM tokens."}

            # For simplicity, sending a generic message.
            # Could be customized to include spot_label_str if the app handles it.
            message_title = "Parking Spot Available!"
            message_body = f"Spot {spot_label_str} is now free."
            
            message = messaging.MulticastMessage(
                notification=messaging.Notification(
                    title=message_title,
                    body=message_body,
                ),
                # android=messaging.AndroidConfig(
                # priority='high',
                # notification=messaging.AndroidNotification(
                # sound='default'
                # )
                # ),
                # apns=messaging.APNSConfig(
                # payload=messaging.APNSPayload(
                # aps=messaging.Aps(
                # sound='default'
                # )
                # )
                # ),
                tokens=fcm_tokens,
            )
            response = messaging.send_multicast(message)
            logger.info(f'{response.success_count} FCM messages were sent successfully for spot {spot_label_str}')
            if response.failure_count > 0:
                responses = response.responses
                failed_tokens = []
                for idx, resp in enumerate(responses):
                    if not resp.success:
                        failed_tokens.append(fcm_tokens[idx])
                logger.warning(f'List of tokens that caused failures: {failed_tokens}')
            
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
    logger.info("Application startup...")
    init_db() # Ensure database and tables are created
    spot_logic.refresh_spots() # Load spots from DB
    previous_spot_states_global = {str(spot_label): False for spot_label in spot_logic.SPOTS.keys()}
    
    # Start video processing only if not already active (e.g., from a previous run if using --reload)
    if not video_processing_active:
        try:
            video_capture_global = make_capture() # Initialize video capture
            if video_capture_global and video_capture_global.isOpened():
                asyncio.create_task(video_processor())
                logger.info("Video processing task created on startup.")
            else:
                logger.error("Failed to open video source on startup. Video processing will not start.")
                # Broadcast error state
                await manager.broadcast({"type": "video_error", "data": {"error": "Video source failed on startup"}})

        except RuntimeError as e:
            logger.error(f"RuntimeError during startup video capture initialization: {e}")
            # Broadcast error state
            await manager.broadcast({"type": "video_error", "data": {"error": "Video source failed on startup", "detail": str(e)}})
        except Exception as e:
            logger.error(f"Unexpected error during startup: {e}")
            logger.error(traceback.format_exc())
            await manager.broadcast({"type": "video_error", "data": {"error": "Unexpected startup error", "detail": str(e)}})
    else:
        logger.info("Video processing already active.")


@app.on_event("shutdown")
async def shutdown_event():
    global video_processing_active, video_capture_global
    logger.info("Application shutdown...")
    video_processing_active = False # Signal video_processor to stop
    if video_capture_global:
        video_capture_global.release()
    # Give the processor a moment to stop
    await asyncio.sleep(0.5) 
    logger.info("Video processing stopped.")

# --- API Endpoints ---
@app.get("/api/spots")
async def get_spots_config():
    # Refresh spots from DB each time this is called to ensure latest config
    spot_logic.refresh_spots() 
    return spot_logic.SPOTS

@app.post("/api/spots")
async def save_spots_config(spots: Dict[str, List[int]], db: Session = Depends(lambda: SessionLocal())):
    global previous_spot_states_global
    logger.info(f"Received spot configuration to save: {spots}")
    try:
        # Clear existing configurations for the default camera
        # In a multi-camera setup, you'd pass a camera_id
        default_camera_id = "default_camera"
        
        # Delete existing spots for this camera_id
        statement_del = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == default_camera_id)
        results_del = db.exec(statement_del).all()
        for old_spot in results_del:
            db.delete(old_spot)
        
        # Add new spots
        new_spot_configs = []
        for label, coords in spots.items():
            if len(coords) == 4:
                spot_config = ParkingSpotConfig(
                    spot_label=str(label), 
                    x_coord=coords[0], 
                    y_coord=coords[1], 
                    width=coords[2], 
                    height=coords[3],
                    camera_id=default_camera_id # Assuming a single camera for now
                )
                new_spot_configs.append(spot_config)
                db.add(spot_config)
            else:
                logger.warning(f"Spot {label} has invalid coordinate count: {len(coords)}. Expected 4.")

        db.commit()
        for spot_config in new_spot_configs: # Refresh IDs for the newly added spots
            db.refresh(spot_config)

        logger.info("Spot configuration saved successfully to database.")
        
        # Refresh in-memory SPOTS from DB
        spot_logic.refresh_spots()
        
        # Re-initialize previous_spot_states_global with new/updated spots
        # Set all to False initially after a config change
        previous_spot_states_global = {str(spot_label): False for spot_label in spot_logic.SPOTS.keys()}
        logger.info(f"Global spot states re-initialized: {previous_spot_states_global}")

        # Broadcast new spot configuration to connected clients
        await manager.broadcast({"type": "spots_updated", "data": spot_logic.SPOTS})
        
        return {"message": "Spot configuration saved successfully", "spots": spot_logic.SPOTS}

    except Exception as e:
        db.rollback()
        logger.error(f"Error saving spot configuration: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        db.close()

@app.get("/webcam_feed")
async def webcam_feed(request: Request):
    global video_capture_global, video_processing_active
    if not video_processing_active and (video_capture_global is None or not video_capture_global.isOpened()):
        logger.info("/webcam_feed: Video processing not active and capture not open. Attempting to start.")
        try:
            # Ensure startup logic for video capture is triggered if it failed or wasn't started
            if not video_processing_active: # Double check, as startup_event might be running
                video_capture_global = make_capture()
                if video_capture_global and video_capture_global.isOpened() and not video_processing_active:
                    asyncio.create_task(video_processor())
                    logger.info("/webcam_feed: Video processing task created.")
                    # Wait a brief moment for the first frame to be potentially processed
                    await asyncio.sleep(0.5) 
                elif not video_capture_global or not video_capture_global.isOpened():
                    logger.error("/webcam_feed: Failed to open video source for MJPEG stream.")
                    return HTTPException(status_code=500, detail="Video source failed to start for MJPEG stream.")
        except RuntimeError as e:
            logger.error(f"RuntimeError during /webcam_feed video capture initialization: {e}")
            return HTTPException(status_code=500, detail=f"Video source error: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error during /webcam_feed video capture initialization: {e}")
            return HTTPException(status_code=500, detail=f"Unexpected video error: {str(e)}")


    if not video_processing_active or not video_capture_global or not video_capture_global.isOpened():
         logger.error("/webcam_feed: Video source not available for streaming.")
         # Provide a more informative error or a placeholder image if preferred
         return HTTPException(status_code=503, detail="Video stream is not currently available. Please check server logs.")

    return StreamingResponse(generate_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/spots")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current state upon connection
        current_statuses = previous_spot_states_global.copy()
        initial_data = {
            "type": "all_spot_statuses",
            "data": current_statuses,
            "timestamp": time.time()
        }
        await websocket.send_text(json.dumps(initial_data))
        
        while True:
            # Keep connection alive, updates are broadcast by video_processor
            await websocket.receive_text() # Keep it open, or handle client messages
    except Exception as e: # Handles client disconnecting, etc.
        logger.info(f"WebSocket error or client disconnected: {e}")
    finally:
        manager.disconnect(websocket)

@app.post("/api/register_fcm_token")
async def register_fcm_token(payload: Dict[str, str], db: Session = Depends(lambda: SessionLocal())):
    token = payload.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="FCM token not provided")

    try:
        # Check if token already exists
        statement = select(DeviceToken).where(DeviceToken.token == token)
        existing_token = db.exec(statement).first()
        if existing_token:
            logger.info(f"FCM token already registered: {token}")
            return {"message": "Token already registered."}
        
        new_token_record = DeviceToken(token=token)
        db.add(new_token_record)
        db.commit()
        db.refresh(new_token_record)
        logger.info(f"FCM token registered successfully: {token}")
        return {"message": "Token registered successfully."}
    except Exception as e:
        db.rollback()
        logger.error(f"Error registering FCM token {token}: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to register token.")
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    # This block is for running with `python main.py` directly (local dev)
    # App Runner will use a command like `uvicorn app.main:app --host 0.0.0.0 --port 8000`
    logger.info("Starting Uvicorn server for local development...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
