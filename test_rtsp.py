import os, cv2, time

RTSP_URL = os.getenv("VIDEO_SOURCE", "rtsp://127.0.0.1:8554/stream")
print("RUNNING with", RTSP_URL)

# Open with FFMPEG backend for RTSP
cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
print("isOpened?", cap.isOpened())
if not cap.isOpened():
    print("[ERROR] Cannot open RTSP stream")
    exit(1)

# Give the stream time to start
time.sleep(1.0)

# Attempt to grab & retrieve until we get a real frame or timeout
start_grab = time.time()
first_ret = False
while time.time() - start_grab < 5.0:  # wait up to 5 seconds
    if not cap.grab():
        time.sleep(0.1)
        continue
    first_ret, frame = cap.retrieve()
    if first_ret:
        break

print("first frame ret:", first_ret)
if not first_ret:
    print("[ERROR] First frame still not available after 5s.")
    cap.release()
    exit(1)

# Now read the rest 99 frames normally
frame_count = 1
start = time.time()
while frame_count < 100:
    if not cap.grab():
        time.sleep(0.05)
        continue
    ret, _ = cap.retrieve()
    if not ret:
        continue
    frame_count += 1

elapsed = time.time() - start
print(f"[DONE] Received {frame_count} frames in {elapsed:.1f}s ({frame_count/elapsed:.1f} FPS)")
cap.release()
