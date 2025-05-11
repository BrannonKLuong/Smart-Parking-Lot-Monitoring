import json
import cv2
import anyio
import threading # Import the threading module
import queue # Import the standard queue module
import asyncio # Import the asyncio module
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()   # <-- this will read .env automatically

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from inference.cv_model import detect
from .spot_logic import SPOTS, refresh_spots
from .db import engine, Base, SessionLocal, VacancyEvent, DeviceToken

import os
import firebase_admin
from firebase_admin import credentials, messaging, initialize_app
from pydantic import BaseModel
from sqlmodel import Session, select

# We no longer need to import create_task_group or create_task directly from anyio for spawning event_processor
# from anyio import create_task_group # Removed direct import
# from anyio import create_task # Removed direct import

from .db import DeviceToken
from .notifications import notify_all

# Initialize Firebase
cred_path = os.environ.get("FIREBASE_CRED", "")
if not cred_path or not os.path.isfile(cred_path):
    # Raising RuntimeError here as Firebase is likely required for notifications
    raise RuntimeError(f"Firebase credential not found at {cred_path!r}")
cred = credentials.Certificate(cred_path)
try:
    initialize_app(cred)
    print("Firebase app initialized successfully.")
except ValueError:
    print("Firebase app already initialized.") # Handle re-initialization if necessary


# Initialize DB tables
Base.metadata.create_all(bind=engine)

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
        print("ConnectionManager initialized.")
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
        print(f"WebSocket connected: {ws}. Total connections: {len(self.active_connections)}")
        # Add a log here to confirm the connection is added to the list
        print(f"Active connections after connect: {len(self.active_connections)}")
    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
            print(f"WebSocket disconnected: {ws}. Total connections: {len(self.active_connections)}")
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
    print("WebSocket endpoint accessed.")
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
        print("WebSocketDisconnect exception caught.")
        manager.disconnect(ws)
    except Exception as e:
        print(f"Unexpected error in websocket_endpoint: {e}")
        manager.disconnect(ws)

# Global standard Python queue for communication between thread and async loop
# Initialize this during startup
event_queue: queue.Queue = None

def broadcast_vacancy(event: dict):
    print(f"broadcast_vacancy called with event: {event}")
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


# Async task to process events from the queue and broadcast via WebSocket
async def event_processor():
    print("Event processor task started.")
    while True:
        print("Event processor: Checking queue...") # Log inside the loop
        try:
            # Safely get an item from the synchronous queue in the async loop
            # anyio.to_thread.run_sync will run the blocking queue.get() in a worker thread
            message = await anyio.to_thread.run_sync(event_queue.get)
            print(f"Processing message from queue in event_processor: {message}") # Added log

            # Add a log here to confirm we are about to broadcast
            print("Event processor: About to broadcast message...")

            await manager.broadcast(message)
            # Add a small sleep after processing a message
            await anyio.sleep(0.5) # Increased sleep duration

        except Exception as e:
            print(f"Error in event processor task: {e}")
            # Add a small sleep to prevent a tight loop in case of persistent errors
            await anyio.sleep(1)


# Spots API
@app.get("/api/spots")
def get_spots():
    print("GET /api/spots endpoint accessed.")
    try:
        raw = json.loads(SPOTS_PATH.read_text())
        return {"spots": [
            {"id": s["id"], "x": s["bbox"][0], "y": s["bbox"][1],
             "w": s["bbox"][2]-s["bbox"][0], "h": s["bbox"][3]-s["bbox"][1]}
            for s in raw.get("spots", [])
        ]}
    except FileNotFoundError:
        print(f"spots.json not found at {SPOTS_PATH}")
        raise HTTPException(status_code=404, detail="Spots configuration not found")
    except json.JSONDecodeError:
        print(f"Error decoding spots.json at {SPOTS_PATH}")
        raise HTTPException(status_code=500, detail="Error reading spots configuration")
    except Exception as e:
        print(f"Unexpected error in get_spots: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@app.post("/api/spots")
def save_spots(config: dict = Body(...)):
    print("POST /api/spots endpoint accessed.")
    try:
        disk = {"spots": [{"id": s["id"],
                           "bbox": [s["x"], s["y"], s["x"]+s["w"], s["y"]+s["h"]]
                          } for s in config.get("spots", [])]}
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        refresh_spots()
        print("spots.json saved and spots refreshed.")
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
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(idx)

    if not cap.isOpened():
        print(f"Could not open video source {src!r}")
        raise RuntimeError(f"Could not open video source {src!r}")
    print(f"Video source {src!r} opened successfully.")
    return cap


# Video stream + detection (This will now run in a background thread)
def frame_generator():
    """Runs the video processing and sends updates via WebSocket."""
    print("frame_generator started.") # Updated log message
    cap = None # Initialize cap outside the try block
    try:
        # --- Catch exceptions during initial capture setup ---
        try:
            cap = make_capture()
        except Exception as e:
            print(f"Failed to initialize video capture: {e}")
            # Exit the generator if capture fails
            return
        # ----------------------------------------------------

        prev_states = {}
        empty_start = {}
        notified = {}
        display_states = {}
        VACANCY_DELAY = timedelta(seconds=2)
        vehicle_classes = {"car","truck","bus","motorbike","bicycle"}

        while True:
            # print("Processing frame...") # Uncomment for verbose frame logging
            for sid in SPOTS:
                prev_states.setdefault(sid, False)
                empty_start.setdefault(sid, None)
                notified.setdefault(sid, False)
                display_states.setdefault(sid, True)
            for sid in list(prev_states):
                if sid not in SPOTS:
                    prev_states.pop(sid)
                    empty_start.pop(sid)
                    notified.pop(sid)
                    display_states.pop(sid)

            ret, frame = cap.read()
            if not ret:
                print("Failed to read frame from video source. Attempting to re-open...")
                # Attempt to re-open the capture if it fails
                if cap: # Check if cap is not None before releasing
                    cap.release() # Release the old capture object
                print("Attempting to re-open video capture...") # Use direct print in thread

                try:
                    cap = make_capture()
                    if not cap.isOpened():
                         print("Failed to re-open video capture. Stopping processing.") # Use direct print
                         break # Exit the loop if re-opening fails
                except Exception as e:
                     print(f"Error during video capture re-open: {e}. Stopping processing.") # Use direct print
                     break # Exit the loop if re-opening fails completely
                continue # Skip the rest of the loop for this frame

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
                    display_states[sid] = False
                    # notify_all(sid) # FCM notification - uncomment if needed

                # Check for occupied status change
                if not was and is_:
                    print(f"Spot {sid} changed from vacant to occupied.")
                    broadcast_vacancy({"spot_id":str(sid), "timestamp":now.isoformat(), "status":"occupied"}) # Send "occupied" status
                    display_states[sid] = True

                # Reset timers if spot becomes occupied
                if is_:
                    empty_start[sid] = None
                    notified[sid] = False

            prev_states = curr_states.copy()

            # Note: Drawing on the frame here will only affect the webcam_feed endpoint.
            # The WebSocket clients receive only the status updates.
            # If you want the video feed with overlays in the app, you'd need
            # to load the /webcam_feed endpoint in the WebView or a separate view.

            # Optional: Add a small sleep to avoid consuming too much CPU if processing is very fast
            # time.sleep(0.01) # Use time.sleep in a thread

    except Exception as e:
        print(f"Error in frame_generator: {e}") # Updated log message
    finally:
        print("frame_generator finished.") # Updated log message
        if cap:
            cap.release()


# Add this startup event to initialize the queue and start the background thread
@app.on_event("startup")
async def startup_event():
    print("App startup event triggered. Starting video processing background task.")
    global event_queue # Only need to declare global for the queue
    try:
        # Initialize the standard Python queue
        event_queue = queue.Queue(maxsize=100) # Set a reasonable maxsize
        print("Event queue initialized.")

        # Use threading to run the synchronous frame_generator in a separate thread
        thread = threading.Thread(target=frame_generator, daemon=True)
        thread.start()
        print("frame_generator started in a background thread.") # Added print

    except Exception as e:
        print(f"Error starting video processing background task: {e}")
        # With threading, exceptions in the thread won't be caught here directly.
        # Error handling is needed inside frame_generator itself.
        print(f"Details of the error: {e}")

# Register the event_processor as a separate startup event handler
@app.on_event("startup")
async def start_event_processor_task():
     print("Starting event processor task.")
     # Use asyncio.create_task to start the async task directly
     # This task will run in the main async loop
     asyncio.create_task(event_processor())
     print("Event processor task scheduled using asyncio.create_task.")


# Video stream endpoint (optional, for viewing the feed with overlays)
@app.get("/webcam_feed")
def webcam_feed():
    print("GET /webcam_feed endpoint accessed.")
    # Note: This endpoint runs its own frame_generator instance.
    # The background task is what sends WebSocket updates.
    # This endpoint is for streaming video, not for the background processing that sends WebSocket messages.
    # It's likely you don't need to call frame_generator here if the video feed is not used directly.
    # If you DO need the video feed, this function should yield frames as before.
    # If not, you could remove or simplify this endpoint.
    # For now, keeping the original streaming logic:
    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")


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
