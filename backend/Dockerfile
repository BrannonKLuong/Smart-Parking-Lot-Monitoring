FROM python:3.12-slim

WORKDIR /app

# Use the comprehensive requirements.txt from the app directory
COPY app/requirements.txt . 

RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the rest of the application code
COPY . .

RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Ensure paths for copied files like videos and secrets are correct relative to the build context (backend/)
COPY videos/test_video.mov /app/videos/test_video.mov
COPY secrets/firebase-sa.json /app/secrets/firebase-sa.json
COPY inference /app/inference 
COPY yolov8n.pt /app/yolov8n.pt


EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets", "--root-path", "${APP_ROOT_PATH:-}"]