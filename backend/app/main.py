import json
import cv2
import anyio
import threading # Import the threading module
import queue # Import the standard queue module
import asyncio # Import the asyncio module
import time # Import time for sleep in thread
import numpy as np # Import numpy for handling image data
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()   # <-- this will read .env automatically

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from inference.cv_model import detect
from .spot_logic import SPOTS, refresh_spots # SPOTS holds bounding boxes
from .db import engine, Base, SessionLocal, VacancyEvent, DeviceToken

import os
import firebase_admin
from firebase_admin import credentials, messaging, initialize_app
from pydantic import BaseModel
from sqlmodel import Session, select

from .db import DeviceToken
from .notifications import notify_all

# Initialize Firebase
cred_path = os.environ.get("FIREBASE_CRED", "")
if not cred_path or not os.path.isfile(cred_path):
    # Raising RuntimeError here as Firebase is likely required for notifications
    raise RuntimeError(f"Firebase credential not found at {cred_path!r}. FCM notifications will not work.")
cred = credentials.Certificate(cred_path)
try:
    initialize_app(cred)
    print("Firebase app initialized successfully.")
except ValueError:
    print("Firebase app already initialized.") # Handle re-initialization if necessary


# Initialize DB tables
Base.metadata.create_all(bind=engine) # Corrected typo here

# Paths
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

# Serve static files
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
        print("ConnectionManager initialized.") # Added logging
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
        print(f"WebSocket connected: {ws}. Total connections: {len(self.active_connections)}") # Added logging
    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
            print(f"WebSocket disconnected: {ws}. Total connections: {len(self.active_connections)}") # Added logging
    async def broadcast(self, message: str):
        # print(f"Broadcasting message: {message}") # Keep this log if you want to see every message broadcast attempt
        disconnected_websockets = [] # List to hold websockets that failed to send
        print(f"Broadcasting message to {len(self.active_connections)} connections.") # Log number of connections
        for ws in list(self.active_connections): # Iterate over a copy in case we modify the list
            try:
                print(f"Attempting to send message to {ws}: {message[:50]}...") # Log before sending
                await ws.send_text(message)
                print(f"Message successfully sent to {ws}") # Log after successful send
            except Exception as e:
                print(f"Error sending message to {ws}: {e}")
                # Mark for disconnection after the loop
                disconnected_websockets.append(ws)

        # Disconnect the failed websockets outside the loop
        for ws in disconnected_websockets:
             self.disconnect(ws)


manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    print("WebSocket endpoint accessed.") # Added logging
    await manager.connect(ws)
    try:
        while True:
            # This loop keeps the connection open. We don't expect to receive messages
            # from the client in this flow, but we need to await something.
            # A simple receive_text() or receive_bytes() is sufficient.
            # If the client sends a message, it will be received here.
            # If the client disconnects, WebSocketDisconnect will be raised.
            # Adding a small timeout to receive to prevent blocking indefinitely if no messages are expected
            try:
                # You might not expect to receive messages, but awaiting something keeps the connection alive.
                # If you don't receive anything, the connection might time out.
                # Consider adding a small ping/pong mechanism if needed for keep-alive.
                await asyncio.wait_for(ws.receive_text(), timeout=60) # Wait for 60 seconds
            except asyncio.TimeoutError:
                # If no message received for 60 seconds, continue the loop to keep connection alive
                print(f"WebSocket {ws} received no message for 60 seconds, keeping connection open.")
                pass # Continue the loop
    except WebSocketDisconnect:
        print("WebSocketDisconnect exception caught.") # Added logging
        manager.disconnect(ws)
    except Exception as e:
        print(f"Unexpected error in websocket_endpoint: {e}") # Added logging
        manager.disconnect(ws)

# Global standard Python queue for communication between thread and async loop
# Initialize this during startup
event_queue: queue.Queue = None

# Global dictionary to hold the current detection status of each spot
# This will be updated by the video_processor thread and read by the GET /api/spots endpoint
current_spot_statuses = {}

# Global variable to hold the latest processed frame with overlays
latest_processed_frame: np.ndarray = None
# Lock to protect access to latest_processed_frame
frame_lock = threading.Lock()


def broadcast_vacancy(event: dict):
    print(f"broadcast_vacancy called with event: {event}") # Added logging
    # Add a type field to distinguish this message
    event["type"] = "spot_status_update"
    if "timestamp" in event and not event["timestamp"].endswith("Z"):
        event["timestamp"] += "Z"
    # Put the event onto the standard Python queue from the background thread
    if event_queue:
        try:
            event_queue.put_nowait(json.dumps(event)) # Use put_nowait to avoid blocking the thread
            print("Put event onto queue.")
        except queue.Full:
             print("Event queue is full, dropping event.")
        except Exception as e:
             print(f"Error putting event onto queue: {e}")
    else:
        print("Event queue not initialized.")

def broadcast_config_update(): # Simplified: no longer takes spots_config as arg
    print("broadcast_config_update called.")
    message = json.dumps({
        "type": "config_update" # <-- Simple message indicating config changed
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
    print("Event processor task started.") # Added logging
    while True:
        print("Event processor: Checking queue...") # Log inside the loop
        try:
            # Safely get an item from the synchronous queue in the async loop
            # anyio.to_thread.run_sync will run the blocking queue.get() in a worker thread
            message_str = await anyio.to_thread.run_sync(event_queue.get)
            print(f"Processing message from queue in event_processor: {message_str}") # Added log

            # Add a log here to confirm we are about to broadcast
            print("Event processor: About to broadcast message...")

            await manager.broadcast(message_str)
            # Add a small sleep after processing a message
            await anyio.sleep(0.5) # Increased sleep duration

        except Exception as e:
            print(f"Error in event processor task: {e}") # Added logging
            # Add a small sleep to prevent a tight loop in case of persistent errors
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
            is_occupied = current_spot_statuses.get(spot_id, False) # <-- Use current_spot_statuses
            spots_with_status.append({
                "id": spot_id,
                "x": s["bbox"][0],
                "y": s["bbox"][1],
                "w": s["bbox"][2]-s["bbox"][0],
                "h": s["bbox"][3]-s["bbox"][1],
                "is_available": not is_occupied # is_available is the opposite of is_occupied
            })

        return {"spots": spots_with_status}

    except FileNotFoundError:
        print(f"spots.json not found at {SPOTS_PATH}") # Added logging
        # Return an empty list if spots.json is not found, or handle as an error based on requirements
        # For now, returning empty list as a fallback
        return {"spots": []}
    except json.JSONDecodeError:
        print(f"Error decoding spots.json at {SPOTS_PATH}") # Added logging
        raise HTTPException(status_code=500, detail="Error reading spots configuration")
    except Exception as e:
        print(f"Unexpected error in get_spots: {e}") # Added logging
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@app.post("/api/spots")
async def save_spots(config: dict = Body(...)): # Made async to await broadcast_config_update
    print("POST /api/spots endpoint accessed.")
    try:
        disk = {"spots": [{"id": s["id"],
                           "bbox": [s["x"], s["y"], s["x"]+s["w"], s["y"]+s["h"]]
                          } for s in config.get("spots", [])]}
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        print("spots.json saved.") # Added logging
        refresh_spots() # Call refresh_spots after saving
        print("spots refreshed.") # Added logging

        # After saving and refreshing spots, broadcast a simple config update message
        broadcast_config_update() # <-- Call the simplified broadcast

        return {"ok": True}
    except Exception as e:
        print(f"Error in save_spots: {e}") # Added logging
        raise HTTPException(500, f"Could not write spots.json: {e}")

# Video capture helper
def make_capture():
    src = os.getenv("VIDEO_SOURCE", "0")
    print(f"Attempting to open video source: {src!r}") # Added logging
    try:
        idx = int(src)
    except ValueError:
         # URL → use FFmpeg backend
        # Explicitly try CAP_FFMPEG backend for RTSP streams
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        # Optional: Enable more verbose FFmpeg logging if needed for debugging
        # cv2.setLogLevel(cv2.LOG_INFO)
    else:
        cap = cv2.VideoCapture(idx)

    if not cap.isOpened():
        print(f"Could not open video source {src!r}") # Added logging
        raise RuntimeError(f"Could not open video source {src!r}")
    print(f"Video source {src!r} opened successfully.") # Added logging
    return cap


# Video processing and frame generation for streaming and updates
def video_processor():
    """Reads video frames, performs detection, updates status, and stores latest frame."""
    print("video_processor started.") # Updated log message
    cap = None # Initialize cap outside the try block
    global current_spot_statuses # Declare global to modify the dictionary
    global latest_processed_frame # Declare global to modify the frame variable

    try:
        # --- Catch exceptions during initial capture setup ---
        try:
            print("video_processor: Attempting to open video source now.")
            cap = make_capture()
        except Exception as e:
            print(f"Failed to initialize video capture: {e}")
            # Exit the processor if capture fails
            return # Exit the thread
        # ----------------------------------------------------

        prev_states = {}
        empty_start = {}
        notified = {}
        VACANCY_DELAY = timedelta(seconds=2)
        vehicle_classes = {"car","truck","bus","motorbike","bicycle"}

        # --- Perform initial detection pass ---
        print("Performing initial detection pass...")
        ret, frame = cap.read()
        if not ret:
             print("Failed to read initial frame from video source. Cannot perform initial detection.")
             # We can still proceed, but initial statuses will be based on defaults (False/free)
             # Or choose to exit if initial frame is mandatory
             # For now, we'll proceed with default False and rely on the loop to eventually get frames
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

        # Update current_spot_statuses based on initial detection
        for sid,(sx,sy,sw,sh) in SPOTS.items():
             occ = any(
                 sx <= (bx1+bx2)/2 <= sx+sw and sy <= (by1+by2)/2 <= sy+sh
                 for bx1,by1,bx2,by2 in initial_vehicle_boxes
             )
             current_spot_statuses[str(sid)] = occ # Ensure key is string

        print("Initial detection pass complete. Initial spot statuses:", current_spot_statuses)

        # Drawing on the initial frame (for the webcam_feed endpoint)
        # Use current_spot_statuses for drawing color on the initial frame
        if ret: # Only draw if initial frame was successfully read
            frame_with_overlays = frame.copy() # Draw on a copy
            for sid,(sx,sy,sw,sh) in SPOTS.items():
                x1,y1 = int(sx),int(sy)
                x2,y2 = int(sx+sw),int(sy+sh)
                # Ensure spot_id is string when accessing current_spot_statuses
                color = (0,0,255) if current_spot_statuses.get(str(sid), False) else (0,255,0) # Red if occupied (True), Green if free (False)
                cv2.rectangle(frame_with_overlays,(x1,y1),(x2,y2),color,2)
                cv2.putText(frame_with_overlays,f"Spot {sid}",(x1,y1-10),cv2.FONT_HERSHEY_SIMPLEX,0.7,color,2)
            for bx1,by1,bx2,by2 in initial_vehicle_boxes:
                cv2.rectangle(frame_with_overlays,(int(bx1),int(by1)),(int(bx2),int(by2)),(0,255,255),2)

            # Store the initial processed frame
            with frame_lock: # Use the lock when accessing the shared variable
                latest_processed_frame = frame_with_overlays
        else:
             # If initial frame failed, set latest_processed_frame to None or a placeholder
             with frame_lock:
                 latest_processed_frame = None # Or a black image np.zeros(...)

        # --- Start the main processing loop ---
        while True:
            # print("Processing frame...") # Uncomment for verbose frame logging
            # Initialize/remove spots in internal state based on SPOTS
            # Note: current_spot_statuses is now initialized before the loop
            for sid in SPOTS:
                # Ensure spot_id is string when accessing current_spot_statuses
                prev_states.setdefault(sid, current_spot_statuses.get(str(sid), False)) # Initialize prev_states from current_spot_statuses
                empty_start.setdefault(sid, None)
                notified.setdefault(sid, False)
            for sid in list(prev_states):
                if sid not in SPOTS:
                    print(f"Removing state for spot {sid} as it's no longer in SPOTS.") # Added logging
                    prev_states.pop(sid)
                    empty_start.pop(sid)
                    notified.pop(sid)
                    current_spot_statuses.pop(sid) # Remove status for removed spots


            print("Attempting to read frame...") # Added logging before read
            ret, frame = cap.read()
            print(f"Frame read result: {ret}") # Added logging after read
            if not ret:
                print("Failed to read frame from video source. Attempting to re-open...")
                # Attempt to re-open the capture if it fails
                if cap: # Check if cap is not None before releasing
                    print("Releasing capture object...") # Added logging
                    cap.release() # Release the old capture object
                time.sleep(2) # Add a small delay before attempting to re-open
                print("Attempting to re-open video capture now.")
                try:
                    cap = make_capture()
                    print(f"Capture re-opened successfully: {cap.isOpened()}") # Added logging
                    if not cap.isOpened():
                         print("Failed to re-open video capture. Stopping processing.")
                         break # Exit the loop if re-opening fails
                except Exception as e:
                     print(f"Error during video capture re-open: {e}. Stopping processing.")
                     break # Exit the loop if re-opening fails completely
                continue # Skip the rest of the loop for this frame

            # --- Frame Processing ---
            try: # Added try-except block around core processing
                results = detect(frame)
                vehicle_boxes = []
                for res in results:
                    boxes = res.boxes.xyxy.tolist()
                    classes = res.boxes.cls.tolist()
                    for i, cls_idx in enumerate(classes):
                        if res.names[cls_idx] in vehicle_classes:
                            vehicle_boxes.append(boxes[i])

                curr_states = {}
                for sid,(sx,sy,sw,sh) in SPOTS.items():
                    occ = any(
                        sx <= (bx1+bx2)/2 <= sx+sw and sy <= (by1+by2)/2 <= sy+sh
                        for bx1,by1,bx2,by2 in vehicle_boxes
                    )
                    curr_states[sid] = occ

                now = datetime.utcnow()
                for sid in SPOTS:
                    was = prev_states.get(sid, False) # Use .get with default for safety
                    is_ = curr_states.get(sid, False) # Use .get with default for safety

                    if was and not is_:
                        print(f"Spot {sid} changed from occupied to vacant.")
                        empty_start[sid] = now
                        notified[sid] = False

                    # Check for vacancy notification condition
                    if not is_ and empty_start.get(sid) is not None and not notified.get(sid, False) and now - empty_start[sid] >= VACANCY_DELAY:
                        print(f"Spot {sid} vacant for {VACANCY_DELAY}, sending notification.")
                        with SessionLocal() as session:
                            evt = VacancyEvent(timestamp=now, spot_id=sid, camera_id="main")
                            session.add(evt); session.commit()
                        broadcast_vacancy({"spot_id":str(sid), "timestamp":now.isoformat(), "status":"free"}) # Send "free" status
                        notified[sid] = True
                        current_spot_statuses[str(sid)] = False # Update global status after sending event

                    # Check for occupied status change
                    if not was and is_:
                        print(f"Spot {sid} changed from vacant to occupied.")
                        broadcast_vacancy({"spot_id":str(sid), "timestamp":now.isoformat(), "status":"occupied"}) # Send "occupied" status
                        current_spot_statuses[str(sid)] = True # Update global status after sending event

                    # Reset timers if spot becomes occupied
                    if is_:
                        empty_start[sid] = None
                        notified[sid] = False

                prev_states = curr_states.copy()

                # Drawing on the frame (for the webcam_feed endpoint)
                # Use current_spot_statuses for drawing color
                frame_with_overlays = frame.copy() # Draw on a copy to avoid modifying the original frame if needed elsewhere
                for sid,(sx,sy,sw,sh) in SPOTS.items():
                    x1,y1 = int(sx),int(sy)
                    x2,y2 = int(sx+sw),int(sy+sh)
                    # Ensure spot_id is string when accessing current_spot_statuses
                    color = (0,0,255) if current_spot_statuses.get(str(sid), False) else (0,255,0) # Red if occupied (True), Green if free (False)
                    cv2.rectangle(frame_with_overlays,(x1,y1),(x2,y2),color,2)
                    cv2.putText(frame_with_overlays,f"Spot {sid}",(x1,y1-10),cv2.FONT_HERSHEY_SIMPLEX,0.7,color,2)
                for bx1,by1,bx2,by2 in vehicle_boxes:
                    cv2.rectangle(frame_with_overlays,(int(bx1),int(by1)),(int(bx2),int(by2)),(0,255,255),2)

                # Store the latest processed frame in the global variable
                with frame_lock: # Use the lock when accessing the shared variable
                    latest_processed_frame = frame_with_overlays

            except Exception as e:
                 print(f"Error during frame processing loop: {e}") # Log specific error

            # Optional: Add a small sleep to avoid consuming too much CPU if processing is very fast
            # time.sleep(0.01) # Use time.sleep in a thread

    except Exception as e:
        print(f"Error in video_processor: {e}") # Updated log message
    finally:
        print("video_processor finished. Releasing capture.") # Updated log message
        if cap:
            cap.release()


# Add this startup event to run the video processing in a background thread
@app.on_event("startup")
async def startup_event():
    print("App startup event triggered. Starting video processing background task.")
    global event_queue # Only need to declare global for the queue
    try:
        # Initialize the standard Python queue
        event_queue = queue.Queue(maxsize=100) # Set a reasonable maxsize
        print("Event queue initialized.")

        # Use threading.Thread to run the synchronous video_processor function in a separate thread
        # This is appropriate for blocking I/O like video capture/processing.
        thread = threading.Thread(target=video_processor, daemon=True)
        thread.start()
        print("video_processor started in a background thread.")

        # Start the event_processor task in the main async loop
        asyncio.create_task(event_processor())
        print("Event processor task scheduled using asyncio.create_task.")

    except Exception as e:
        print(f"Error during startup event: {e}")
        # Log the specific sub-exception if it's an ExceptionGroup (from asyncio tasks)
        # Need to import ExceptionGroup if using it directly
        # from exceptiongroup import ExceptionGroup # Import ExceptionGroup
        # if isinstance(e, ExceptionGroup):
        #     print("Sub-exceptions:")
        #     for i, sub_e in enumerate(e.exceptions):
        #         print(f"  {i+1}: {sub_e}")
        # else:
        print(f"Details of the error: {e}")


# Video stream endpoint for the webcam feed
@app.get("/webcam_feed")
def webcam_feed():
    print("GET /webcam_feed endpoint accessed.")

    def generate_streaming_frames():
        # This generator yields the latest frame from the background processor
        while True:
            with frame_lock: # Acquire the lock before accessing the shared frame
                frame = latest_processed_frame
            if frame is not None:
                success, jpeg = cv2.imencode('.jpg', frame)
                if success:
                    # Corrected the boundary ending from \r\n\r\r to \r\n\r\n
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            # Add a small sleep to control the streaming rate
            time.sleep(0.03) # Adjust this value to control the frame rate (e.g., 0.03 for ~30fps)


    # Return a StreamingResponse that uses the generate_streaming_frames generator
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

