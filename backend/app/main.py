# backend/app/main.py
import json
import cv2
import anyio
import threading
import queue
import asyncio
import time # Ensure time is imported
import numpy as np
import os
from datetime import datetime, timedelta
from pathlib import Path
import traceback
from typing import Dict, List, Optional # Added Dict, List, Optional for type hinting

from dotenv import load_dotenv
from firebase_admin import credentials, initialize_app, App as FirebaseApp, messaging, exceptions as firebase_exceptions
from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel, Session, select, Field # Added Field from sqlmodel

# Updated imports from local modules
from .spot_logic import SPOTS, refresh_spots, get_spot_states, detect_vacancies
from .db import engine as db_engine, Base, SessionLocal, VacancyEvent, DeviceToken, ParkingSpotConfig
from .notifications import notify_all # Keep this for FCM
from inference.cv_model import detect

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env") # Load .env from backend directory

# --- Firebase Initialization --- #
firebase_app_instance: Optional[FirebaseApp] = None # Use Optional
try:
    cred_path_str = os.getenv("FIREBASE_CRED", "/app/secrets/firebase-sa.json")
    cred_path = Path(cred_path_str)

    if not cred_path.is_file():
        alt_cred_path = Path(__file__).resolve().parent.parent / "secrets" / "firebase-sa.json"
        if alt_cred_path.is_file():
            cred_path = alt_cred_path
        else:
            print(f"Warning: Firebase credential not found at {cred_path_str} or {alt_cred_path}. FCM notifications will be disabled.")
            
    if cred_path.is_file() and cred_path.exists():
        cred = credentials.Certificate(str(cred_path))
        try:
            firebase_app_instance = initialize_app(cred)
            print(f"Firebase app initialized successfully from {cred_path}.")
        except ValueError: # App already initialized
            try:
                firebase_app_instance = FirebaseApp.get_app(name='[DEFAULT]')
                print(f"Firebase app '[DEFAULT]' already initialized and retrieved from {cred_path}.")
            except ValueError: # If [DEFAULT] doesn't exist for some reason
                unique_app_name = f"firebase-app-{int(time.time())}"
                firebase_app_instance = initialize_app(cred, name=unique_app_name) 
                print(f"Firebase app initialized with unique name '{unique_app_name}' from {cred_path}.")
    else:
        if not cred_path.exists(): # Check if path itself is invalid
             print(f"Firebase credential path '{cred_path}' does not exist. FCM notifications will be disabled.")
        else: # Path exists but is not a file (e.g. directory)
             print(f"Firebase credential path '{cred_path}' is not a file. FCM notifications will be disabled.")


except Exception as e:
    print(f"An error occurred during Firebase initialization: {e}")
    print(traceback.format_exc())
    print("FCM notifications may be unavailable.")


# --- Database Initialization --- #
# Tables are created by Alembic migrations.
# This function can be removed or kept as a placeholder.
def create_db_and_tables():
    print("Database tables should be created by Alembic migrations.")
    pass

# --- FastAPI Application Setup --- #
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Static Files & Root Endpoint --- #
project_root_static_dir = Path(__file__).resolve().parent.parent.parent / "static"
if project_root_static_dir.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(project_root_static_dir), html=False),
        name="static",
    )
    index_html_path = project_root_static_dir / "index.html"
    if index_html_path.is_file():
        @app.get("/", include_in_schema=False)
        async def serve_index():
            return FileResponse(str(index_html_path))
    else:
        print(f"Warning: index.html not found at {index_html_path}. Root path will not serve the frontend.")
        @app.get("/", include_in_schema=False)
        async def serve_root_message():
            return {"message": "Welcome to Smart Parking API. Frontend index.html not found."}
else:
    print(f"Warning: Static directory not found at {project_root_static_dir}. Static files will not be served.")
    @app.get("/", include_in_schema=False)
    async def serve_root_message():
        return {"message": "Welcome to Smart Parking API. Static directory for frontend not found."}


# --- WebSocket Connection Manager --- #
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
        print(f"WebSocket connected: {ws.client.host}:{ws.client.port}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
            print(f"WebSocket disconnected: {ws.client.host}:{ws.client.port}")

    async def broadcast(self, message: str):
        disconnected_websockets = []
        for ws in list(self.active_connections): 
            try:
                await ws.send_text(message)
            except Exception: 
                disconnected_websockets.append(ws)
        
        for ws_remove in disconnected_websockets: 
            if ws_remove in self.active_connections: 
                 self.disconnect(ws_remove)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await asyncio.sleep(60) 
            try:
                await ws.send_text(json.dumps({"type": "ping"}))
            except Exception: 
                print(f"WebSocket {ws.client.host}:{ws.client.port} failed to send ping, assuming disconnected.")
                break 
    except WebSocketDisconnect:
        print(f"WebSocket {ws.client.host}:{ws.client.port} initiated disconnect.")
    except Exception as e_ws:
        print(f"Unexpected error in websocket_endpoint for {ws.client.host}:{ws.client.port}: {e_ws}")
        print(traceback.format_exc())
    finally:
        manager.disconnect(ws)

# --- Globals for Video Processing and Event Queue --- #
event_queue: Optional[queue.Queue] = None
current_spot_statuses: Dict[str, bool] = {} 
latest_processed_frame: Optional[np.ndarray] = None
frame_lock = threading.Lock()

# --- Event Broadcasting via Queue --- #
def _enqueue_message(event_data: dict):
    if event_queue:
        try:
            event_queue.put_nowait(json.dumps(event_data))
        except queue.Full:
            print(f"Warning: Event queue full. Dropping event: {event_data.get('type')}")
        except Exception as e:
            print(f"Error enqueuing message: {e}")
    else:
        print("Warning: Event queue not initialized. Cannot enqueue message.")

def broadcast_spot_update(event_data: dict):
    event_data["type"] = "spot_status_update"
    if "timestamp" in event_data:
        ts = event_data["timestamp"]
        if isinstance(ts, datetime):
            event_data["timestamp"] = ts.isoformat()
        # Ensure Zulu time for string timestamps
        if isinstance(event_data["timestamp"], str) and not event_data["timestamp"].endswith("Z"):
            event_data["timestamp"] += "Z"
    _enqueue_message(event_data)

def broadcast_config_update():
    _enqueue_message({"type": "config_update"})

# --- Event Processor (Queue to WebSocket) --- #
async def event_processor():
    print("Event processor task started.")
    while True:
        try:
            if event_queue and not event_queue.empty():
                message_str = await anyio.to_thread.run_sync(event_queue.get)
                await manager.broadcast(message_str)
                event_queue.task_done() 
            else:
                await asyncio.sleep(0.1) 
        except Exception as e:
            print(f"Error in event processor task: {e}")
            print(traceback.format_exc())
            await asyncio.sleep(1) 

# --- API Endpoints --- #
def get_db_session():
    with Session(db_engine) as session:
        yield session

@app.get("/api/spots")
def get_spots_api(session: Session = Depends(get_db_session)):
    print("GET /api/spots endpoint accessed.")
    try:
        if not SPOTS:
            print("/api/spots: Global SPOTS is empty, attempting to refresh from DB...")
            refresh_spots()

        spots_data_to_return = []
        for spot_label, (x, y, w, h) in SPOTS.items():
            is_occupied = current_spot_statuses.get(str(spot_label), False) 
            spots_data_to_return.append({
                "id": str(spot_label), 
                "x": x, "y": y, "w": w, "h": h,
                "is_available": not is_occupied
            })
        return {"spots": spots_data_to_return}
    except Exception as e:
        print(f"Unexpected error in get_spots_api: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")

class SpotConfigIn(BaseModel):
    id: str 
    x: int
    y: int
    w: int
    h: int

class SpotsSaveRequest(BaseModel):
    spots: List[SpotConfigIn]

@app.post("/api/spots")
async def save_spots_api(request_data: SpotsSaveRequest, session: Session = Depends(get_db_session)):
    print(f"POST /api/spots endpoint accessed with {len(request_data.spots)} spots.")
    camera_id_to_save = "default_camera"
    try:
        stmt_delete = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == camera_id_to_save)
        spots_to_delete = session.exec(stmt_delete).all()
        for spot_db in spots_to_delete:
            session.delete(spot_db)
        
        new_spot_configs = []
        for spot_in in request_data.spots:
            new_spot_config = ParkingSpotConfig(
                spot_label=str(spot_in.id), 
                camera_id=camera_id_to_save,
                x_coord=spot_in.x, y_coord=spot_in.y,
                width=spot_in.w, height=spot_in.h,
            )
            session.add(new_spot_config)
            new_spot_configs.append(new_spot_config)
        
        session.commit()
        print(f"Successfully saved {len(new_spot_configs)} spots to database for camera '{camera_id_to_save}'.")
        refresh_spots()
        broadcast_config_update()
        return {"ok": True, "message": "Spot configurations saved successfully."}
    except Exception as e:
        session.rollback()
        print(f"Error in save_spots_api: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Could not save spot configurations: {str(e)}")

# --- Video Processing Logic --- #
def make_capture():
    src = os.getenv("VIDEO_SOURCE", "0") 
    print(f"Attempting to open video source: {src}")
    try:
        idx = int(src)
        cap = cv2.VideoCapture(idx)
    except ValueError:
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG) 
    
    if not cap.isOpened():
        error_msg = f"Could not open video source: {src}"
        print(error_msg)
        raise RuntimeError(error_msg)
    print(f"Successfully opened video source: {src}")
    return cap

# Global dictionary to track when spots became empty (for FCM notification delay)
# Key: spot_label (str), Value: datetime object when spot became empty
empty_start_times: Dict[str, Optional[datetime]] = {}
# Global dictionary to track if notification has been sent for the current vacancy
# Key: spot_label (str), Value: bool
fcm_notified_for_current_vacancy: Dict[str, bool] = {}

def video_processor():
    cap = None
    global current_spot_statuses, latest_processed_frame, empty_start_times, fcm_notified_for_current_vacancy

    try:
        print("video_processor thread: Initializing video capture...")
        cap = make_capture()
    except Exception as e_cap:
        print(f"video_processor thread: CRITICAL - Failed to initialize video capture: {e_cap}")
        with frame_lock:
            error_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(error_frame, f"Video Error: {str(e_cap)[:100]}", (30, 30), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            latest_processed_frame = error_frame
        return

    prev_states: Dict[str, bool] = {}
    for spot_label_init in SPOTS.keys():
        prev_states[str(spot_label_init)] = current_spot_statuses.get(str(spot_label_init), False)
        empty_start_times[str(spot_label_init)] = None # Initialize
        fcm_notified_for_current_vacancy[str(spot_label_init)] = False # Initialize

    VACANCY_DELAY_FOR_FCM = timedelta(seconds=int(os.getenv("FCM_VACANCY_DELAY_SECONDS", "5"))) # Delay before sending FCM
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

    print(f"video_processor thread: Starting main processing loop. FCM Vacancy delay: {VACANCY_DELAY_FOR_FCM.total_seconds()}s")

    while True:
        try:
            # Synchronize SPOTS if configuration changed (e.g. editor saved new spots)
            # This check can be optimized if config changes are infrequent.
            # For now, this simply ensures newly added/removed spots are handled.
            current_spot_labels_in_mem = set(SPOTS.keys())
            for label in list(prev_states.keys()): # Use list for safe iteration if modifying
                if label not in current_spot_labels_in_mem:
                    prev_states.pop(label, None)
                    current_spot_statuses.pop(label, None)
                    empty_start_times.pop(label, None)
                    fcm_notified_for_current_vacancy.pop(label, None)
            for label in current_spot_labels_in_mem:
                if label not in prev_states:
                    prev_states[label] = False # Assume new spots are initially free in prev_states
                    current_spot_statuses[label] = False
                    empty_start_times[label] = None
                    fcm_notified_for_current_vacancy[label] = False
            
            ret, frame = cap.read()
            if not ret:
                print("video_processor thread: Failed to read frame. Attempting to reopen...")
                if cap: cap.release()
                time.sleep(5) 
                try:
                    cap = make_capture()
                    print("video_processor thread: Video source reopened.")
                    prev_states = {str(label): current_spot_statuses.get(str(label), False) for label in SPOTS.keys()}
                except Exception as e_reopen:
                    print(f"video_processor thread: Failed to reopen video source: {e_reopen}. Exiting thread.")
                    with frame_lock:
                        error_frame = np.zeros((480, 640, 3), dtype=np.uint8)
                        cv2.putText(error_frame, "Video Source Lost", (30, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255),2)
                        latest_processed_frame = error_frame
                    break 
                continue

            detection_results = detect(frame) 
            curr_detected_states = get_spot_states(detection_results) 

            now = datetime.utcnow()

            # Process state changes and FCM notifications
            for spot_label_str in SPOTS.keys(): # Iterate over configured spots
                spot_label_str = str(spot_label_str) # Ensure string
                is_currently_occupied = curr_detected_states.get(spot_label_str, False) # Current detected state
                was_previously_occupied = prev_states.get(spot_label_str, False)       # Previous iteration's state

                # Update global current_spot_statuses (used by /api/spots)
                current_spot_statuses[spot_label_str] = is_currently_occupied

                # State change detection
                if was_previously_occupied and not is_currently_occupied: # Became Vacant
                    print(f"[video_processor] Spot {spot_label_str} became VACANT.")
                    broadcast_spot_update({"spot_id": spot_label_str, "timestamp": now, "status": "free"})
                    empty_start_times[spot_label_str] = now # Record time it became empty
                    fcm_notified_for_current_vacancy[spot_label_str] = False # Reset FCM notification status

                elif not was_previously_occupied and is_currently_occupied: # Became Occupied
                    print(f"[video_processor] Spot {spot_label_str} became OCCUPIED.")
                    broadcast_spot_update({"spot_id": spot_label_str, "timestamp": now, "status": "occupied"})
                    empty_start_times[spot_label_str] = None # No longer empty
                    # fcm_notified_for_current_vacancy already False or doesn't matter

                # FCM Notification Logic (if spot is free and delay has passed)
                if not is_currently_occupied and \
                   empty_start_times.get(spot_label_str) is not None and \
                   not fcm_notified_for_current_vacancy.get(spot_label_str, False):
                    
                    if (now - empty_start_times[spot_label_str]) >= VACANCY_DELAY_FOR_FCM:
                        print(f"[video_processor] Spot {spot_label_str} confirmed vacant for {VACANCY_DELAY_FOR_FCM.total_seconds()}s. Sending FCM notification.")
                        try:
                            spot_id_int = int(spot_label_str) # notify_all expects int
                            # Log VacancyEvent to DB
                            with Session(db_engine) as db_session:
                                evt = VacancyEvent(timestamp=now, spot_id=spot_id_int, camera_id="default_camera")
                                db_session.add(evt)
                                db_session.commit()
                                print(f"[video_processor] Logged VacancyEvent for spot {spot_label_str} (ID: {spot_id_int}).")
                            
                            # Send FCM
                            if firebase_app_instance: # Check if Firebase was initialized
                                notify_response = notify_all(spot_id=spot_id_int)
                                print(f"[video_processor] FCM send attempt for spot {spot_label_str}: Successes={notify_response.get('success_count',0)}, Failures={notify_response.get('failure_count',0)}")
                            else:
                                print("[video_processor] Firebase not initialized, skipping FCM notification.")
                            
                            fcm_notified_for_current_vacancy[spot_label_str] = True
                        except ValueError:
                            print(f"[video_processor] ERROR: Spot label '{spot_label_str}' is not an integer. Cannot send FCM or log VacancyEvent with int ID.")
                        except Exception as e_fcm:
                            print(f"[video_processor] ERROR sending FCM for spot {spot_label_str}: {e_fcm}")
                            print(traceback.format_exc())
            
            prev_states = curr_detected_states.copy()

            # --- Drawing on the frame ---
            frame_with_overlays = frame.copy()
            if detection_results: 
                for res in detection_results:
                    if hasattr(res, 'boxes') and hasattr(res.boxes, 'xyxy'):
                        boxes = res.boxes.xyxy.tolist() if hasattr(res.boxes.xyxy, "tolist") else res.boxes.xyxy
                        classes = res.boxes.cls.tolist() if hasattr(res.boxes.cls, "tolist") else res.boxes.cls
                        names = res.names
                        for i, (x1, y1, x2, y2) in enumerate(boxes):
                            if i < len(classes): 
                                cls_idx = int(classes[i])
                                if cls_idx < len(names) and names[cls_idx] in vehicle_classes: 
                                     cv2.rectangle(frame_with_overlays, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 255), 1)

            for spot_label, (sx, sy, sw, sh) in SPOTS.items():
                x1_spot, y1_spot = int(sx), int(sy)
                x2_spot, y2_spot = int(sx + sw), int(sy + sh)
                is_spot_occupied = current_spot_statuses.get(str(spot_label), False)
                spot_color = (0, 0, 255) if is_spot_occupied else (0, 255, 0) 
                cv2.rectangle(frame_with_overlays, (x1_spot, y1_spot), (x2_spot, y2_spot), spot_color, 2)
                cv2.putText(frame_with_overlays, str(spot_label), (x1_spot, y1_spot - 10), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, spot_color, 2)
            
            with frame_lock:
                latest_processed_frame = frame_with_overlays

            time.sleep(1/float(os.getenv("VIDEO_PROCESSING_FPS", "10"))) 

        except Exception as e_loop:
            print(f"video_processor thread: Error during frame processing loop: {e_loop}")
            print(traceback.format_exc())
            time.sleep(1) 

    if cap: cap.release()
    print("video_processor thread: Finished and released video capture.")

# --- Application Startup --- #
@app.on_event("startup")
async def startup_event():
    print("Application startup sequence initiated...")
    
    if not SPOTS: 
        print("Startup: Global SPOTS is empty, attempting refresh from DB...")
        try:
            refresh_spots()
        except Exception as e_startup_refresh:
            print(f"Startup: Error refreshing spots: {e_startup_refresh}")

    global event_queue
    event_queue = queue.Queue(maxsize=200) 
    print("Event queue initialized.")

    print("Scheduling video processing task...")
    asyncio.create_task(anyio.to_thread.run_sync(video_processor))
    
    print("Scheduling WebSocket event processor task...")
    asyncio.create_task(event_processor())
    
    print("Application startup complete.")

# --- Static Endpoints & Token Registration --- #
@app.get("/webcam_feed")
def webcam_feed():
    def generate_frames_for_stream():
        while True:
            frame_to_send = None 
            with frame_lock: 
                if latest_processed_frame is not None:
                    frame_to_send = latest_processed_frame.copy() 
            
            if frame_to_send is not None:
                try:
                    flag, encoded_jpeg = cv2.imencode('.jpg', frame_to_send)
                    if flag:
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + encoded_jpeg.tobytes() + b'\r\n')
                except Exception as e_mjpeg:
                    print(f"Error encoding frame for MJPEG stream: {e_mjpeg}")
            else:
                placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(placeholder, "Waiting for video...", (30, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
                _, encoded_jpeg = cv2.imencode('.jpg', placeholder)
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + encoded_jpeg.tobytes() + b'\r\n')
            time.sleep(1/30) 
            
    return StreamingResponse(generate_frames_for_stream(), media_type="multipart/x-mixed-replace; boundary=frame")

class TokenIn(BaseModel):
    token: str
    platform: str = Field(default="android")

@app.post("/api/register_token")
async def register_token_api(data: TokenIn, session: Session = Depends(get_db_session)):
    print(f"POST /api/register_token invoked with token: {data.token[:10]}...") 
    
    existing_token_stmt = select(DeviceToken).where(DeviceToken.token == data.token)
    existing_token = session.exec(existing_token_stmt).first()
    
    if not existing_token:
        db_token = DeviceToken(token=data.token, platform=data.platform)
        session.add(db_token)
        try:
            session.commit()
            session.refresh(db_token) 
            print(f"Registered new device token: ID {db_token.id}, Platform: {db_token.platform}")
            return {"status":"ok", "message": "Token registered successfully."}
        except Exception as e_commit: 
            session.rollback()
            print(f"Error committing new token, possibly due to race condition: {e_commit}")
            existing_token_after_error = session.exec(select(DeviceToken).where(DeviceToken.token == data.token)).first()
            if existing_token_after_error:
                 print(f"Device token already exists (found after commit error): {data.token[:10]}...")
                 return {"status":"exists", "message": "Token already registered."}
            raise HTTPException(status_code=500, detail=f"Could not register token: {str(e_commit)}")
    else:
        print(f"Device token already exists: {data.token[:10]}...")
        return {"status":"exists", "message": "Token already registered."}

@app.get("/test_event")
def test_event_endpoint():
    print("GET /test_event endpoint accessed.")
    test_spot_id = "1" 
    broadcast_spot_update({
        "spot_id": test_spot_id, 
        "timestamp": datetime.utcnow(), # Pass datetime object
        "status": "free" 
    })
    return {"message": f"Test event sent for spot {test_spot_id}."}
