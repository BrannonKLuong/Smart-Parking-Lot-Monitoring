FROM python:3.12-slim

WORKDIR /app

# 1. Create the /app/app directory where Python modules will reside
RUN mkdir -p /app/app

# 2. Copy requirements.txt into /app/app and install dependencies
# Path is now relative to './backend' context
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/app/requirements.txt

# 3. Copy the entire content of your local app directory (which is backend/app locally)
#    into the /app/app directory in the image.
COPY app/ /app/app/

# 4. Copy other necessary assets from the backend directory context
COPY inference /app/inference  
                               
# Assuming yolov8n.pt is at the same level as your backend/Dockerfile (i.e., in the backend/ folder)
COPY yolov8n.pt /app/yolov8n.pt

COPY videos/test_video.mov /app/videos/test_video.mov
COPY secrets/firebase-sa.json /app/secrets/firebase-sa.json


# 5. Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "debug", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets"]