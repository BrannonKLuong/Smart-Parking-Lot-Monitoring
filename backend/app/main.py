import json
import cv2
import anyio
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
    raise RuntimeError(f"Firebase credential not found at {cred_path!r}")
cred = credentials.Certificate(cred_path)
initialize_app(cred)

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
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)
    async def broadcast(self, message: str):
        for ws in list(self.active_connections):
            try:
                await ws.send_text(message)
            except:
                self.disconnect(ws)
manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)

def broadcast_vacancy(event: dict):
    if "timestamp" in event and not event["timestamp"].endswith("Z"):
        event["timestamp"] += "Z"
    anyio.from_thread.run(manager.broadcast, json.dumps(event))

# Spots API
@app.get("/api/spots")
def get_spots():
    raw = json.loads(SPOTS_PATH.read_text())
    return {"spots": [
        {"id": s["id"], "x": s["bbox"][0], "y": s["bbox"][1],
         "w": s["bbox"][2]-s["bbox"][0], "h": s["bbox"][3]-s["bbox"][1]}
        for s in raw.get("spots", [])
    ]}

@app.post("/api/spots")
def save_spots(config: dict = Body(...)):
    try:
        disk = {"spots": [{"id": s["id"],
                           "bbox": [s["x"], s["y"], s["x"]+s["w"], s["y"]+s["h"]]
                          } for s in config.get("spots", [])]}
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        refresh_spots()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"Could not write spots.json: {e}")

# Video capture helper
def make_capture():
    src = os.getenv("VIDEO_SOURCE", "0")
    try:
        idx = int(src)
    except ValueError:
         # URL → use FFmpeg backend
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(idx)

    if not cap.isOpened():
        raise RuntimeError(f"Could not open video source {src!r}")
    return cap


# Video stream + detection
def frame_generator():
    cap = make_capture()
    prev_states = {}
    empty_start = {}
    notified = {}
    display_states = {}
    VACANCY_DELAY = timedelta(seconds=2)
    vehicle_classes = {"car","truck","bus","motorbike","bicycle"}
    try:
        while True:
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
                break
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
                was = prev_states[sid]
                is_ = curr_states[sid]
                if was and not is_:
                    empty_start[sid] = now
                    notified[sid] = False
                if not is_ and empty_start[sid] and not notified[sid] and now - empty_start[sid] >= VACANCY_DELAY:
                    with SessionLocal() as session:
                        evt = VacancyEvent(timestamp=now, spot_id=sid, camera_id="main")
                        session.add(evt); session.commit()
                    broadcast_vacancy({"spot_id":sid, "timestamp":now.isoformat()})
                    notified[sid] = True
                    display_states[sid] = False
                    notify_all(sid)
                if not was and is_:
                    broadcast_vacancy({"spot_id":sid, "timestamp":now.isoformat(), "status":"occupied"})
                    display_states[sid] = True
                if is_:
                    empty_start[sid] = None
                    notified[sid] = False
            prev_states = curr_states.copy()
            for sid,(sx,sy,sw,sh) in SPOTS.items():
                x1,y1 = int(sx),int(sy)
                x2,y2 = int(sx+sw),int(sy+sh)
                color = (0,0,255) if display_states[sid] else (0,255,0)
                cv2.rectangle(frame,(x1,y1),(x2,y2),color,2)
                cv2.putText(frame,f"Spot {sid}",(x1,y1-10),cv2.FONT_HERSHEY_SIMPLEX,0.7,color,2)
            for bx1,by1,bx2,by2 in vehicle_boxes:
                cv2.rectangle(frame,(int(bx1),int(by1)),(int(bx2),int(by2)),(0,255,255),2)
            success, jpeg = cv2.imencode('.jpg', frame)
            if not success:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
    finally:
        cap.release()

@app.get("/webcam_feed")
def webcam_feed():
    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/test_event")
def test_event():
    spot_id = 1
    evt = {"spot_id": spot_id, "timestamp": datetime.utcnow().isoformat()}
    broadcast_vacancy(evt)

    # send the push and capture the result
    resp: messaging.BatchResponse = notify_all(spot_id)
    fcm_info = {
        "success_count": resp.success_count if resp else 0,
        "failure_count": resp.failure_count if resp else 0
    }

    return {"sent": evt, "fcm": fcm_info}

class TokenIn(BaseModel):
    token: str
    platform: str = "android"

@app.post("/api/register_token")
async def register_token(data: TokenIn):
    with Session(engine) as sess:
        if not sess.exec(select(DeviceToken).where(DeviceToken.token == data.token)).first():
            sess.add(DeviceToken(token=data.token, platform=data.platform))
            sess.commit()
    return {"status":"ok"}
