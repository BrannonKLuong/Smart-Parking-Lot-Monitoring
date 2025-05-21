# backend/app/spot_logic.py
import json # Keep for potential future use or debugging
from typing import Dict, Tuple, List, cast, Callable # Added Callable for type hinting
import datetime

# Import database components
from .db import SessionLocal, ParkingSpotConfig, engine as db_engine # Renamed engine to db_engine
from sqlmodel import Session, select

# SPOTS will be loaded from the database.
# The keys will be spot_label (string), and values will be (x, y, width, height)
SPOTS: Dict[str, Tuple[int, int, int, int]] = {}

def load_spots_from_db(session: Session) -> Dict[str, Tuple[int, int, int, int]]:
    """
    Loads spot configurations from the parking_spot_config table for a given camera_id.
    Returns a dictionary in the format {spot_label: (x, y, width, height)}.
    """
    camera_id_to_load = "default_camera" # Assuming a single default camera for now
    
    statement = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == camera_id_to_load)
    results = session.exec(statement).all()
    
    loaded_spots: Dict[str, Tuple[int, int, int, int]] = {}
    for config in results:
        loaded_spots[config.spot_label] = (
            config.x_coord,
            config.y_coord,
            config.width,
            config.height,
        )
    print(f"[spot_logic] Loaded {len(loaded_spots)} spots from DB for camera '{camera_id_to_load}'.")
    return loaded_spots

def refresh_spots() -> None:
    """
    Re-read spot configurations from the database and update the global SPOTS dict in-place.
    """
    print("[spot_logic] Refreshing spots from database...")
    with Session(db_engine) as session:
        new_spots_data = load_spots_from_db(session)
        SPOTS.clear()
        SPOTS.update(new_spots_data)
        # print(f"[spot_logic] SPOTS updated: {SPOTS}") # For debugging

def get_spot_states(detections) -> Dict[str, bool]:
    """
    Determines the occupancy state of each spot based on detections.
    Detections are assumed to be from a model like YOLO.
    Returns a dictionary {spot_label: is_occupied_boolean}.
    """
    states: Dict[str, bool] = {spot_label: False for spot_label in SPOTS.keys()}

    if not detections: # Handle cases with no detections
        return states

    for det in detections:
        if not hasattr(det, 'boxes') or not hasattr(det.boxes, 'xyxy'):
            continue
            
        raw_boxes = det.boxes.xyxy
        boxes = raw_boxes.tolist() if hasattr(raw_boxes, "tolist") else raw_boxes

        for x1, y1, x2, y2 in boxes:
            center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
            
            for spot_label, (spot_x, spot_y, spot_w, spot_h) in SPOTS.items():
                if (spot_x <= center_x <= spot_x + spot_w and
                        spot_y <= center_y <= spot_y + spot_h):
                    states[str(spot_label)] = True # Ensure spot_label is string
                    break 
    return states

# Initial load of spots when the module is imported.
# This ensures SPOTS is populated when the application starts.
try:
    print("[spot_logic] Performing initial load of spots from database...")
    with Session(db_engine) as initial_session:
        initial_spots_data = load_spots_from_db(initial_session)
        SPOTS.update(initial_spots_data)
except Exception as e:
    print(f"[spot_logic] CRITICAL ERROR: Failed to perform initial load of spots from database: {e}")
    print("[spot_logic] SPOTS dictionary will be empty. Spot detection may not function correctly.")


# detect_vacancies now accepts a broadcast_func to decouple it from main.py's broadcast_spot_update
def detect_vacancies(
    prev_states: Dict[str, bool], 
    curr_states: Dict[str, bool],
    broadcast_func: Callable[[dict], None], # Function to call for broadcasting
    notify_func: Callable[[int], dict] # Function to call for FCM notifications (e.g., notify_all)
):
    """
    Compares previous and current spot states to detect new vacancies.
    Logs vacancy events to the database and triggers notifications/broadcasts.
    
    Args:
        prev_states: Dictionary of {spot_label: was_occupied_boolean}
        curr_states: Dictionary of {spot_label: is_occupied_boolean}
        broadcast_func: Function to broadcast spot status updates.
        notify_func: Function to send FCM push notifications.
    """
    from .db import SessionLocal, VacancyEvent # Keep SessionLocal for this specific task

    # VACANCY_DELAY and FCM notification logic is now primarily in main.py's video_processor
    # This function will focus on identifying state changes and logging VacancyEvent.
    # The decision to send FCM and the delay will be handled by the caller (video_processor).

    session = SessionLocal()
    newly_vacant_spots: List[str] = [] # For spots that just became vacant
    newly_occupied_spots: List[str] = [] # For spots that just became occupied

    try:
        for spot_label, is_currently_occupied in curr_states.items():
            spot_label_str = str(spot_label) # Ensure it's a string
            was_previously_occupied = prev_states.get(spot_label_str, False) 

            if was_previously_occupied and not is_currently_occupied: # Spot became vacant
                timestamp = datetime.datetime.utcnow()
                print(f"[spot_logic] Spot {spot_label_str} became vacant at {timestamp}.")
                newly_vacant_spots.append(spot_label_str)
                
                try:
                    spot_id_int = int(spot_label_str)
                    evt = VacancyEvent(timestamp=timestamp, spot_id=spot_id_int, camera_id="default_camera")
                    session.add(evt)
                    session.commit()
                    print(f"[spot_logic] Logged VacancyEvent for spot {spot_label_str} (ID: {spot_id_int}).")
                    
                    # Broadcast immediately that it's free
                    broadcast_func({
                        "spot_id": spot_label_str, 
                        "timestamp": timestamp, # Pass datetime object, main.py will format
                        "status": "free"
                    })

                except ValueError:
                    print(f"[spot_logic] ERROR: Could not convert spot_label '{spot_label_str}' to an integer. "
                          "VacancyEvent not logged for this spot.")
                    session.rollback()
                except Exception as e_db_broadcast:
                    print(f"[spot_logic] ERROR during vacancy DB/broadcast processing for spot {spot_label_str}: {e_db_broadcast}")
                    session.rollback()
            
            elif not was_previously_occupied and is_currently_occupied: # Spot became occupied
                timestamp = datetime.datetime.utcnow()
                print(f"[spot_logic] Spot {spot_label_str} became occupied at {timestamp}.")
                newly_occupied_spots.append(spot_label_str)
                # Broadcast immediately that it's occupied
                broadcast_func({
                    "spot_id": spot_label_str,
                    "timestamp": timestamp, # Pass datetime object
                    "status": "occupied"
                })

    except Exception as e:
        print(f"[spot_logic] ERROR in detect_vacancies main loop: {e}")
        session.rollback()
    finally:
        session.close()
    
    # The decision to send FCM notifications (and the associated delay) will be handled
    # by the video_processor in main.py using the newly_vacant_spots list if needed,
    # or by directly observing current_spot_statuses and empty_start times there.
    # For now, this function focuses on logging to DB and immediate status broadcast.
    return newly_vacant_spots # Return spots that just became vacant

