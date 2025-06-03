# backend/app/spot_logic.py
import logging
from typing import Dict, Tuple, List, cast, Callable
import datetime

# Import database components
from .db import ParkingSpotConfig, engine as db_engine # SessionLocal not needed here
from sqlmodel import Session as SQLModelSession, select # Use SQLModel Session & select for internal logic

logger = logging.getLogger(__name__)

# SPOTS will be loaded from the database.
# Initialize as empty; main.py's startup will populate it after DB init.
SPOTS: Dict[str, Tuple[int, int, int, int]] = {}

def load_spots_from_db(session: SQLModelSession) -> Dict[str, Tuple[int, int, int, int]]:
    """
    Loads spot configurations from the parking_spot_config table for a given camera_id.
    Uses a SQLModelSession.
    Returns a dictionary in the format {spot_label: (x, y, width, height)}.
    """
    camera_id_to_load = "default_camera"
    loaded_spots: Dict[str, Tuple[int, int, int, int]] = {}
    try:
        statement = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == camera_id_to_load)
        results = session.exec(statement).all() # SQLModel session uses .exec()
        for config in results:
            loaded_spots[config.spot_label] = (
                config.x_coord,
                config.y_coord,
                config.width,
                config.height,
            )
        logger.info(f"[spot_logic] Loaded {len(loaded_spots)} spots from DB for camera '{camera_id_to_load}'.")
    except Exception as e:
        logger.warning(f"[spot_logic] Could not load spots from DB in load_spots_from_db: {e}. This might be normal during initial setup.")
        return {}
    return loaded_spots

def refresh_spots() -> None:
    """
    Re-read spot configurations from the database and update the global SPOTS dict in-place.
    This function creates its own SQLModelSession.
    """
    logger.info("[spot_logic] Attempting to refresh spots from database...")
    try:
        with SQLModelSession(db_engine) as session: # Create and use a SQLModelSession
            new_spots_data = load_spots_from_db(session)
            SPOTS.clear()
            SPOTS.update(new_spots_data)
            logger.info(f"[spot_logic] SPOTS global dict refreshed. Current count: {len(SPOTS)}")
    except Exception as e:
        logger.error(f"[spot_logic] CRITICAL ERROR during refresh_spots: {e}", exc_info=True)

def get_spot_states(detections, # Assuming detections is a list of result objects from Ultralytics
                      vehicle_classes: set = {"car", "truck", "bus", "motorbike", "bicycle"}
                     ) -> Dict[str, bool]:
    """
    Determines the occupancy state of each spot based on detections.
    Detections are assumed to be from a model like Ultralytics YOLO.
    Returns a dictionary {spot_label: is_occupied_boolean}.
    """
    if not SPOTS:
        # logger.warning("[spot_logic] get_spot_states called but SPOTS is empty. Try refreshing spots or checking DB.")
        return {} # Return empty if no spots are configured

    states: Dict[str, bool] = {spot_label: False for spot_label in SPOTS.keys()}

    if not detections:
        return states

    try:
        for det_result in detections: # Process each result object from the list
            if hasattr(det_result, 'boxes') and det_result.boxes is not None and \
               hasattr(det_result.boxes, 'xyxy') and hasattr(det_result.boxes, 'cls') and \
               hasattr(det_result, 'names'):

                box_coords_list = det_result.boxes.xyxy.tolist()
                class_indices = det_result.boxes.cls.tolist()
                class_names_map = det_result.names # This is typically {index: 'name'}

                for i, box_coords in enumerate(box_coords_list):
                    class_idx = int(class_indices[i])
                    detected_class_name = class_names_map.get(class_idx)

                    if detected_class_name and detected_class_name in vehicle_classes:
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
            else:
                logger.debug(f"[spot_logic] Detection result object missing expected attributes (boxes, xyxy, cls, names): {det_result}")

    except AttributeError as e:
        logger.error(f"[spot_logic] Error processing detections in get_spot_states (AttributeError): {e}. Detections structure might be unexpected.", exc_info=True)
    except Exception as e:
        logger.error(f"[spot_logic] Generic error processing detections in get_spot_states: {e}", exc_info=True)
    return states

logger.info("[spot_logic.py] loaded. SPOTS will be populated by main.py's startup event.")