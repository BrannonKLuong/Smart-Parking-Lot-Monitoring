FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade -r requirements.txt

RUN pip install --no-cache-dir --upgrade anyio==4.9.0

COPY . .

RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY . .

COPY videos/test_video.mov /app/videos/test_video.mov

COPY secrets/firebase-sa.json /app/secrets/firebase-sa.json
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws", "websockets", "--root-path", "${APP_ROOT_PATH:-}"]
