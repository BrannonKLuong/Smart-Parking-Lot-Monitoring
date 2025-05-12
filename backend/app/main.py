import json
import cv2
import anyio
import threading # Import the threading module
import queue # Import the standard queue module
import asyncio # Import the asyncio module
import time # Import time for sleep in thread
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

from .db import DeviceToken
from .notifications import notify_all

# Initialize Firebase
cred_path = os.environ.get("FIREBASE_CRED", "")
if not cred_path or not os.path.isfile(cred_path):
    # Restored RuntimeError as requested
    raise RuntimeError(f"Firebase credential not found at {cred_path!r}. FCM notifications will not work.")
# The 'cred' variable needs to be defined outside the if block if you want to use it later,
# but initialize_app handles the credential internally after initialization.
# We still need to load the credential to pass to initialize_app.
cred = credentials.Certificate(cred_path)
try:
    initialize_app(cred)
    print("Firebase app initialized successfully.")
except ValueError:
    print("Firebase app already initialized.") # Handle re-initialization if necessary


# Initialize DB tables
# Check if tables exist before creating to avoid errors on re-runs
# from sqlalchemy import inspect
# inspector = inspect(engine)
# if not inspector.has_table("occupancy") or not inspector.has_table("vacancy_events") or not inspector.has_table("devicetoken"):
#     print("Creating database tables...")
Base.metadata.create_all(bind=engine)
# else:
#     print("Database tables already exist.")


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

def broadcast_config_update(spots_config: list):
    print(f"broadcast_config_update called with {len(spots_config)} spots.")
    message = json.dumps({
        "type": "config_update",
        "spots": spots_config
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
    print("GET /api/spots endpoint accessed.") # Added logging
    try:
        raw = json.loads(SPOTS_PATH.read_text())
        return {"spots": [
            {"id": s["id"], "x": s["bbox"][0], "y": s["bbox"][1],
             "w": s["bbox"][2]-s["bbox"][0], "h": s["bbox"][3]-s["bbox"][1]}
            for s in raw.get("spots", [])
        ]}
    except FileNotFoundError:
        print(f"spots.json not found at {SPOTS_PATH}") # Added logging
        # Return an empty list if spots.json is not found, or handle as an error based on requirements
        # For now, raising 404 as before:
        raise HTTPException(status_code=404, detail="Spots configuration not found")
    except json.JSONDecodeError:
        print(f"Error decoding spots.json at {SPOTS_PATH}") # Added logging
        raise HTTPException(status_code=500, detail="Error reading spots configuration")
    except Exception as e:
        print(f"Unexpected error in get_spots: {e}") # Added logging
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@app.post("/api/spots")
async def save_spots(config: dict = Body(...)): # Made async to await broadcast_config_update
    print("POST /api/spots endpoint accessed.") # Added logging
    try:
        disk = {"spots": [{"id": s["id"],
                           "bbox": [s["x"], s["y"], s["x"]+s["w"], s["y"]+s["h"]]
                          } for s in config.get("spots", [])]}
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        refresh_spots()
        print("spots.json saved and spots refreshed.") # Added logging

        # After saving and refreshing spots, broadcast the new configuration
        # We need to get the updated list of spots in the format expected by the Android app
        updated_spots_for_broadcast = [
             {"id": s["id"], "is_available": True} # Assume newly configured spots are initially available
             for s in disk.get("spots", [])
        ]
        broadcast_config_update(updated_spots_for_broadcast)

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


# Video stream + detection
def frame_generator():
    """Generates video frames with detection overlays."""
    print("frame_generator started.") # Added logging
    cap = None
    try:
        # Attempt to open video capture
        try:
            # Add a small delay before attempting to open the video source
            print("frame_generator: Waiting 5 seconds before opening video source...")
            time.sleep(5) # Sleep for 5 seconds
            print("frame_generator: Attempting to open video source now.")
            cap = make_capture()
        except Exception as e:
            print(f"Failed to initialize video capture: {e}")
            # If capture fails, yield nothing and exit
            return

        prev_states = {}
        empty_start = {}
        notified = {}
        display_states = {}
        VACANCY_DELAY = timedelta(seconds=2)
        vehicle_classes = {"car","truck","bus","motorbike","bicycle"}

        while True:
            try:
                ret, frame = cap.read()
                if not ret:
                    print("Failed to read frame from video source. Attempting to re-open...")
                    # Attempt to re-open the capture if it fails
                    if cap: # Check if cap is not None before releasing
                        cap.release() # Release the old capture object
                    print("Attempting to re-open video capture...") # Use direct print in thread

                    try:
                        # Add a small delay before attempting to re-open
                        time.sleep(2)
                        cap = make_capture()
                        if not cap.isOpened():
                             print("Failed to re-open video capture. Stopping processing.") # Use direct print
                             break # Exit the loop if re-opening fails
                    except Exception as e:
                         print(f"Error during video capture re-open: {e}. Stopping processing.") # Use direct print
                         break # Exit the loop if re-opening fails completely
                    continue # Skip the rest of the loop for this frame

                # --- Frame Processing ---
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
                        print(f"Spot {sid} changed from occupied to vacant.") # Added logging
                        empty_start[sid] = now
                        notified[sid] = False

                    # Check for vacancy notification condition
                    if not is_ and empty_start.get(sid) is not None and not notified.get(sid, False) and now - empty_start[sid] >= VACANCY_DELAY:
                        print(f"Spot {sid} vacant for {VACANCY_DELAY}, sending notification.") # Added logging
                        with SessionLocal() as session:
                            evt = VacancyEvent(timestamp=now, spot_id=sid, camera_id="main")
                            session.add(evt); session.commit()
                        broadcast_vacancy({"spot_id":str(sid), "timestamp":now.isoformat(), "status":"free"}) # Send "free" status
                        notified[sid] = True
                        display_states[sid] = False
                        # notify_all(sid) # FCM notification - uncomment if needed

                    # Check for occupied status change
                    if not was and is_:
                        print(f"Spot {sid} changed from vacant to occupied.") # Added logging
                        broadcast_vacancy({"spot_id":str(sid), "timestamp":now.isoformat(), "status":"occupied"}) # Send "occupied" status
                        display_states[sid] = True

                    # Reset timers if spot becomes occupied
                    if is_:
                        empty_start[sid] = None
                        notified[sid] = False

                prev_states = curr_states.copy()

                # Drawing on the frame (for the webcam_feed endpoint)
                for sid,(sx,sy,sw,sh) in SPOTS.items():
                    x1,y1 = int(sx),int(sy)
                    x2,y2 = int(sx+sw),int(sy+sh)
                    color = (0,0,255) if display_states.get(sid, True) else (0,255,0) # Use .get with default
                    cv2.rectangle(frame,(x1,y1),(x2,y2),color,2)
                    cv2.putText(frame,f"Spot {sid}",(x1,y1-10),cv2.FONT_HERSHEY_SIMPLEX,0.7,color,2)
                for bx1,by1,bx2,by2 in vehicle_boxes:
                    cv2.rectangle(frame,(int(bx1),int(by1)),(int(bx2),int(by2)),(0,255,255),2)

                success, jpeg = cv2.imencode('.jpg', frame)
                if not success:
                    print("Failed to encode frame to JPEG.") # Added logging
                    # Continue the loop if encoding fails, don't break the stream
                    continue

                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

            except Exception as e:
                # Log errors during frame processing but keep the stream alive if possible
                print(f"Error processing frame: {e}")
                # Optionally yield a placeholder image or skip the frame

    except Exception as e:
        # Catch any unexpected errors in the generator loop itself
        print(f"Critical error in frame_generator: {e}")
    finally:
        print("frame_generator finished. Releasing capture.") # Added logging
        if cap:
            cap.release()
        # Ensure the stream is terminated properly
        yield (b'--frame--\r\n') # Yield the closing boundary


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
    print("GET /webcam_feed endpoint accessed.") # Added logging
    # Note: This endpoint runs its own frame_generator instance.
    # The background task is what sends WebSocket updates.
    # This endpoint now correctly calls the frame_generator function
    # This endpoint is for streaming video, not for the background processing that sends WebSocket messages.
    # It's likely you don't need to call frame_generator here if the video feed is not used directly.
    # If you DO need the video feed, this function should yield frames as before.
    # If not, you could remove or simplify this endpoint.
    # For now, keeping the original streaming logic:
    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/test_event")
def test_event():
    print("GET /test_event endpoint accessed.") # Added logging
    spot_id = 1
    evt = {"spot_id": str(spot_id), "timestamp": datetime.utcnow().isoformat(), "status":"free"} # Added status for testing
    broadcast_vacancy(evt)

    # send the push and capture the result
    # resp: messaging.BatchResponse = notify_all(spot_id) # FCM notification - uncomment if needed
    # fcm_info = {
    #     "success_count": resp.success_count if resp else 0,
    #     "failure_count": resp.failure_count if resp else 0
    # }

    return {"sent": evt} # Removed fcm_info if notify_all is commented


class TokenIn(BaseModel):
    token: str
    platform: str = "android"

@app.post("/api/register_token")
async def register_token(data: TokenIn):
    print("POST /api/register_token endpoint accessed.") # Added logging
    with Session(engine) as sess:
        if not sess.exec(select(DeviceToken).where(DeviceToken.token == data.token)).first():
            sess.add(DeviceToken(token=data.token, platform=data.platform))
            sess.commit()
            print(f"Registered new device token: {data.token}") # Added logging
        else:
            print(f"Device token already exists: {data.token}") # Added logging
    return {"status":"ok"}

