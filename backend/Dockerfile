FROM python:3.12-slim

WORKDIR /app

# 1. Create the /app/app directory where Python modules will reside
RUN mkdir -p /app/app

# 2. Copy requirements.txt into /app/app and install dependencies
COPY backend/app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/app/requirements.txt

# 3. Copy the entire content of your local backend/app directory
#    into the /app/app directory in the image.
#    This includes main.py, db.py, spot_logic.py, __init__.py, etc.
COPY backend/app/ /app/app/

# 4. Copy other necessary top-level assets into /app (NOT /app/app)
#    if they are referenced by code assuming they are in /app (e.g., model paths)
COPY backend/inference /app/inference 
COPY yolov8n.pt /app/yolov8n.pt
COPY backend/videos/test_video.mov /app/videos/test_video.mov
COPY backend/secrets/firebase-sa.json /app/secrets/firebase-sa.json


# 5. Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8000

# CMD: No --reload for testing stability.
# Uvicorn targets app.main:app. Python will look for package 'app' (which is /app/app now)
# and module 'main' inside it.
# The WORKDIR is /app. Python's sys.path will include /app by default.
# So, 'import app.main' should find /app/app/main.py.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "debug", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets"]
