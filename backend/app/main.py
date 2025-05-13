import json
import cv2
import anyio
import threading 
import queue 
import asyncio 
import time 
import numpy as np 
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import traceback
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from inference.cv_model import detect
from .spot_logic import SPOTS, refresh_spots
from .db import engine, Base, SessionLocal, VacancyEvent, DeviceToken

import os
import firebase_admin
from firebase_admin import credentials, messaging, initialize_app
from pydantic import BaseModel
from .db import engine, Base, DeviceToken

from sqlmodel import SQLModel, Session, select
from .notifications import notify_all

cred_path = os.environ.get("FIREBASE_CRED", "")
if not cred_path or not os.path.isfile(cred_path):
    raise RuntimeError(f"Firebase credential not found at {cred_path!r}. FCM notifications will not work.")
cred = credentials.Certificate(cred_path)
try:
    initialize_app(cred)
    print("Firebase app initialized successfully.")
except ValueError:
    print("Firebase app already initialized.")


def create_db_and_tables():
    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)  # For SQLAlchemy Base models (Occupancy, VacancyEvent)
    SQLModel.metadata.create_all(bind=engine) # For SQLModel models (DeviceToken)
    print("Database tables creation attempt finished.")
    # You can add the inspect logic here too if you want to see tables on every startup
    # from sqlalchemy import inspect
    # insp = inspect(engine)
    # print("Tables now in database:", insp.get_table_names())

Base.metadata.create_all(bind=engine)

APP_DIR     = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR    = BACKEND_DIR.parent
SPOTS_PATH  = BACKEND_DIR / "spots.json"

# FastAPI setup
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/static",
    StaticFiles(directory=str(ROOT_DIR / "static"), html=False),
    name="static",
)

@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(str(ROOT_DIR / "static" / "index.html"))

# WebSocket manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        print("ConnectionManager initialized.")
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
        print(f"WebSocket connected: {ws}. Total connections: {len(self.active_connections)}") 
    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
            print(f"WebSocket disconnected: {ws}. Total connections: {len(self.active_connections)}")
    async def broadcast(self, message: str):
        disconnected_websockets = [] 
        print(f"Broadcasting message to {len(self.active_connections)} connections.") 
        for ws in list(self.active_connections): 
            try:
                print(f"Attempting to send message to {ws}: {message[:50]}...") 
                await ws.send_text(message)
                print(f"Message successfully sent to {ws}") 
            except Exception as e:
                print(f"Error sending message to {ws}: {e}")
                disconnected_websockets.append(ws)

        # Disconnect the failed websockets outside the loop
        for ws in disconnected_websockets:
             self.disconnect(ws)


manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    print("WebSocket endpoint accessed.") 
    await manager.connect(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=60) 
            except asyncio.TimeoutError:
                print(f"WebSocket {ws} received no message for 60 seconds, keeping connection open.")
                pass 
    except WebSocketDisconnect:
        print("WebSocketDisconnect exception caught.")
        manager.disconnect(ws)
    except Exception as e:
        print(f"Unexpected error in websocket_endpoint: {e}") 
        manager.disconnect(ws)

# Global standard Python queue for communication between thread and async loop
event_queue: queue.Queue = None

# Global dictionary to hold the current detection status of each spot
current_spot_statuses = {}

# Global variable to hold the latest processed frame with overlays
latest_processed_frame: np.ndarray = None

# Lock to protect access to latest_processed_frame
frame_lock = threading.Lock()


def broadcast_vacancy(event: dict):
    print(f"broadcast_vacancy called with event: {event}") # 
    event["type"] = "spot_status_update"
    if "timestamp" in event and not event["timestamp"].endswith("Z"):
        event["timestamp"] += "Z"
    if event_queue:
        try:
            event_queue.put_nowait(json.dumps(event))
            print("Put event onto queue.")
        except queue.Full:
             print("Event queue is full, dropping event.")
        except Exception as e:
             print(f"Error putting event onto queue: {e}")
    else:
        print("Event queue not initialized.")

def broadcast_config_update(): 
    print("broadcast_config_update called.")
    message = json.dumps({
        "type": "config_update" 
    })
    if event_queue:
        try:
            event_queue.put_nowait(message)
            print("Put config update event onto queue.")
        except queue.Full:
            print("Event queue is full, dropping config update event.")
        except Exception as e:
            print(f"Error putting config update event onto queue: {e}")
    else:
        print("Event queue not initialized for config update.")


# Async task to process events from the queue and broadcast via WebSocket
async def event_processor():
    print("Event processor task started.") 
    while True:
        print("Event processor: Checking queue...")
        try:
            message_str = await anyio.to_thread.run_sync(event_queue.get)
            print(f"Processing message from queue in event_processor: {message_str}")
            print("Event processor: About to broadcast message...")

            await manager.broadcast(message_str)
            await anyio.sleep(0.5) 

        except Exception as e:
            print(f"Error in event processor task: {e}")
            await anyio.sleep(1)

# Spots API
@app.get("/api/spots")
def get_spots():
    print("GET /api/spots endpoint accessed.")
    try:
        raw = json.loads(SPOTS_PATH.read_text())
        # When fetching spots, include the current detection status from current_spot_statuses
        spots_with_status = []
        for s in raw.get("spots", []):
            spot_id = str(s["id"]) # Ensure spot_id is string for consistent keys
            # Get current status from the global current_spot_statuses dictionary.
            # Default to False (available/free) if spot_id not found (e.g., very new spot before first detection)
            is_occupied = current_spot_statuses.get(spot_id, False)
            spots_with_status.append({
                "id": spot_id,
                "x": s["bbox"][0],
                "y": s["bbox"][1],
                "w": s["bbox"][2]-s["bbox"][0],
                "h": s["bbox"][3]-s["bbox"][1],
                "is_available": not is_occupied
            })

        return {"spots": spots_with_status}

    except FileNotFoundError:
        print(f"spots.json not found at {SPOTS_PATH}")
        return {"spots": []}
    except json.JSONDecodeError:
        print(f"Error decoding spots.json at {SPOTS_PATH}") 
        raise HTTPException(status_code=500, detail="Error reading spots configuration")
    except Exception as e:
        print(f"Unexpected error in get_spots: {e}") 
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@app.post("/api/spots")
async def save_spots(config: dict = Body(...)): # Made async to await broadcast_config_update
    print("POST /api/spots endpoint accessed.")
    try:
        disk = {"spots": [{"id": s["id"],
                           "bbox": [s["x"], s["y"], s["x"]+s["w"], s["y"]+s["h"]]
                          } for s in config.get("spots", [])]}
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        print("spots.json saved.") 
        refresh_spots()
        print("spots refreshed.") 

        broadcast_config_update()

        return {"ok": True}
    except Exception as e:
        print(f"Error in save_spots: {e}") 
        raise HTTPException(500, f"Could not write spots.json: {e}")

# Video capture helper
def make_capture():
    src = os.getenv("VIDEO_SOURCE", "0")
    print(f"Attempting to open video source: {src!r}") 
    try:
        idx = int(src)
    except ValueError:
         # URL → use FFmpeg backend
        # Explicitly try CAP_FFMPEG backend for RTSP streams
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(idx)

    if not cap.isOpened():
        print(f"Could not open video source {src!r}") 
        raise RuntimeError(f"Could not open video source {src!r}")
    print(f"Video source {src!r} opened successfully.") 
    return cap


# Video processing and frame generation for streaming and updates
def video_processor():
    """Reads video frames, performs detection, updates status, and stores latest frame."""
    print("video_processor started.")
    cap = None
    global current_spot_statuses
    global latest_processed_frame

    try:
        try:
            print("video_processor: Attempting to open video source now.")
            cap = make_capture()
        except Exception as e:
            print(f"Failed to initialize video capture: {e}")
            return # Exit the thread if capture fails

        prev_states = {}  # Stores {spot_id_int: is_occupied_bool}
        empty_start = {}  # Stores {spot_id_str: datetime_obj_when_spot_became_empty}
        notified = {}     # Stores {spot_id_str: bool_if_notification_sent_for_current_vacancy}
        VACANCY_DELAY = timedelta(seconds=2) # Or your preferred delay
        vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

        # --- Perform initial detection pass ---
        print("Performing initial detection pass...")
        ret, frame = cap.read()
        if not ret:
            print("Failed to read initial frame. Cannot perform initial detection.")
            initial_vehicle_boxes = []
        else:
            initial_results = detect(frame)
            initial_vehicle_boxes = []
            for res in initial_results:
                boxes = res.boxes.xyxy.tolist()
                classes = res.boxes.cls.tolist()
                for i, cls_idx in enumerate(classes):
                    if res.names[cls_idx] in vehicle_classes:
                        initial_vehicle_boxes.append(boxes[i])

        # Initialize current_spot_statuses, prev_states, and notified based on SPOTS and initial detection
        # Ensure SPOTS uses integer IDs as keys if sid_int below is to be used directly
        for sid_int, (sx, sy, sw, sh) in SPOTS.items(): # Assuming SPOTS keys are integers
            sid_str = str(sid_int)
            is_initially_occupied = any(
                sx <= (bx1 + bx2) / 2 <= sx + sw and sy <= (by1 + by2) / 2 <= sy + sh
                for bx1, by1, bx2, by2 in initial_vehicle_boxes
            )
            current_spot_statuses[sid_str] = is_initially_occupied
            prev_states[sid_int] = is_initially_occupied
            notified[sid_str] = False # Initially, no notifications sent for the current session
            if not is_initially_occupied:
                empty_start[sid_str] = datetime.utcnow() # If starts empty, timer starts now
            else:
                empty_start[sid_str] = None


        print("Initial detection pass complete. Initial spot statuses:", current_spot_statuses)
        # (Initial frame drawing logic as you had it)
        if ret:
            frame_with_overlays = frame.copy()
            for sid_int, (sx, sy, sw, sh) in SPOTS.items():
                sid_str = str(sid_int)
                x1, y1 = int(sx), int(sy)
                x2, y2 = int(sx + sw), int(sy + sh)
                color = (0,0,255) if current_spot_statuses.get(sid_str, False) else (0,255,0)
                cv2.rectangle(frame_with_overlays,(x1,y1),(x2,y2),color,2)
                cv2.putText(frame_with_overlays,f"Spot {sid_str}",(x1,y1-10),cv2.FONT_HERSHEY_SIMPLEX,0.7,color,2)
            for bx1,by1,bx2,by2 in initial_vehicle_boxes:
                cv2.rectangle(frame_with_overlays,(int(bx1),int(by1)),(int(bx2),int(by2)),(0,255,255),2)
            with frame_lock:
                latest_processed_frame = frame_with_overlays
        else:
            with frame_lock:
                latest_processed_frame = None
        # --- End of Initial Pass Adjustments ---

        while True:
            # Dynamic spot list update (remove deleted spots from internal state)
            # Ensure internal state dicts only contain current spots from SPOTS
            current_spot_ids_int = set(SPOTS.keys())
            for sid_int_key in list(prev_states.keys()):
                if sid_int_key not in current_spot_ids_int:
                    print(f"Removing state for spot {sid_int_key} as it's no longer in SPOTS.")
                    prev_states.pop(sid_int_key, None)
                    empty_start.pop(str(sid_int_key), None)
                    notified.pop(str(sid_int_key), None)
                    current_spot_statuses.pop(str(sid_int_key), None)
            
            # Add new spots to internal state if SPOTS updated
            for sid_int_key in current_spot_ids_int:
                if sid_int_key not in prev_states: # New spot found
                    print(f"Initializing state for new spot {sid_int_key}.")
                    # Initialize based on a quick current detection, or default to free
                    # For simplicity, let's default new spots to appear free until first proper detection pass
                    # Or better, do a quick check if possible, otherwise default
                    prev_states[sid_int_key] = False # Assume free initially
                    current_spot_statuses[str(sid_int_key)] = False
                    empty_start[str(sid_int_key)] = datetime.utcnow()
                    notified[str(sid_int_key)] = False


            ret, frame = cap.read()
            if not ret:
                # ... (your existing frame read error handling and re-open logic) ...
                print("Failed to read frame from video source. Attempting to re-open...")
                if cap: cap.release()
                time.sleep(2)
                try:
                    cap = make_capture()
                    if not cap.isOpened(): print("Failed to re-open. Stopping."); break
                except Exception as e: print(f"Error re-opening: {e}. Stopping."); break
                continue

            try:
                results = detect(frame)
                vehicle_boxes = []
                for res in results:
                    boxes = res.boxes.xyxy.tolist()
                    classes = res.boxes.cls.tolist()
                    for i, cls_idx in enumerate(classes):
                        if res.names[cls_idx] in vehicle_classes:
                            vehicle_boxes.append(boxes[i])

                # This curr_states should be {int: bool} to match prev_states
                curr_states_detected_occupied = {} # Spot ID (int) -> is_occupied (bool)
                for sid_int, (sx, sy, sw, sh) in SPOTS.items(): # Assuming SPOTS keys are integers
                    is_occupied_now = any(
                        sx <= (bx1 + bx2) / 2 <= sx + sw and sy <= (by1 + by2) / 2 <= sy + sh
                        for bx1, by1, bx2, by2 in vehicle_boxes
                    )
                    curr_states_detected_occupied[sid_int] = is_occupied_now

                now = datetime.utcnow()

                for sid_int in SPOTS: # Iterate using integer keys from SPOTS
                    sid_str = str(sid_int) # For string-keyed dictionaries like current_spot_statuses, empty_start, notified

                    # Get current and previous occupation status
                    # Ensure prev_states has an entry for sid_int (should be handled by initialization)
                    was_occupied = prev_states.get(sid_int, False) # Default to False if somehow missing
                    is_occupied = curr_states_detected_occupied.get(sid_int, False)

                    # **** START: Revised Spam Prevention Logic ****
                    if was_occupied and not is_occupied:  # Transition: Occupied -> Free
                        print(f"Spot {sid_str} changed from occupied to vacant.")
                        empty_start[sid_str] = now
                        notified[sid_str] = False  # Reset notification status, eligible for new notification
                        current_spot_statuses[sid_str] = False
                        broadcast_vacancy({"spot_id": sid_str, "timestamp": now.isoformat(), "status": "free"})
                    
                    elif not was_occupied and is_occupied:  # Transition: Free -> Occupied
                        print(f"Spot {sid_str} changed from vacant to occupied.")
                        empty_start[sid_str] = None  # Clear the timer
                        # notified[sid_str] remains as it was (likely True if it was free long enough to be notified)
                        # It will be reset to False only when it becomes free again.
                        current_spot_statuses[sid_str] = True
                        broadcast_vacancy({"spot_id": sid_str, "timestamp": now.isoformat(), "status": "occupied"})

                    # Check for sending vacancy notification
                    if not is_occupied and empty_start.get(sid_str) and not notified.get(sid_str, False):
                        if (now - empty_start[sid_str]) >= VACANCY_DELAY:
                            print(f"Spot {sid_str} confirmed vacant for {VACANCY_DELAY}, sending notification.")
                            
                            with SessionLocal() as session_db:
                                evt = VacancyEvent(timestamp=now, spot_id=sid_int, camera_id="main") # Use sid_int for DB if it's integer type
                                session_db.add(evt)
                                session_db.commit()
                            
                            # WebSocket broadcast for "free" status was already done when it transitioned
                            # If you need to re-broadcast here, you can, but it might be redundant.

                            try:
                                notify_response = notify_all(spot_id=sid_int) # Use integer spot ID
                                print(f"Attempted to send FCM notification for spot {sid_str}.")
                                if notify_response:
                                    print(f"  FCM Send Summary: Successes={notify_response.get('success_count', 0)}, Failures={notify_response.get('failure_count', 0)}")
                                    if notify_response.get('failure_count', 0) > 0:
                                        for resp_details in notify_response.get('responses', []):
                                            if not resp_details.get('success'):
                                                print(f"    Failed send detail: {resp_details.get('exception')}")
                                else:
                                    print("  notify_all returned None or an unexpected response.")
                            except Exception as e:
                                print(f"Exception caught in main.py while trying to call notify_all: {e}")
                                print(traceback.format_exc())
                            
                            notified[sid_str] = True # Mark as notified for this specific vacancy event
                    # **** END: Revised Spam Prevention Logic ****

                prev_states = curr_states_detected_occupied.copy() # Update prev_states for the next iteration

                # Drawing on the frame
                frame_with_overlays = frame.copy()
                for sid_int_draw, (sx_draw, sy_draw, sw_draw, sh_draw) in SPOTS.items():
                    sid_str_draw = str(sid_int_draw)
                    x1, y1 = int(sx_draw), int(sy_draw)
                    x2, y2 = int(sx_draw + sw_draw), int(sy_draw + sh_draw)
                    color = (0, 0, 255) if current_spot_statuses.get(sid_str_draw, False) else (0, 255, 0)
                    cv2.rectangle(frame_with_overlays, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame_with_overlays, f"Spot {sid_str_draw}", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                for bx1, by1, bx2, by2 in vehicle_boxes:
                    cv2.rectangle(frame_with_overlays, (int(bx1), int(by1)), (int(bx2), int(by2)), (0, 255, 255), 2)

                with frame_lock:
                    latest_processed_frame = frame_with_overlays

            except Exception as e:
                print(f"Error during frame processing loop: {e}")
                print(traceback.format_exc()) # Print full traceback for frame processing errors

    except Exception as e:
        print(f"Outer error in video_processor: {e}")
        print(traceback.format_exc()) # Print full traceback for outer errors
    finally:
        print("video_processor finished. Releasing capture.")
        if cap:
            cap.release()



# Add this startup event to run the video processing in a background thread
@app.on_event("startup")
async def startup_event():
    print("App startup event triggered. Starting video processing background task.")
    create_db_and_tables() 
    global event_queue # Only need to declare global for the queue
    try:
        event_queue = queue.Queue(maxsize=100) 
        print("Event queue initialized.")

        thread = threading.Thread(target=video_processor, daemon=True)
        thread.start()
        print("video_processor started in a background thread.")


        asyncio.create_task(event_processor())
        print("Event processor task scheduled using asyncio.create_task.")

    except Exception as e:
        print(f"Error during startup event: {e}")
        print(f"Details of the error: {e}")


# Video stream endpoint for the webcam feed
@app.get("/webcam_feed")
def webcam_feed():
    print("GET /webcam_feed endpoint accessed.")

    def generate_streaming_frames():
        while True:
            with frame_lock: 
                frame = latest_processed_frame
            if frame is not None:
                success, jpeg = cv2.imencode('.jpg', frame)
                if success:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            time.sleep(0.03) 


    return StreamingResponse(generate_streaming_frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/test_event")
def test_event():
    print("GET /test_event endpoint accessed.")
    spot_id = 1
    # Use broadcast_vacancy to send a test message via WebSocket
    broadcast_vacancy({"spot_id":str(spot_id), "timestamp":datetime.utcnow().isoformat(), "status":"free"})
    return {"sent_test_event": True}


class TokenIn(BaseModel):
    token: str
    platform: str = "android"

@app.post("/api/register_token")
async def register_token(data: TokenIn):
    print("POST /api/register_token endpoint accessed.")
    with Session(engine) as sess:
        if not sess.exec(select(DeviceToken).where(DeviceToken.token == data.token)).first():
            sess.add(DeviceToken(token=data.token, platform=data.platform))
            sess.commit()
            print(f"Registered new device token: {data.token}")
        else:
            print(f"Device token already exists: {data.token}")
    return {"status":"ok"}

