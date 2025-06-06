# Smart Parking Lot Monitoring System
![Smart Parking Lot Demo](assets/smart-parking-lot-demo.gif)

## Deployment Status: Deployed on AWS
This project has been fully deployed to the AWS cloud, utilizing App Runner, S3, CloudFront, and RDS. The live environment is not maintained 24/7 to manage operational costs, but the system is fully configured for automated deployment via the CI/CD pipeline.

## Project Description

This project is a Smart Parking Lot Monitoring System that utilizes video analysis to detect the occupancy status of parking spots. In the demo, these frames are streamed from the user's local webcam to the backend via HTTP for real-time processing. It is built with a Dockerized architecture and now fully deployed to AWS. Featuring a Python backend for video processing and object detection, and a React frontend for a user-friendly interface to visualize the parking lot status and manage spot configurations. The backend also includes basic functionality for communicating with Android devices via Firebase Cloud Messaging (FCM).

The primary goal is to provide real-time information about parking spot availability and notify users when spots become free.

## Architecture and Technologies

The system is deployed on AWS and can be run locally for development using Docker Compose.

* **Cloud Architecture:**
   * The application is deployed on AWS, with the backend running on App Runner, the frontend hosted on S3 and served via CloudFront, and the database managed by RDS.

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
* **Docker:**
    * Used to containerize the backend and its dependencies for consistent deployment on AWS App Runner and for easy local development setup.

## Features
* **Client-Side Video Streaming:** Utilizes the user's local webcam to capture and stream frames to the backend over HTTP, enabling live analysis without requiring a dedicated camera source.
* Real-time video feed display in the web UI.
* Automatic detection of vehicles and parking spot occupancy using YOLOv8.
* Visual representation of parking spots and their status (occupied/free) on the video feed in the web UI.
* Real-time updates of spot status and free spot count in the web UI.
* Notifications within the web UI for spot status changes (e.g., "Spot X freed").
* Web-based editor to add, remove, and resize parking spot bounding boxes.
* Backend endpoint for Android devices to register FCM tokens.
* Basic Android app emulator functionality showing spot counts and updates (based on user commentary).
* Automated CI/CD Pipeline: The project is integrated with GitHub Actions to automatically build, test (migrations), and deploy any changes to AWS.

## Progress and Current Status

The project has reached a stable, feature-complete state. The core functionalities for video processing, frontend visualization, spot configuration, and database integration have been successfully implemented. The entire application has been deployed and validated on AWS, with an automated CI/CD pipeline in place.


## Future Enhancements

* **Performance Optimization:** Refine the video processing pipeline and App Runner configuration to reduce latency and improve responsiveness.
* **User Authentication:** Implement an authentication system for administrative features like spot editing.
* **Advanced Analytics:** Develop a dashboard to show historical parking data, peak hours, and usage trends.
* **Multi-Camera Support:** Extend the architecture to manage and process feeds from multiple cameras simultaneously.
  
## Local Development Setup

While the project is deployed, you can still run it locally for development and testing.

1.  **Prerequisites**:
    * Docker and Docker Compose
    * Node.js and npm
    * Python 3.12

2.  **Clone the Repository**:
    ```bash
    git clone [https://github.com/BrannonKLuong/Smart-Parking-Lot-Monitoring](https://github.com/BrannonKLuong/Smart-Parking-Lot-Monitoring)
    cd Smart-Parking-Lot-Monitoring
    ```

3.  **Configure Environment**:
    * Create a `.env` file in the root of the project.
    * Populate it with necessary variables like `DB_USER`, `DB_PASSWORD`, and `DB_NAME`.
    * The `DATABASE_URL` for local development should point to the Dockerized Postgres service (e.g., `postgresql://user:password@db:5432/dbname`).
    * Create a `backend/secrets` directory.
    * Place your Firebase service account key (`firebase-sa.json`) inside `backend/secrets`.

4.  **Run with Docker Compose**:
    * From the root directory, build and run all services:
        ```bash
        docker-compose up --build
        ```
    * This command will start the FastAPI backend, the PostgreSQL database, and serve the React frontend build if configured in your `docker-compose.yml`.

5.  **Access the Application**:
    * The web UI will be available at `http://localhost:8000` (or the port you have configured for the backend service).
