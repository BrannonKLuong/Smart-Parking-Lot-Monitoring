FROM python:3.12-slim

WORKDIR /app

# 1. Create necessary directories
RUN mkdir -p /app/app
RUN mkdir -p /app/inference
RUN mkdir -p /app/videos
RUN mkdir -p /app/secrets
RUN mkdir -p /app/frontend_build # For serving SPA from backend

# 2. Copy requirements.txt and install dependencies
# Source path 'app/requirements.txt' is relative to the build context (which is ./backend)
# This copies ./backend/app/requirements.txt
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/app/requirements.txt

# 3. Copy your application code (from ./backend/app/ into /app/app/ in image)
# Ensure backend/app/__init__.py exists for 'app.main' to be importable
COPY app/ /app/app/

# 4. Copy other necessary assets (paths are relative to the 'backend' directory context)
COPY inference/ /app/inference/ 
COPY yolov8n.pt /app/yolov8n.pt  
COPY videos/test_video.mov /app/videos/test_video.mov
# secrets/firebase-sa.json is created by the GHA workflow in ./backend/secrets/
# before this Dockerfile is processed.
COPY secrets/firebase-sa.json /app/secrets/firebase-sa.json 

# 5. Copy prepared frontend build assets (if GHA workflow copied them to ./backend/frontend_build_source_for_docker)
# This directory 'frontend_build_source_for_docker' must exist in the build context (./backend)
# if this COPY command is to succeed.
COPY frontend_build_source_for_docker /app/frontend_build

# 6. Install system dependencies for OpenCV etc.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8000

# CMD assumes main.py is in /app/app/ and __init__.py makes 'app' a package
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "debug", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets"]
