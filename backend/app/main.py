# backend/app/main.py (V11.26 - Manual DB Session for GET & POST)
import json 
import logging
from fastapi import FastAPI, Request, HTTPException, Depends, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, ValidationError 

# Database imports
from .db import SessionLocal, init_db, engine as db_engine, ParkingSpotConfig 
from sqlmodel import Session, select # Session for 'with Session(engine)'
from sqlalchemy import text 
from sqlalchemy.engine.row import Row 

# Local module imports
from . import spot_logic 
from datetime import datetime

import asyncio 
import time 

print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
print(">>>> LOADING main.py (V11.26 - MANUAL DB SESSION FOR GET & POST) <<<<")
print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

logging.basicConfig(level=logging.DEBUG) 
logger = logging.getLogger(__name__)

app = FastAPI(debug=True) 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Models ---
class SpotConfigIn(BaseModel): 
    id: str; x: int; y: int; w: int; h: int
class SpotsUpdateRequest(BaseModel): 
    spots: List[SpotConfigIn]


DB_INIT_MAX_RETRIES = 15 
DB_INIT_RETRY_DELAY = 5

# --- Global States ---
previous_spot_states_global: Dict[str, bool] = {} 
empty_start: Dict[str, Any] = {} 
notified: Dict[str, bool] = {} 
event_queue: Optional[asyncio.Queue] = None

def enqueue_event(event_data: Dict[str, Any]): 
    global event_queue
    if event_queue:
        try: event_queue.put_nowait(event_data)
        except asyncio.QueueFull: logger.warning("Event queue full.")
    else: logger.warning("Event queue not init.")


@app.on_event("startup")
async def startup_event_v11_26(): 
    global event_queue, previous_spot_states_global, empty_start, notified
    logger.info("V11.26: Startup event - Attempting Robust Database Initialization...")
    db_initialized_successfully = False
    for attempt in range(DB_INIT_MAX_RETRIES):
        try:
            logger.info(f"V11.26: DB init attempt {attempt + 1}/{DB_INIT_MAX_RETRIES}...")
            with db_engine.connect() as conn_test: 
                 conn_test.execute(select(1)) 
                 logger.info("V11.26: DB engine connection test successful.")
            init_db() 
            logger.info("V11.26: init_db() called. Tables should be created.")
            with Session(db_engine) as session_verify: 
                session_verify.exec(select(ParkingSpotConfig).limit(1)).first() 
                logger.info("V11.26: Verified ParkingSpotConfig table exists after init_db.")
            db_initialized_successfully = True
            logger.info("V11.26: Database initialization successful.")
            break
        except Exception as e:
            logger.error(f"V11.26: Error during DB init attempt {attempt + 1}: {e}", exc_info=False)
            if attempt < DB_INIT_MAX_RETRIES - 1:
                logger.info(f"V11.26: Retrying DB init in {DB_INIT_RETRY_DELAY} seconds...")
                await asyncio.sleep(DB_INIT_RETRY_DELAY)
            else:
                logger.error("V11.26: Max DB init retries reached. DB might not be initialized.")
    
    if db_initialized_successfully:
        try:
            spot_logic.refresh_spots() 
            logger.info(f"V11.26: Spots loaded by spot_logic on startup: {len(spot_logic.SPOTS)}")
            for spot_id_str_key in spot_logic.SPOTS.keys(): 
                s_id = str(spot_id_str_key); previous_spot_states_global.setdefault(s_id, False) 
                empty_start.setdefault(s_id, datetime.utcnow()); notified.setdefault(s_id, False)
            logger.info(f"V11.26: Global spot states initialized for {len(previous_spot_states_global)} spots.")
        except Exception as e_sl: 
            logger.error(f"V11.26: Error initializing spot_logic or global states after DB init: {e_sl}", exc_info=True)
    else: 
        logger.critical("V11.26: DATABASE FAILED TO INITIALIZE PROPERLY.")

    logger.info("V11.26: Registered routes:")
    from fastapi.routing import APIRoute 
    for route in app.routes:
        if isinstance(route, APIRoute): logger.info(f"V11.26 Route: {route.methods} {route.path} (Name: {route.name})")
    event_queue = asyncio.Queue(200); logger.info("V11.26: Event queue initialized.")
    logger.info("V11.26: Application startup sequence complete.")


@app.get("/")
async def root_health_check_v11_26(): 
    logger.info("V11.26: Root path / accessed.")
    return {"message": "Smart Parking API (V11.26 - Manual DB Session for GET & POST) is running"}

@app.post("/api/nuke_test_save") 
async def nuke_save_route_final_logic_manual_session(payload: SpotsUpdateRequest): 
    logger.info(">>>> V11.26: /api/nuke_test_save (MANUAL DB SESSION) EXECUTING NOW! <<<<")
    logger.info(f"V11.26: Validated payload: {payload.model_dump_json(indent=2)}")
    default_camera_id = "default_camera"
    with Session(db_engine) as db: 
        try:
            statement_existing = select(ParkingSpotConfig).where(ParkingSpotConfig.camera_id == default_camera_id)
            existing_spot_configs: List[ParkingSpotConfig] = db.exec(statement_existing).all()
            existing_spots_map: Dict[str, ParkingSpotConfig] = {}
            for config_instance in existing_spot_configs:
                if hasattr(config_instance, 'spot_label'):
                    existing_spots_map[str(config_instance.spot_label)] = config_instance
                else: logger.error(f"V11.26 POST: 'spot_label' not found on {config_instance}")
            logger.info(f"V11.26 POST: Constructed existing_spots_map with {len(existing_spots_map)} entries.")
            
            incoming_spot_labels = set()
            response_spots_data = [] 
            for spot_in_model in payload.spots: 
                label = str(spot_in_model.id) 
                incoming_spot_labels.add(label)
                spot_x, spot_y, spot_w, spot_h = spot_in_model.x, spot_in_model.y, spot_in_model.w, spot_in_model.h
                if label in existing_spots_map:
                    config_to_update = existing_spots_map[label]
                    config_to_update.x_coord, config_to_update.y_coord = spot_x, spot_y
                    config_to_update.width, config_to_update.height = spot_w, spot_h
                    config_to_update.updated_at = datetime.utcnow() 
                    db.add(config_to_update)
                else:
                    new_config = ParkingSpotConfig(spot_label=label, camera_id=default_camera_id, x_coord=spot_x, y_coord=spot_y, width=spot_w, height=spot_h)
                    db.add(new_config)
                response_spots_data.append(spot_in_model.model_dump())
            for existing_label_in_db, config_to_delete in existing_spots_map.items():
                if existing_label_in_db not in incoming_spot_labels: db.delete(config_to_delete)
            db.commit()
            logger.info("V11.26 POST: Spot configuration saved successfully.")
            spot_logic.refresh_spots() # Refresh spot_logic.SPOTS after DB changes
            
            current_db_spot_ids = set(spot_logic.SPOTS.keys())
            for sid_key in list(previous_spot_states_global.keys()): 
                if str(sid_key) not in current_db_spot_ids: 
                    previous_spot_states_global.pop(str(sid_key),None); empty_start.pop(str(sid_key),None); notified.pop(str(sid_key),None)
            for sid_key in current_db_spot_ids: 
                s_id = str(sid_key)
                if s_id not in previous_spot_states_global: 
                     previous_spot_states_global[s_id] = False 
                     empty_start[s_id] = datetime.utcnow()
                     notified[s_id] = False
            
            if event_queue: enqueue_event({"type": "spots_config_updated", "data": {"spots": response_spots_data}})
            return {"message": "V11.26 /api/nuke_test_save: Spots saved to DB successfully!", "spots": response_spots_data}
        except ValidationError as ve: 
            logger.error(f"V11.26 /api/nuke_test_save Pydantic Error: {ve.errors()}", exc_info=True)
            raise HTTPException(status_code=422, detail=ve.errors())
        except Exception as e: 
            db.rollback(); logger.error(f"V11.26 /api/nuke_test_save internal error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/api/spots")
async def old_spots_path_catcher_v11_26(request: Request): 
    logger.error("--- V11.26: OLD /api/spots POST PATH HIT UNEXPECTEDLY ---")
    return HTTPException(status_code=410, detail="This /api/spots POST path is deprecated; use /api/nuke_test_save")

# GET /api/spots_v10_get (App.js V11 calls this)
# MODIFIED: Uses manual DB session management (indirectly via spot_logic.refresh_spots which uses db_engine)
@app.get("/api/spots_v10_get") 
async def get_spots_v11_26_manual_db_get(): # Removed db: Session = Depends(SessionLocal)
    logger.info("V11.26: GET /api/spots_v10_get (MANUAL DB SESSION VIA SPOT_LOGIC) hit.")
    spots_data = []
    try:
        # spot_logic.refresh_spots() creates its own session with db_engine
        spot_logic.refresh_spots() 
        logger.info(f"V11.26 GET: spot_logic.SPOTS refreshed. Count: {len(spot_logic.SPOTS)}")

        if not spot_logic.SPOTS:
             logger.info("V11.26 GET: No spots in spot_logic.SPOTS. Returning empty list.")
             return {"spots": []}

        for spot_id, coords in spot_logic.SPOTS.items():
            spot_id_str = str(spot_id) 
            if isinstance(coords, tuple) and len(coords) == 4:
                is_available_status = not previous_spot_states_global.get(spot_id_str, False)
                spots_data.append({
                    "id": spot_id_str, "x": coords[0], "y": coords[1], "w": coords[2], "h": coords[3],
                    "is_available": is_available_status 
                })
            else:
                logger.warning(f"V11.26 GET: Malformed coords for spot_id {spot_id_str}: {coords}")
        
        logger.info(f"V11.26 GET: Successfully prepared {len(spots_data)} spots.")
        return {"spots": spots_data}

    except Exception as e: 
        logger.error(f"V11.26 GET: Critical error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching spots: {str(e)}")


@app.get("/webcam_feed")
async def webcam_feed_placeholder_v11_26(): 
    logger.info("V11.26: GET /webcam_feed placeholder hit.")
    return {"message": "Webcam feed placeholder (V11.26)."}

@app.websocket("/ws/video_stream_upload")
async def ws_video_placeholder_v11_26(websocket: WebSocket): 
    await websocket.accept()
    logger.info(f"V11.26 WS Client {websocket.client} connected (placeholder)")
    try:
        while True: 
            data = await websocket.receive_text() 
            logger.debug(f"V11.26 WS Recv: {data[:50]}")
    except WebSocketDisconnect: 
        logger.info(f"V11.26 WS Client {websocket.client} disconnected.")
    except Exception as e:
        logger.error(f"V11.26 WS Error for {websocket.client}: {e}", exc_info=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="debug")
