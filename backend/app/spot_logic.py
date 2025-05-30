# backend/app/spot_logic.py
import logging # Added for better logging
from typing import Dict, Tuple, List, cast, Callable 
import datetime

# Import database components
from .db import SessionLocal, ParkingSpotConfig, engine as db_engine 
from sqlmodel import Session, select

logger = logging.getLogger(__name__)

# SPOTS will be loaded from the database.
# Initialize as empty; main.py's startup will populate it after DB init.
SPOTS: Dict[str, Tuple[int, int, int, int]] = {}

def load_spots_from_db(session: Session) -> Dict[str, Tuple[int, int, int, int]]:
    """
    Loads spot configurations from the parking_spot_config table for a given camera_id.
    Returns a dictionary in the format {spot_label: (x, y, width, height)}.
    """
    camera_id_to_load = "default_camera" 
    loaded_spots: Dict[str, Tuple[int, int, int, int]] = {}
    try:
        statement = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == camera_id_to_load)
        results = session.exec(statement).all()
        for config in results:
            loaded_spots[config.spot_label] = (
                config.x_coord,
                config.y_coord,
                config.width,
                config.height,
            )
        logger.info(f"[spot_logic] Loaded {len(loaded_spots)} spots from DB for camera '{camera_id_to_load}'.")
    except Exception as e:
        # This can happen if the table doesn't exist yet (e.g., during initial startup before migrations/init_db)
        logger.warning(f"[spot_logic] Could not load spots from DB in load_spots_from_db: {e}. This might be normal during initial setup.")
        return {} # Return empty if DB query fails
    return loaded_spots

def refresh_spots() -> None:
    """
    Re-read spot configurations from the database and update the global SPOTS dict in-place.
    """
    logger.info("[spot_logic] Attempting to refresh spots from database...")
    try:
        with Session(db_engine) as session: # Use the imported db_engine
            new_spots_data = load_spots_from_db(session)
            SPOTS.clear()
            SPOTS.update(new_spots_data)
            logger.info(f"[spot_logic] SPOTS global dict refreshed. Current count: {len(SPOTS)}")
    except Exception as e:
        logger.error(f"[spot_logic] CRITICAL ERROR during refresh_spots: {e}", exc_info=True)
        # SPOTS might be empty or outdated if this fails.
        # Depending on the error, the DB connection itself might be the issue.

def get_spot_states(detections) -> Dict[str, bool]: # Added type hint for detections
    """
    Determines the occupancy state of each spot based on detections.
    Detections are assumed to be from a model like YOLO.
    Returns a dictionary {spot_label: is_occupied_boolean}.
    """
    # Ensure SPOTS is not empty; if it is, refresh_spots might have failed or DB is empty.
    if not SPOTS:
        logger.warning("[spot_logic] get_spot_states called but SPOTS is empty. Try refreshing spots or checking DB.")
        # Attempt a refresh, though this might also fail if DB is the issue
        # refresh_spots() 
        # if not SPOTS: # If still empty after refresh attempt
        return {} # Return empty states if no spots are configured

    states: Dict[str, bool] = {spot_label: False for spot_label in SPOTS.keys()}

    if not detections: 
        return states

    # Assuming detections is a list of results, and each result has 'boxes'
    # This part needs to align with the actual structure of 'detections' from your cv_model.py
    try:
        for det_result in detections: # Assuming detections is a list of result objects
            if hasattr(det_result, 'boxes') and det_result.boxes is not None:
                # Assuming det_result.boxes.xyxy gives coordinates [x1, y1, x2, y2]
                # And det_result.boxes.cls gives class indices, and det_result.names maps indices to names
                vehicle_classes = {"car", "truck", "bus", "motorbike", "bicycle"} # Define relevant classes
                
                box_coords_list = det_result.boxes.xyxy.tolist()
                class_indices = det_result.boxes.cls.tolist()
                class_names_map = det_result.names

                for i, box_coords in enumerate(box_coords_list):
                    class_idx = int(class_indices[i])
                    if class_idx in class_names_map and class_names_map[class_idx] in vehicle_classes:
                        x1, y1, x2, y2 = box_coords
                        center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
                        
                        for spot_label, spot_definition in SPOTS.items():
                            if not (isinstance(spot_definition, tuple) and len(spot_definition) == 4):
                                logger.warning(f"Malformed spot definition for {spot_label} in SPOTS. Skipping.")
                                continue
                            spot_x, spot_y, spot_w, spot_h = spot_definition
                            if (spot_x <= center_x <= spot_x + spot_w and
                                    spot_y <= center_y <= spot_y + spot_h):
                                states[str(spot_label)] = True 
                                break # Vehicle is in this spot, move to next vehicle
    except AttributeError as e:
        logger.error(f"[spot_logic] Error processing detections in get_spot_states (AttributeError): {e}. Detections structure might be unexpected.", exc_info=True)
    except Exception as e:
        logger.error(f"[spot_logic] Generic error processing detections in get_spot_states: {e}", exc_info=True)
    return states

# Initial call to refresh_spots is removed from module level.
# It will be called by main.py after init_db().
logger.info("[spot_logic.py] loaded. SPOTS will be populated by main.py's startup event.")
