# backend/inference/cv_model.py
from ultralytics import YOLO # Assuming you are using ultralytics/YOLO
import cv2
# Load your trained model
# Ensure the path 'yolov8n.pt' is correct relative to where the script runs
# or use an absolute path or environment variable if needed
model = YOLO('yolov8n.pt')

def detect(frame):
    """
    Performs object detection on a single frame.

    Args:
        frame: A numpy array representing the image frame.

    Returns:
        A list of detection results.
    """
    # Perform inference, suppressing verbose output
    # The 'verbose=False' argument suppresses the speed/inference logs
    results = model.predict(frame, verbose=False)

    # results is a list of Results objects
    return results

if __name__ == '__main__':
    # Example usage (for testing the model directly)
    # This part might vary depending on your original cv_model.py
    print("Running example detection...")
    # Load a dummy image or capture from a camera
    dummy_frame = cv2.imread("path/to/a/test_image.jpg") # Replace with a valid image path or capture logic
    if dummy_frame is not None:
        detections = detect(dummy_frame)
        print(f"Detected {len(detections[0].boxes)} objects.") # Assuming detections[0] is the main result
    else:
        print("Could not load dummy image.")

