from sqlalchemy import select
from sqlmodel import Session
from firebase_admin import messaging
from .db import engine, DeviceToken

def notify_all(spot_id: int):
    """Send an FCM notification to all registered device tokens."""
    with Session(engine) as sess:
        raw_tokens = sess.exec(select(DeviceToken)).scalars().all()

    tokens = [str(t) for t in raw_tokens if isinstance(t, str) and t.strip()]
    if not tokens:
        return None
    
    message = messaging.MulticastMessage(
        notification = messaging.Notification(
            title = "Parking Spot Available!",
            body = f"Spot {spot_id} is now available.", 
        ),
        tokens=tokens,
    )
    response = messaging.send_multicast(message)
    return response