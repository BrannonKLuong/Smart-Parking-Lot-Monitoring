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
    __tablename__ = "device_token"
    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    platform: str = Field(default="android")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ParkingSpotConfig(SQLModel, table=True):
    __tablename__ = "parking_spot_config"
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    spot_label: str = Field(index=True)
    camera_id: str = Field(default="default_camera", index = True)

    x_coord: int = Field()
    y_coord: int = Field()
    width: int = Field()
    height: int = Field()

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column_kwargs={"onupdate": datetime.utcnow})

def init_db():
    Base.metadata.create_all(bind=engine)
    SQLModel.metadata.create_all(engine)
    insp = inspect(engine)
    print("Tables now in database:", insp.get_table_names())

if __name__ == "__main__":
    init_db()