# backend/init_db.py
from sqlmodel import SQLModel
from app.db import engine  # adjust if your engine is elsewhere

from app.schemas import Occupancy  # adjust to match your path

def init():
    SQLModel.metadata.create_all(engine)

if __name__ == "__main__":
    init()
