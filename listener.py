import asyncio
import threading
import time
import websockets
import psycopg2
import cv2

# Configuration
WS_URI = "ws://localhost:8000/ws"
DB_CONN_INFO = {
    'host': 'localhost',
    'dbname': 'parking',
    'user': 'parking_user',
    'password': 'password'
}
STREAM_URL = "http://localhost:8000/webcam_feed"


def show_feed():
    """
    Display the MJPEG feed in an OpenCV window.
    Press 'q' to quit.
    """
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        print("Failed to open video stream")
        return
    print("Showing webcam_feed. Press 'q' to exit.")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        cv2.imshow('Parking Feed', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    cap.release()
    cv2.destroyAllWindows()


async def ws_listener():
    """
    Listen for vacancy events over WebSocket and print them.
    """
    print(f"Connecting to WebSocket at {WS_URI}...")
    try:
        async with websockets.connect(WS_URI) as ws:
            print("WebSocket connected, listening for vacancy events...")
            while True:
                msg = await ws.recv()
                print(f"Vacancy event: {msg}")
    except Exception as e:
        print(f"WebSocket error: {e}")


def db_poller(poll_interval=2):
    """
    Poll the vacancy_events table every poll_interval seconds and print new rows.
    """
    conn = psycopg2.connect(**DB_CONN_INFO)
    cur = conn.cursor()
    last_id = None
    print("Starting DB poller...")
    while True:
        cur.execute("SELECT id, timestamp, spot_id FROM vacancy_events ORDER BY id DESC LIMIT 1;")
        row = cur.fetchone()
        if row and row[0] != last_id:
            last_id = row[0]
            print(f"DB record: id={row[0]}, timestamp={row[1]}, spot_id={row[2]}")
        time.sleep(poll_interval)


def main():
    # Start the video feed in a separate thread
    feed_thread = threading.Thread(target=show_feed, daemon=True)
    feed_thread.start()

    # Start DB poller in a separate thread
    db_thread = threading.Thread(target=db_poller, daemon=True)
    db_thread.start()

    # Run WebSocket listener in the asyncio event loop
    asyncio.run(ws_listener())


if __name__ == '__main__':
    main()
