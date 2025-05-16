from ultralytics import YOLO
import cv2

model = YOLO('/app/yolov8n.pt')

def detect(frame):
    """
    Performs object detection on a single frame.
    Returns: A list of detection results.
    """
    
    results = model.predict(frame, verbose=False)
    return results


