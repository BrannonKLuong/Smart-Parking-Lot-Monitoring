FROM python:3.12-slim

WORKDIR /app

# 1. Create the /app/app directory where Python modules will reside
RUN mkdir -p /app/app
RUN mkdir -p /app/inference
RUN mkdir -p /app/videos
RUN mkdir -p /app/secrets
# Optional: Create directory for frontend build if serving from backend
RUN mkdir -p /app/frontend_build

# 2. Copy requirements.txt and install dependencies
# Assuming backend/app/requirements.txt is the correct one for the app
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/app/requirements.txt

# 3. Copy your application code
COPY app/ /app/app/

# 4. Copy other necessary assets (paths are relative to the 'backend' directory context)
COPY inference/ /app/inference/ 
COPY yolov8n.pt /app/yolov8n.pt  
COPY videos/test_video.mov /app/videos/test_video.mov
COPY secrets/firebase-sa.json /app/secrets/firebase-sa.json 
# --- ADD THIS SECTION IF SERVING UI FROM BACKEND ---
# Assuming your React build output is in the project_root/static/ directory
# The Docker build context is ./backend, so to get to project_root/static, we use ../static
# Note: Docker COPY from parent directories (../) can be tricky or disallowed depending on Docker version/daemon.
# A safer approach is to ensure your GitHub Action prepares all needed files *within* the build context.
#
# If your GitHub Action workflow copies the `project_root/static` folder into `./backend/frontend_build_temp`
# before running `docker build`, then you would do:
# COPY frontend_build_temp /app/frontend_build
#
# For now, I'll assume you'll handle bringing the static files into the `backend` context if needed.
# If your `main.py` expects UI files in `/app/frontend_build`, you need a COPY for that.
# Example if you copied your UI build to `backend/frontend_build_source` before docker build:
# COPY frontend_build_source /app/frontend_build
# --- END OF UI SECTION ---


# 5. Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "debug", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets"]