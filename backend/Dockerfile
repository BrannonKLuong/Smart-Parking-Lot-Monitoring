# Use a Python base image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install the Python dependencies
# Using --no-cache-dir to prevent caching issues and --upgrade to ensure latest versions
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Explicitly install anyio again as a troubleshooting step
RUN pip install --no-cache-dir --upgrade anyio==4.9.0

# Copy the rest of the backend code into the container
COPY . .

# Install any necessary system dependencies for OpenCV
# libgl1 is often needed for GUI operations, even in headless mode for some OpenCV functions
# libglib2.0-0 is also needed
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Expose the port the application will run on
EXPOSE 8000

# Command to run the application
# Using uvicorn with reload for development is fine, but for production
# you might remove --reload and use a production-ready server like gunicorn
# Corrected CMD to ensure all elements in the JSON array are strings
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
