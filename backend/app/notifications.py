from sqlalchemy import select
from sqlmodel import Session
from firebase_admin import messaging
from .db import engine, DeviceToken

def notify_all(spot_id: int):
    """Send an FCM notification to all registered device tokens."""
