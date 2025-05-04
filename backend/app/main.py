import json
import cv2
import anyio
from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from inference.cv_model import detect
from .spot_logic import SPOTS
from .db import engine, Base, SessionLocal, VacancyEvent

# Initialize database tables
Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

def broadcast_vacancy(event: dict):
    """
    Send a vacancy event to all connected WebSocket clients from any thread.
    """
    if "timestamp" in event and not event["timestamp"].endswith("Z"):
        event["timestamp"] += "Z"  # Ensure UTC format
    anyio.from_thread.run(manager.broadcast, json.dumps(event))

def frame_generator():
    cap = cv2.VideoCapture(0)

    # Track previous state and vacancy timers
    prev_states = {spot_id: False for spot_id in SPOTS}
    empty_start = {spot_id: None  for spot_id in SPOTS}
    notified    = {spot_id: False for spot_id in SPOTS}

    VACANCY_DELAY = timedelta(seconds=2)
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # 2) Run YOLO and filter for vehicle boxes
            results = detect(frame)
            vehicle_boxes = []
            for res in results:
                boxes   = res.boxes.xyxy.tolist()
                classes = res.boxes.cls.tolist()
                for i, cls_idx in enumerate(classes):
                    if res.names[cls_idx] in vehicle_classes:
                        vehicle_boxes.append(boxes[i])

            # 3) Compute occupied/empty per spot by checking box centers
            curr_states = {}
            for spot_id, (sx, sy, sw, sh) in SPOTS.items():
                occupied = False
                for bx1, by1, bx2, by2 in vehicle_boxes:
                    cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
                    if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
                        occupied = True
                        break
                curr_states[spot_id] = occupied

            # 1) Draw your ROIs, color‐coded by occupancy (red=occupied, green=free)
            for spot_id, (sx, sy, sw, sh) in SPOTS.items():
                x1, y1 = int(sx), int(sy)
                x2, y2 = int(sx + sw), int(sy + sh)

                occupied = curr_states.get(spot_id, True)
                roi_color = (0, 0, 255) if occupied else (0, 255, 0)  # BGR

                cv2.rectangle(frame, (x1, y1), (x2, y2), roi_color, 2)
                cv2.putText(frame,
                            f"Spot {spot_id}",
                            (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            roi_color,
                            2)

            # 4) Hysteresis: only fire once after VACANCY_DELAY
            now = datetime.utcnow()
            for spot_id in SPOTS:
                was_occ = prev_states[spot_id]
                is_occ  = curr_states[spot_id]

                # occupied → empty: start timer
                if was_occ and not is_occ:
                    empty_start[spot_id] = now
                    notified[spot_id]    = False

                # still empty and delay passed → vacancy
                if not is_occ and empty_start[spot_id]:
                    elapsed = now - empty_start[spot_id]
                    if not notified[spot_id] and elapsed >= VACANCY_DELAY:
                        # Persist to DB
                        session = SessionLocal()
                        evt = VacancyEvent(
                            timestamp=now,
                            spot_id=spot_id,
                            camera_id="main"
                        )
                        session.add(evt)
                        session.commit()
                        session.close()

                        # Broadcast vacancy
                        broadcast_vacancy({
                            "spot_id":   spot_id,
                            "timestamp": now.isoformat()
                        })
                        notified[spot_id] = True

                # empty → occupied: broadcast occupancy
                if not was_occ and is_occ:
                    broadcast_vacancy({
                        "spot_id":   spot_id,
                        "timestamp": now.isoformat(),
                        "status":    "occupied"
                    })

                # reset on re‐occupy
                if is_occ:
                    empty_start[spot_id] = None
                    notified[spot_id]    = False

            prev_states = curr_states.copy()

            # 5) Draw vehicle detection boxes in yellow
            det_color = (0, 255, 255)
            for bx1, by1, bx2, by2 in vehicle_boxes:
                cv2.rectangle(frame,
                              (int(bx1), int(by1)),
                              (int(bx2), int(by2)),
                              det_color, 2)

            # 6) Encode to JPEG and yield MJPEG chunk
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
    evt = {"spot_id": 1, "timestamp": datetime.utcnow().isoformat()}
    broadcast_vacancy(evt)
    return {"sent": evt}

app.mount("/", StaticFiles(directory="../static", html=True), name="static")
