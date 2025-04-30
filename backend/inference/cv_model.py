from ultralytics import YOLO

# Load the YOLOv8 model (use lightweight or custom weights as needed)
model = YOLO('yolov8n.pt')

def detect(frame):
    """
    Run inference on a single image/frame.
    Returns a Results object containing .boxes, .conf, .cls attributes.
    """
    results = model(frame)
    return results