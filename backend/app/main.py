import json
import cv2
import anyio
from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles 

from inference.cv_model import detect
from .spot_logic import get_spot_states, detect_vacancies, SPOTS
from .db import engine, Base, SessionLocal, VacancyEvent

import io
from contextlib import redirect_stdout, redirect_stderr

# Initialize database tables
Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# app.mount("/static", StaticFiles(directory="../static"), name="static")
# @app.get("/", include_in_schema=False)
# def index():
#     return FileResponse("../static/stream.html")


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
    prev_states   = {spot_id: False for spot_id in SPOTS}
    empty_start   = {spot_id: None  for spot_id in SPOTS}
    notified      = {spot_id: False for spot_id in SPOTS}

    # Minimum time a spot must remain empty before we notify
    VACANCY_DELAY = timedelta(seconds=2)

    # Only these classes count as vehicles
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # 1) Draw your ROIs
            for spot_id, (sx, sy, sw, sh) in SPOTS.items():
                x1, y1 = int(sx), int(sy)
                x2, y2 = int(sx + sw), int(sy + sh)
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
                cv2.putText(frame, f"Spot {spot_id}",
                            (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (255, 0, 0), 2)

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
                x1, y1 = sx, sy
                x2, y2 = sx + sw, sy + sh
                occupied = False
                for bx1, by1, bx2, by2 in vehicle_boxes:
                    cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
                    if x1 <= cx <= x2 and y1 <= cy <= y2:
                        occupied = True
                        break
                curr_states[spot_id] = occupied

            # 4) Hysteresis: only fire once after VACANCY_DELAY
            now = datetime.utcnow()
            for spot_id in SPOTS:
                was_occ = prev_states[spot_id]
                is_occ  = curr_states[spot_id]

                # just turned from occupied → empty: start timer
                if was_occ and not is_occ:
                    empty_start[spot_id] = now
                    notified[spot_id]    = False

                # if still empty, check elapsed
                if not is_occ and empty_start[spot_id]:
                    elapsed = now - empty_start[spot_id]
                    if not notified[spot_id] and elapsed >= VACANCY_DELAY:
                        # 4a) Persist to DB
                        session = SessionLocal()
                        evt = VacancyEvent(
                            timestamp=now,
                            spot_id=spot_id,
                            camera_id="main"
                        )
                        session.add(evt)
                        session.commit()
                        session.close()

                        # 4b) Broadcast over WS
                        broadcast_vacancy({
                            "spot_id":   spot_id,
                            "timestamp": now.isoformat()
                        })
                        notified[spot_id] = True
                
                if not was_occ and is_occ:
                    broadcast_vacancy({
                        "spot_id":   spot_id,
                        "timestamp": now.isoformat(),
                        "status": "occupied"
                    })

                # if re‐occupied, reset
                if is_occ:
                    empty_start[spot_id] = None
                    notified[spot_id]    = False

            prev_states = curr_states.copy()

            # 5) Draw vehicle boxes
            for bx1, by1, bx2, by2 in vehicle_boxes:
                cv2.rectangle(frame,
                              (int(bx1), int(by1)),
                              (int(bx2), int(by2)),
                              (0, 255, 0), 2)

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
    """
    MJPEG stream showing ROIs, vehicle detections, and vacancy detection.
    """
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/test_event")
def test_event():
    """
    Manually broadcast a fake vacancy on spot 1 for debugging.
    """
    evt = {
        "spot_id": 1,
        "timestamp": datetime.utcnow().isoformat()
    }
    broadcast_vacancy(evt)
    return {"sent": evt}

app.mount("/", StaticFiles(directory="../static", html=True), name="static")