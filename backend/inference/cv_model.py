from ultralytics import YOLO
import cv2

model = YOLO('yolov8n.pt')

def detect(frame):
    """
    Performs object detection on a single frame.

    Args:
        frame: A numpy array representing the image frame.

    Returns:
        A list of detection results.
    """

    results = model.predict(frame, verbose=False)


    return results

if __name__ == '__main__':
    print("Running example detection...")
    dummy_frame = cv2.imread("path/to/a/test_image.jpg") 
    if dummy_frame is not None:
        detections = detect(dummy_frame)
        print(f"Detected {len(detections[0].boxes)} objects.") 
    else:
        print("Could not load dummy image.")

