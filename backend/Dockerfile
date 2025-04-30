FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN apt-get update && apt-get install -y libgl1 && rm -rf /var/lib/apt/lists/*
RUN apt-get update \
    && apt-get install -y \
       libglib2.0-0 \
       libgl1 \
    && rm -rf /var/lib/apt/lists/*

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]