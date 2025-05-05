#!/usr/bin/env python3
import asyncio
import threading
import time

import cv2
import psycopg2
import websockets

# ——— Configuration ———
WS_URI      = "ws://localhost:8000/ws"
STREAM_URL  = "http://localhost:8000/webcam_feed"
DB_CONN_INFO = {
    'host':     'localhost',
    'dbname':   'parking',
    'user':     'parking_user',
    'password': 'parking_pass',
    'port':     5432,
}

# ——— Video‐feed thread ———
def show_feed():
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        print("❌ Failed to open video stream at", STREAM_URL)
        return
    print("▶️  Showing webcam_feed. Press 'q' to exit.")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        cv2.imshow('Parking Feed', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    cap.release()
    cv2.destroyAllWindows()
    print("🛑 Video window closed.")

# ——— WebSocket listener (async) ———
async def ws_listener():
    print(f"🔗 Connecting to WebSocket at {WS_URI} …")
    try:
        async with websockets.connect(WS_URI) as ws:
            print("✅ WebSocket connected, listening for vacancy events …")
            while True:
                msg = await ws.recv()
                print("📡 Vacancy event:", msg)
    except Exception as e:
        print("⚠️  WebSocket error:", e)

# ——— DB‐polling thread ———
def db_poller(poll_interval=2):
    print("🗄️  Starting DB poller (vacancy_events)…")
    conn = psycopg2.connect(**DB_CONN_INFO)
    cur = conn.cursor()
    last_id = None
    while True:
        cur.execute("""
            SELECT id, timestamp, spot_id
            FROM vacancy_events
            ORDER BY id DESC
            LIMIT 1;
        """)
        row = cur.fetchone()
        if row and row[0] != last_id:
            last_id = row[0]
            print(f"📝 DB record: id={row[0]}, timestamp={row[1]}, spot_id={row[2]}")
        time.sleep(poll_interval)

# ——— Main: wire it all together ———
def main():
    # 1) Start video‐feed in its own thread
    threading.Thread(target=show_feed, daemon=True).start()

    # 2) Start DB poller in its own thread
    threading.Thread(target=db_poller, daemon=True).start()

    # 3) Run WebSocket listener in asyncio loop (blocks)
    asyncio.run(ws_listener())

if __name__ == "__main__":
    main()
