# Smart Parking Lot Monitoring System
![Smart Parking Lot Demo](assets/smart-parking-lot-demo.gif)
## Project Description

This project is a Smart Parking Lot Monitoring System that utilizes video analysis to detect the occupancy status of parking spots. It is built with a Dockerized architecture, featuring a Python backend for video processing and object detection, and a React frontend for a user-friendly interface to visualize the parking lot status and manage spot configurations. The backend also includes basic functionality for communicating with Android devices via Firebase Cloud Messaging (FCM).

The primary goal is to provide real-time information about parking spot availability and notify users when spots become free.

## Architecture and Technologies

The system employs a microservices-like architecture orchestrated with Docker Compose:

* **Backend (`backend/`):**
    * Built with **Python** using the **FastAPI** framework.
    * Handles video stream processing using **OpenCV**.
    * Performs object detection (vehicles) using **YOLOv8**.
    * Manages parking spot configurations.
    * Communicates with the frontend via WebSockets and REST APIs.
    * Includes an API endpoint to register Android device tokens using the **Firebase Admin SDK** for potential push notifications.
    * Uses **SQLModel** and **PostgreSQL** (via a Docker container) for database persistence (storing vacancy events and device tokens).
* **Frontend (`ui/` and built into `static/`):**
    * Developed with **React**.
    * Displays the video feed from the backend.
    * Visualizes parking spot bounding boxes and their occupancy status.
    * Shows real-time notifications about spot status changes.
    * Provides an editor interface to define, modify, and remove parking spot areas.
* **Database:**
    * **PostgreSQL** running in a Docker container.
    * Stores system data, including vacancy events and registered Android device tokens.
* **RTSP Server (`rtsp-simple-server.yml`):**
    * Uses `aler9/rtsp-simple-server` to potentially handle video streams from IP cameras.
* **Docker:**
    * Used to containerize the backend, database, and RTSP server for easy setup and deployment.

## Features

* Real-time video feed display in the web UI.
* Automatic detection of vehicles and parking spot occupancy using YOLOv8.
* Visual representation of parking spots and their status (occupied/free) on the video feed in the web UI.
* Real-time updates of spot status and free spot count in the web UI.
* Notifications within the web UI for spot status changes (e.g., "Spot X freed").
* Web-based editor to add, remove, and resize parking spot bounding boxes.
* Backend endpoint for Android devices to register FCM tokens.
* Basic Android app emulator functionality showing spot counts and updates (based on user commentary).
* **The system architecture is designed to handle live video streams (RTSP/RTMP), and this capability has been tested in previous iterations. The current default configuration uses a local video file (`test_video.mov`) for faster development and testing cycles.**

## Progress and Current Status

This project is currently in active development. Significant progress has been made on the core functionality:

* **Docker Environment:** Successfully configured and running containers for the backend and database.
* **Video Processing:** The backend successfully processes the video file (`test_video.mov`) and serves a stream to the frontend.
* **Frontend Display:** The React frontend correctly displays the video feed and individual spot tiles with accurate status (occupied/free) and counters. Notifications for spot changes are displayed and clear after a duration.
* **Spot Configuration:** The web editor allows for adding, removing, and resizing parking spots, with changes persisted (though spot ID assignment has a known behavior - see Outstanding Issues).
* **Android Communication:** An API endpoint exists for Android devices to register FCM tokens, and the backend uses the Firebase Admin SDK.
* **Database Integration:** Vacancy events are being recorded in the PostgreSQL database.


## Future Goals

* **Integrate Live Video Source:** Transition the system to primarily process a live video stream (e.g., from an RTSP IP camera) for real-world application, leveraging the existing architectural capability.
* **Android Notifications:** Implement the full logic for sending push notifications to registered Android devices when a parking spot becomes available.
* **Android Video Feed:** Explore methods to display the same video feed processed by the backend within the Android application.
* **Improved Accuracy:** Fine-tune the YOLOv8 model or explore other detection methods to improve accuracy in various lighting and weather conditions.
* **Deployment:** Prepare the application for deployment to a cloud platform.

## Setup and Installation

1.  **Prerequisites:** Ensure you have Docker and Docker Compose installed. You will also need Node.js and npm (or yarn) for the frontend build.
2.  **Clone the Repository:**
    ```bash
    git clone https://github.com/BrannonKLuong/Smart-Parking-Lot-Monitoring
    cd Smart-Parking-Lot-Monitoring
    ```
3.  **Configure Environment Variables:** Create a `.env` file at the root directory and potentially in the `backend/` directory based on your project's needs.
4.  **Configure Firebase:** Obtain a Firebase service account key (`firebase-sa.json`) and place it in a secure location, ideally outside the publicly accessible parts of your repository (as configured in your `docker-compose.yml` secrets volume). Update the `FIREBASE_CRED` environment variable to point to this file.
5.  **Build Frontend:** Navigate to the `ui` directory and build the React application.
    ```bash
    cd ui
    npm install # or yarn install
    npm run build # or yarn build
    ```
6.  **Build and Run with Docker Compose:** Navigate back to the root directory and run Docker Compose.
    ```bash
    cd ..
    docker-compose up --build
    ```
7.  **Access the Web UI:** Once the containers are running, the frontend should be accessible at `http://localhost:8000`.


## Usage

1.  **Start Backend and Database Containers:**
    Navigate to the root directory of the project and run:
    ```bash
    docker-compose up -d --build db rtsp backend
    ```
    This starts the database, RTSP server, and backend containers in detached mode.
2.  **Activate Backend Python Environment:**
    Navigate to the `backend` directory and activate your Python virtual environment:
    ```bash
    cd backend
    # On Windows PowerShell:
    .\.venv\Scripts\Activate.ps1
    # On macOS/Linux:
    source .venv/bin/activate
    ```
3.  **Run the Backend Application:**
    From within the activated virtual environment in the `backend` directory, run the FastAPI application:
    ```bash
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
    ```
4.  **Start the Frontend Development Server:**
    Open a *new* terminal window, navigate to the `ui` directory, and start the React development server:
    ```bash
    cd ui
    npm start # or yarn start
    ```
5.  **Access the Web UI:**
    Open your web browser and go to:
    ```
    http://localhost:3000
    ```
    You should see the parking lot monitoring interface. The frontend running on port 3000 will communicate with the backend running (either in Docker or directly via uvicorn) on port 8000.
