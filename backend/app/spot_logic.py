import json
from pathlib import Path
from typing import Dict, Tuple
import datetime

# only ever use this one
SPOTS_FILE = Path(__file__).resolve().parents[1] / "spots.json"

def _load_raw():
    return json.loads(SPOTS_FILE.read_text()).get("spots", [])

def _unpack(spot: dict) -> Tuple[float,float,float,float]:
    """
    If spot has 'bbox':[x1,y1,x2,y2], use it.
    Otherwise expect {x,y,w,h} and compute bbox.
    Returns (sx,sy,sw,sh).
    """
    if "bbox" in spot:
        x1,y1,x2,y2 = spot["bbox"]
        return x1, y1, x2 - x1, y2 - y1
    # fallback to React-editor shape
    x, y, w, h = spot["x"], spot["y"], spot["w"], spot["h"]
    return x, y, w, h

# initial load
_raw = _load_raw()
SPOTS: Dict[int, Tuple[float,float,float,float]] = {
    spot["id"]: _unpack(spot)
    for spot in _raw
}

def refresh_spots():
    """
    Re-read spots.json and update the existing SPOTS dict in-place,
    so any code holding a reference to SPOTS sees the new contents.
    """
    from pathlib import Path
    import json

    SPOTS_FILE = Path(__file__).resolve().parents[1] / "spots.json"
    raw = json.loads(SPOTS_FILE.read_text()).get("spots", [])

    # build a new dict of the same shape
    new = {
        spot["id"]: (
            spot["bbox"][0],
            spot["bbox"][1],
            spot["bbox"][2] - spot["bbox"][0],
            spot["bbox"][3] - spot["bbox"][1],
        )
        for spot in raw
    }

    # mutate the existing SPOTS dict in-place
    SPOTS.clear()
    SPOTS.update(new)

def get_spot_states(detections) -> Dict[int, bool]:
    states = {sid: False for sid in SPOTS}
    for det in detections:
        raw = det.boxes.xyxy
        boxes = raw.tolist() if hasattr(raw, "tolist") else raw
        for x1,y1,x2,y2 in boxes:
            cx, cy = (x1+x2)/2, (y1+y2)/2
            for sid, (sx, sy, sw, sh) in SPOTS.items():
                if sx <= cx <= sx+sw and sy <= cy <= sy+sh:
                    states[sid] = True
    return states

def detect_vacancies(prev_states, curr_states):
    from .db import SessionLocal, VacancyEvent
    # import here to avoid circular
    from .main import broadcast_vacancy

    session = SessionLocal()
    for sid, occ in curr_states.items():
        if prev_states.get(sid, False) and not occ:
            ts = datetime.utcnow()
            evt = VacancyEvent(timestamp=ts, spot_id=sid, camera_id="main")
            session.add(evt); session.commit()
            broadcast_vacancy({"spot_id": sid, "timestamp": ts.isoformat()})
    session.close()
