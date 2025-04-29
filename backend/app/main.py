from fastapi import FastAPI, File, UploadFile
from fastapi.responses import StreamingResponse
from inference.cv_model import detect
from app.video import make_frames
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
        if int(box.cls[0]) != 2:
            continue
        for box in r.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            conf = float(box.conf[0])
            cls = int(box.cls[0])
            detections.append({"bbox": [x1, y1, x2, y2], "conf": conf, "class": cls})
    return {"count": len(detections), "detections": detections}

@app.get('/video_feed')
async def video_feed():
    async def streamer():
        for frame in make_frames('sample_videos/sample.mp4'):
            results = detect(frame)
            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            ret, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    return StreamingResponse(streamer(), media_type='multipart/x-mixed-replace; boundary=frame')

@app.get('/webcam_feed')

async def webcam_feed():
    async def gen():
        for frame in make_frames(0):
            results = detect(frame)
            for r in results:
                for box in r.boxes:
                    if int(box.cls[0]) != 2:
                        continue
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    conf = f"{float(box.conf[0]):.2f}"
                    cv2.putText(frame, conf, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 255, 0), 2)
            ret, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    return StreamingResponse(gen(), media_type='multipart/x-mixed-replace; boundary=frame')