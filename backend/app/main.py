import json
import cv2
import anyio
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from inference.cv_model import detect
from .spot_logic import SPOTS, refresh_spots
from .db import engine, Base, SessionLocal, VacancyEvent

# Initialize DB tables
Base.metadata.create_all(bind=engine)

# Path to the single backend/spots.json
APP_DIR     = Path(__file__).resolve().parent     # .../backend/app
BACKEND_DIR = APP_DIR.parent                       # .../backend
ROOT_DIR    = BACKEND_DIR.parent                   # project root
SPOTS_PATH  = BACKEND_DIR / "spots.json"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1) Serve React's static assets under /static
app.mount(
    "/static",
    StaticFiles(directory=str(ROOT_DIR / "static"), html=False),
    name="static",
)

# 2) Serve index.html at /
@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(str(ROOT_DIR / "static" / "index.html"))


# --- WebSocket manager ---
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


# --- Spots Editor API ---
@app.get("/api/spots")
def get_spots():
    raw = json.loads(SPOTS_PATH.read_text())
    return {
        "spots": [
            {
                "id": spot["id"],
                "x":  spot["bbox"][0],
                "y":  spot["bbox"][1],
                "w":  spot["bbox"][2] - spot["bbox"][0],
                "h":  spot["bbox"][3] - spot["bbox"][1],
            }
            for spot in raw.get("spots", [])
        ]
    }

@app.post("/api/spots")
def save_spots(config: dict = Body(...)):
    try:
        disk = {
            "spots": [
                {
                    "id":   s["id"],
                    "bbox": [s["x"], s["y"], s["x"] + s["w"], s["y"] + s["h"]],
                }
                for s in config.get("spots", [])
            ]
        }
        SPOTS_PATH.write_text(json.dumps(disk, indent=2))
        refresh_spots()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"Could not write spots.json: {e}")


# --- Video stream + detection ---
def frame_generator():
    cap = cv2.VideoCapture(0)

    # Initial state maps
    prev_states    = {}
    empty_start    = {}
    notified       = {}
    display_states = {}

    VACANCY_DELAY   = timedelta(seconds=2)
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

    try:
        while True:
            # 1) Ensure every spot in SPOTS has a state entry
            for sid in SPOTS:
                prev_states.setdefault(sid, False)
                empty_start.setdefault(sid, None)
                notified.setdefault(sid, False)
                display_states.setdefault(sid, True)
            # 2) Clean up any removed spots
            for sid in list(prev_states):
                if sid not in SPOTS:
                    prev_states.pop(sid, None)
                    empty_start.pop(sid, None)
                    notified.pop(sid, None)
                    display_states.pop(sid, None)

            ret, frame = cap.read()
            if not ret:
                break

            # 3) Run YOLO and collect vehicle boxes
            results = detect(frame)
            vehicle_boxes = []
            for res in results:
                boxes   = res.boxes.xyxy.tolist()
                classes = res.boxes.cls.tolist()
                for i, cls_idx in enumerate(classes):
                    if res.names[cls_idx] in vehicle_classes:
                        vehicle_boxes.append(boxes[i])

            # 4) Compute current occupancy
            curr_states = {}
            for sid, (sx, sy, sw, sh) in SPOTS.items():
                occ = any(
                    sx <= (bx1+bx2)/2 <= sx+sw and
                    sy <= (by1+by2)/2 <= sy+sh
                    for bx1,by1,bx2,by2 in vehicle_boxes
                )
                curr_states[sid] = occ

            # 5) Hysteresis + event broadcast
            now = datetime.utcnow()
            for sid in SPOTS:
                was = prev_states.get(sid, False)
                is_ = curr_states.get(sid, False)

                # occupied→empty: start timer
                if was and not is_:
                    empty_start[sid] = now
                    notified[sid]    = False

                # still empty + delay → vacancy event
                if (not is_
                        and empty_start[sid]
                        and not notified[sid]
                        and now - empty_start[sid] >= VACANCY_DELAY):
                    session = SessionLocal()
                    evt = VacancyEvent(timestamp=now,
                                       spot_id=sid,
                                       camera_id="main")
                    session.add(evt)
                    session.commit()
                    session.close()

                    broadcast_vacancy({
                        "spot_id":   sid,
                        "timestamp": now.isoformat()
                    })
                    notified[sid]      = True
                    display_states[sid] = False

                # empty→occupied: immediate occupancy event
                if not was and is_:
                    broadcast_vacancy({
                        "spot_id":   sid,
                        "timestamp": now.isoformat(),
                        "status":    "occupied"
                    })
                    display_states[sid] = True

                # reset on any occupied frame
                if is_:
                    empty_start[sid] = None
                    notified[sid]    = False

            # roll prev_states forward
            prev_states = curr_states.copy()

            # 6) Draw all ROIs, using display_states.get() for safety
            for sid, (sx, sy, sw, sh) in SPOTS.items():
                x1, y1 = int(sx), int(sy)
                x2, y2 = int(sx + sw), int(sy + sh)
                color = (0, 0, 255) if display_states.get(sid, True) else (0, 255, 0)

                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame,
                            f"Spot {sid}",
                            (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            color,
                            2)

            # 7) Draw vehicle detection boxes
            for bx1, by1, bx2, by2 in vehicle_boxes:
                cv2.rectangle(frame,
                              (int(bx1), int(by1)),
                              (int(bx2), int(by2)),
                              (0, 255, 255),
                              2)

            # 8) JPEG encode & emit
            success, jpeg = cv2.imencode('.jpg', frame)
            if not success:
                continue

            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' +
                jpeg.tobytes() +
                b'\r\n'
            )

    finally:
        cap.release()



@app.get("/webcam_feed")
def webcam_feed():
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/test_event")
def test_event():
    evt = {"spot_id":1, "timestamp":datetime.utcnow().isoformat()}
    broadcast_vacancy(evt)
    return {"sent":evt}
