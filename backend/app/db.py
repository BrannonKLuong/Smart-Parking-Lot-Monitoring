from sqlalchemy import create_engine, Column, Integer, DateTime, String, inspect
from sqlmodel import SQLModel, Field
from typing import Optional 
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

# Database URL: override via env var or use default
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://parking_user:parking_pass@localhost:5432/parking"
)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Models
class Occupancy(Base):
    __tablename__ = "occupancy"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    camera_id = Column(String, index=True)
    vehicle_count = Column(Integer)

class VacancyEvent(Base):
    __tablename__ = "vacancy_events"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    spot_id = Column(Integer, index=True)
    camera_id = Column(String, index=True)

class DeviceToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    platform: str = Field(default="android")

def init():
    SQLModel.metadata.create_all(engine)
    insp = inspect(engine)
    print("Tables now in database:", insp.get_table_names())

if __name__ == "__main__":
    init()