from fastapi import FastAPI, File, UploadFile
from inference.model import detect
import numpy as np
import cv2

app = FastAPI()

@app.post('/detect')
async def detect_route(file: UploadFile = File(...)):
    data = await file.read()
    nparr = np.frombuffer(data, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    results = detect(frame)
    # Extract boxes and counts
    detections = []
    for r in results:
        for box in r.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            conf = float(box.conf[0])
            cls = int(box.cls[0])
            detections.append({"bbox": [x1, y1, x2, y2], "conf": conf, "class": cls})
    return {"count": len(detections), "detections": detections}