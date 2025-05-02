import json
import cv2
import anyio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from inference.cv_model import detect
from .spot_logic import get_spot_states, detect_vacancies, SPOTS
from .db import engine, Base

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
    anyio.from_thread.run(manager.broadcast, json.dumps(event))



def frame_generator():
    cap = cv2.VideoCapture(0)
    prev_states = {spot_id: False for spot_id in SPOTS}
    # Only consider these COCO classes as vehicles
    vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"}

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Draw ROI outlines
            for spot_id, (sx, sy, sw, sh) in SPOTS.items():
                x1, y1 = int(sx), int(sy)
                x2, y2 = int(sx + sw), int(sy + sh)
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
                cv2.putText(frame, f"Spot {spot_id}", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

            # Perform detection
            detections = detect(frame)
            # Filter to only vehicle detections for state logic
            vehicle_detections = []
            for det in detections:
                boxes = det.boxes.xyxy.tolist()
                classes = det.boxes.cls.tolist()
                # Build new Result-like object with only vehicle boxes
                filtered_indices = [i for i, cls in enumerate(classes) if det.names[cls] in vehicle_classes]
                if not filtered_indices:
                    continue
                # Create a lightweight namespace for filtered dets
                class FilteredDet:
                    def __init__(self, boxes, cls_indices, names):
                        self.boxes = type("B", (), {"xyxy": boxes, "cls": cls_indices})
                        self.names = names
                filt_boxes = [boxes[i] for i in filtered_indices]
                filt_cls = [classes[i] for i in filtered_indices]
                vehicle_detections.append(FilteredDet(filt_boxes, filt_cls, det.names))

            # Update spot states and detect vacancies using only vehicles
            curr_states = get_spot_states(vehicle_detections)
            detect_vacancies(prev_states, curr_states)
            prev_states = curr_states

            # Draw only vehicle detections
            for det in vehicle_detections:
                boxes = det.boxes.xyxy
                cls_list = det.boxes.cls
                for box, cls in zip(boxes, cls_list):
                    x1, y1, x2, y2 = map(int, box)
                    label = det.names[cls]
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(frame, label, (x1, y1 - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

            # Encode as JPEG
            success, jpeg = cv2.imencode('.jpg', frame)
            if not success:
                continue
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
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