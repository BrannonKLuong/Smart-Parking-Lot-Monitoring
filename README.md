# Smart Parking Lot Monitoring System

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

* Real-time video feed display in the web UI (currently from a video file for development speed).
* Automatic detection of vehicles and parking spot occupancy using YOLOv8.
* Visual representation of parking spots and their status (occupied/free) on the video feed in the web UI.
* Real-time updates of spot status and free spot count in the web UI.
* Notifications within the web UI for spot status changes (e.g., "Spot X freed").
* Web-based editor to add, remove, and resize parking spot bounding boxes.
* Backend endpoint for Android devices to register FCM tokens.
* Basic Android app emulator functionality showing spot counts and updates (based on user commentary).
* **Capability for handling live video streams (tested in previous versions).**

## Progress and Current Status

This project is currently in active development. Significant progress has been made on the core functionality:

* **Docker Environment:** Successfully configured and running containers for the backend and database.
* **Video Processing:** The backend successfully processes a video file (`test_video.mov`) and serves a stream to the frontend.
* **Frontend Display:** The React frontend correctly displays the video feed and individual spot tiles with accurate status (occupied/free) and counters. Notifications for spot changes are displayed and clear after a duration.
* **Spot Configuration:** The web editor allows for adding, removing, and resizing parking spots, with changes persisted (though spot ID assignment has a known behavior - see Outstanding Issues).
* **Android Communication:** An API endpoint exists for Android devices to register FCM tokens, and the backend uses the Firebase Admin SDK.
* **Database Integration:** Vacancy events are being recorded in the PostgreSQL database.

## Outstanding Issues

* **Spot ID Assignment:** When adding a new spot after removing a previous one, the new spot receives the next sequential ID instead of reusing the lowest available ID. This is a known behavior that is currently accepted for the project's scope.

## Known Potential Issues

* **Backend Instability:** The backend logs occasionally show `Error in video_processor: 3` (OpenCV errors), causing the video processing thread to restart. While the UI remains functional due to the thread management, this indicates an underlying instability that needs further investigation and resolution for robust, long-term operation.

## Future Goals

* **Android Notifications:** Implement the full logic for sending push notifications to registered Android devices when a parking spot becomes available.
* **Android Video Feed:** Explore methods to display the same video feed processed by the backend within the Android application.
* **Backend Stability:** Diagnose and fix the recurring OpenCV errors in the video processing thread to improve system reliability.
* **Live Video Source:** Transition back to processing a live video stream (e.g., RTSP from an IP camera) for real-world application.
* **Improved Accuracy:** Fine-tune the YOLOv8 model or explore other detection methods to improve accuracy in various lighting and weather conditions.
* **Deployment:** Prepare the application for deployment to a cloud platform.
* **User Authentication:** Implement user authentication for the web UI and potentially for the Android app.

## Setup and Installation

**(Note: Detailed setup instructions would typically go here, including prerequisites like Docker and how to clone the repository and run `docker-compose up`. This is a high-level overview.)**

1.  **Prerequisites:** Ensure you have Docker and Docker Compose installed.
2.  **Clone the Repository:**
    ```bash
    git clone <repository_url>
    cd <repository_name>
    ```
3.  **Configure Firebase:** Obtain a Firebase service account key and place it in the `./secrets` directory as `firebase-sa.json`. Update the `FIREBASE_CRED` environment variable in `backend/.env` and `docker-compose.yml` if you use a different path.
4.  **Build Frontend:** Navigate to the `ui` directory and build the React application.
    ```bash
    cd ui
    npm install # or yarn install
    npm run build # or yarn build
    ```
5.  **Build and Run with Docker Compose:** Navigate back to the root directory and run Docker Compose.
    ```bash
    cd ..
    docker-compose up --build
    ```
6.  **Access the Web UI:** Once the containers are running, the frontend should be accessible at `http://localhost:8000`.
