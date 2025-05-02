import json
import datetime
from pathlib import Path
from typing import Dict, List

from inference.cv_model import detect
from .db import SessionLocal, VacancyEvent

# Load spot definitions
SPOTS_FILE = Path(__file__).parent.parent / "spots.json"
with open(SPOTS_FILE) as f:
    data = json.load(f)
SPOTS = {spot["id"]: tuple(spot["bbox"]) for spot in data.get("spots", [])}


def get_spot_states(detections) -> Dict[int, bool]:
    """
    Given YOLO detections, return a dict mapping spot_id to occupied (True/False).
    Handles both tensor/ndarray and plain-list representations of boxes.
    """
    states = {spot_id: False for spot_id in SPOTS}
    for det in detections:
        # support both numpy/tensor and list inputs for xyxy
        raw_boxes = det.boxes.xyxy
        if hasattr(raw_boxes, "tolist"):
            boxes = raw_boxes.tolist()
        else:
            boxes = raw_boxes  # already a list of [x1,y1,x2,y2]
        for x1, y1, x2, y2 in boxes:
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            for spot_id, (sx, sy, sw, sh) in SPOTS.items():
                if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
                    states[spot_id] = True
    return states

def detect_vacancies(prev_states: Dict[int, bool], curr_states: Dict[int, bool]):
    """
    Compare previous and current spot states and generate vacancy events.
    """
    session = SessionLocal()
    for spot_id, occupied in curr_states.items():
        was_occupied = prev_states.get(spot_id, False)
        if was_occupied and not occupied:
            ts = datetime.datetime.utcnow()
            # Persist via ORM
            event = VacancyEvent(timestamp=ts, spot_id=spot_id, camera_id="main")
            session.add(event)
            session.commit()
            # Broadcast vacancy event
            from .main import broadcast_vacancy
            broadcast_vacancy({"spot_id": spot_id, "timestamp": ts.isoformat()})
    session.close()
