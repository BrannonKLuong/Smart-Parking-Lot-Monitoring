import json
import cv2
import matplotlib.pyplot as plt 

img_path = "sample_images/sample.jpg"
frame = cv2.imread(img_path)

with open("sample_images/detections.json") as f:
    data = json.load(f)


for det in data.get('detections', []):
    x1, y1, x2, y2 = map(int, det['bbox'])
    conf = det.get('conf', 0.0)
    # Draw bounding box
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
    # Prepare text label
    label = f"{conf:.2f}"
    # Calculate text size
    (w, h), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 4.0, 15)
    # Draw background rectangle for text
    cv2.rectangle(frame, (x1, y1 - h - 4), (x1 + w, y1), (0, 255, 0), -1)
    # Put text above box
    cv2.putText(frame, label, (x1, y1 - 2), cv2.FONT_HERSHEY_SIMPLEX, 4.0, (0, 0, 0), 10)

# Convert for Matplotlib
rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

# Display
plt.imshow(rgb)
plt.axis('off')
plt.show()