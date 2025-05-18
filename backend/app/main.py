import json
import cv2
import anyio
import threading 
import queue 
import asyncio 
import time 
import numpy as np 
import os
from datetime import datetime, timedelta
from pathlib import Path
import traceback

from dotenv import load_dotenv
from firebase_admin import credentials, initialize_app
from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import SQLModel, Session, select

from inference.cv_model import detect
from .spot_logic import SPOTS, refresh_spots
from .db import engine, Base, SessionLocal, VacancyEvent, DeviceToken
from .notifications import notify_all

load_dotenv()

# --- Firebase Initialization --- #
cred_path = os.environ.get("FIREBASE_CRED", "")
if not cred_path or not os.path.isfile(cred_path):
    raise RuntimeError(f"Firebase credential not found at {cred_path!r}. FCM notifications will not work.")
cred = credentials.Certificate(cred_path)
try:
    initialize_app(cred)
    print("Firebase app initialized successfully.")
except ValueError:
    print("Firebase app already initialized.")

# --- Database Initialization --- #
def create_db_and_tables():
    Base.metadata.create_all(bind=engine)  
    SQLModel.metadata.create_all(bind=engine) 

# --- Paths --- #
APP_DIR     = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR    = BACKEND_DIR.parent
SPOTS_PATH  = BACKEND_DIR / "spots.json"

# --- FastAPI Application Setup --- #
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Static Files & Root Endpoint --- #
static_dir = ROOT_DIR / "static"
if static_dir.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(static_dir), html=False),
        name="static",
    )
else:
    print(f"Warning: Static directory not found at {static_dir}. Static files will not be served.")


index_html_path = static_dir / "index.html"
if index_html_path.is_file():
    @app.get("/", include_in_schema=False)
    async def serve_index():
        return FileResponse(str(index_html_path))
else:
    print(f"Warning: index.html not found at {index_html_path}. Root path will not serve the frontend.")
    @app.get("/", include_in_schema=False)
    async def serve_root_message():
        return {"message": "Welcome to Smart Parking API. Frontend index.html not found."}


# --- WebSocket Connection Manager --- #
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, message: str):
        disconnected_websockets = [] 

        for ws in list(self.active_connections): 
            try: 
                await ws.send_text(message)
            except Exception as e:
                print(f"Error sending message to {ws}: {e}")
                disconnected_websockets.append(ws)

        for ws in disconnected_websockets:
             self.disconnect(ws)


manager = ConnectionManager()

# @app.websocket("/ws")
# async def websocket_endpoint(ws: WebSocket):
#     await manager.connect(ws)
#     try:
#         while True:
#             try:
#                 await asyncio.wait_for(ws.receive_text(), timeout=60) 
#             except asyncio.TimeoutError:
#                 print(f"WebSocket {ws} received no message for 60 seconds, keeping connection open.")
#                 pass 
#     except WebSocketDisconnect:
#         print("WebSocketDisconnect exception caught.")
#         manager.disconnect(ws)
#     except Exception as e:
#         print(f"Unexpected error in websocket_endpoint: {e}") 
#         manager.disconnect(ws)

@app.get("/ws")
async def http_get_ws_path_test():
    print("HTTP GET request received on /ws path")
    return {"message": "HTTP GET to /ws received successfully"}

# --- Globals for Video Processing and Event Queue --- #
event_queue: queue.Queue = None
current_spot_statuses = {}
latest_processed_frame: np.ndarray = None
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

def broadcast_vacancy(event: dict):
    # print(f"Queueing vacancy event: {event}") # Debug if needed
    event["type"] = "spot_status_update"
    if "timestamp" in event and not event["timestamp"].endswith("Z"):
        event["timestamp"] += "Z"
    _enqueue_message(event)

def broadcast_config_update():
    # print("Queueing config update.") # Debug if needed
    _enqueue_message({"type": "config_update"})

# --- Event Processor (Queue to WebSocket) --- #
async def event_processor():
    while True:
        try:
            message_str = await anyio.to_thread.run_sync(event_queue.get)
            await manager.broadcast(message_str)

        except Exception as e:
            print(f"Error in event processor task: {e}")
            await anyio.sleep(1)

# --- API Endpoints --- #
@app.get("/api/spots")
def get_spots():
    print("CI/CD Test: /api/spots endpoint was hit - new version!")
    print("GET /api/spots endpoint accessed.")
    try:
        raw = json.loads(SPOTS_PATH.read_text())
        spots_with_status = []
        for s in raw.get("spots", []):
            spot_id = str(s["id"])
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
async def save_spots(config: dict = Body(...)): 
    print("POST /api/spots endpoint accessed.")
    try:
        disk = {"spots": [{"id": s["id"],
                           "bbox": [s["x"], s["y"], s["x"]+s["w"], s["y"]+s["h"]]
                          } for s in config.get("spots", [])]}
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        refresh_spots()
        broadcast_config_update()
        return {"ok": True}
    except Exception as e:
        print(f"Error in save_spots: {e}") 
        raise HTTPException(500, f"Could not write spots.json: {e}")

# Video capture helper
def make_capture():
    src = os.getenv("VIDEO_SOURCE", "0")
    try:
        idx = int(src)
    except ValueError:
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(idx)

    if not cap.isOpened():
        print(f"Could not open video source {src!r}") 
        raise RuntimeError(f"Could not open video source {src!r}")
    return cap


# Video processing and frame generation for streaming and updates
def video_processor():
    """Reads video frames, performs detection, updates status, and stores latest frame."""

    cap = None
    global current_spot_statuses
    global latest_processed_frame

    try:
        try:
            print("video_processor: Attempting to open video source now.")
            cap = make_capture()
        except Exception as e:
            print(f"Failed to initialize video capture: {e}")
            return 

        prev_states = {}  # Stores {spot_id_int: is_occupied_bool}
        empty_start = {}  # Stores {spot_id_str: datetime_obj_when_spot_became_empty}
        notified = {}     # Stores {spot_id_str: bool_if_notification_sent_for_current_vacancy}
        VACANCY_DELAY = timedelta(seconds=2)
        vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

        # --- Perform initial detection pass --- #
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

        for sid_int, (sx, sy, sw, sh) in SPOTS.items():
            sid_str = str(sid_int)
            is_initially_occupied = any(
                sx <= (bx1 + bx2) / 2 <= sx + sw and sy <= (by1 + by2) / 2 <= sy + sh
                for bx1, by1, bx2, by2 in initial_vehicle_boxes
            )
            current_spot_statuses[sid_str] = is_initially_occupied
            prev_states[sid_int] = is_initially_occupied
            notified[sid_str] = False 
            if not is_initially_occupied:
                empty_start[sid_str] = datetime.utcnow()
            else:
                empty_start[sid_str] = None

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
       
        # Main processing loop
        while True:
            current_spot_ids_int = set(SPOTS.keys())
            for sid_int_key in list(prev_states.keys()):
                if sid_int_key not in current_spot_ids_int:
                    print(f"Removing state for spot {sid_int_key} as it's no longer in SPOTS.")
                    prev_states.pop(sid_int_key, None)
                    empty_start.pop(str(sid_int_key), None)
                    notified.pop(str(sid_int_key), None)
                    current_spot_statuses.pop(str(sid_int_key), None)
            
            for sid_int_key in current_spot_ids_int:
                if sid_int_key not in prev_states: 
                    print(f"Initializing state for new spot {sid_int_key}.")
                    prev_states[sid_int_key] = False
                    current_spot_statuses[str(sid_int_key)] = False
                    empty_start[str(sid_int_key)] = datetime.utcnow()
                    notified[str(sid_int_key)] = False


            ret, frame = cap.read()
            if not ret:
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

                curr_states_detected_occupied = {} 
                for sid_int, (sx, sy, sw, sh) in SPOTS.items():
                    is_occupied_now = any(
                        sx <= (bx1 + bx2) / 2 <= sx + sw and sy <= (by1 + by2) / 2 <= sy + sh
                        for bx1, by1, bx2, by2 in vehicle_boxes
                    )
                    curr_states_detected_occupied[sid_int] = is_occupied_now

                now = datetime.utcnow()

                for sid_int in SPOTS:
                    sid_str = str(sid_int)

                    was_occupied = prev_states.get(sid_int, False) 
                    is_occupied = curr_states_detected_occupied.get(sid_int, False)

                    # --- State Change Detection --- #
                    if was_occupied and not is_occupied: 
                        print(f"Spot {sid_str} changed from occupied to vacant.")
                        empty_start[sid_str] = now
                        notified[sid_str] = False  # Reset notification status, eligible for new notification
                        current_spot_statuses[sid_str] = False
                        broadcast_vacancy({"spot_id": sid_str, "timestamp": now.isoformat(), "status": "free"})
                    
                    elif not was_occupied and is_occupied:  # Transition: Free -> Occupied
                        print(f"Spot {sid_str} changed from vacant to occupied.")
                        empty_start[sid_str] = None 
                        current_spot_statuses[sid_str] = True
                        broadcast_vacancy({"spot_id": sid_str, "timestamp": now.isoformat(), "status": "occupied"})

                    # --- Check for sending vacancy notification --- #
                    if not is_occupied and empty_start.get(sid_str) and not notified.get(sid_str, False):
                        if (now - empty_start[sid_str]) >= VACANCY_DELAY:
                            print(f"Spot {sid_str} confirmed vacant for {VACANCY_DELAY}, sending notification.")
                            
                            with SessionLocal() as session_db:
                                evt = VacancyEvent(timestamp=now, spot_id=sid_int, camera_id="main")
                                session_db.add(evt)
                                session_db.commit()
                            

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
                            
                            notified[sid_str] = True 

                prev_states = curr_states_detected_occupied.copy() # Update prev_states for the next iteration

                # --- Drawing on the frame --- #
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
                print(traceback.format_exc()) 

    except Exception as e:
        print(f"Outer error in video_processor: {e}")
        print(traceback.format_exc()) 
    finally:
        print("video_processor finished. Releasing capture.")
        if cap:
            cap.release()



# --- Application Startup --- #
@app.on_event("startup")
async def startup_event():
    print("Application startup...")
    create_db_and_tables()
    
    global event_queue
    event_queue = queue.Queue(maxsize=100)
    print("Event queue initialized.")

    # Start background tasks
    processing_thread = threading.Thread(target=video_processor, daemon=True)
    processing_thread.start()
    print("Video processing thread scheduled.")
    
    asyncio.create_task(event_processor())
    print("WebSocket event processor task scheduled.")
    print("Application startup complete.")


# --- Static Endpoints (Webcam Feed, Test) & Token Registration --- #
@app.get("/webcam_feed")
def webcam_feed():
    def generate_frames_for_stream():
        while True:
            with frame_lock:
                frame_data = latest_processed_frame
            if frame_data is not None:
                _ , encoded_jpeg = cv2.imencode('.jpg', frame_data)
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + encoded_jpeg.tobytes() + b'\r\n')
            time.sleep(1/30)
    return StreamingResponse(generate_frames_for_stream(), media_type="multipart/x-mixed-replace; boundary=frame")

class TokenIn(BaseModel):
    token: str
    platform: str = "android"

@app.get("/test_event")
def test_event():
    print("GET /test_event endpoint accessed.")
    spot_id = 1
    # Use broadcast_vacancy to send a test message via WebSocket
    broadcast_vacancy({"spot_id":str(spot_id), "timestamp":datetime.utcnow().isoformat(), "status":"free"})
    return {"sent_test_event": True}


@app.post("/api/register_token")
async def register_token(data: TokenIn):
    with Session(engine) as sess:
        if not sess.exec(select(DeviceToken).where(DeviceToken.token == data.token)).first():
            sess.add(DeviceToken(token=data.token, platform=data.platform))
            sess.commit()
            print(f"Registered new device token: {data.token}")
        else:
            print(f"Device token already exists: {data.token}")
    return {"status":"ok"}

