from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field

class Occupancy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    camera_id: str = Field(default="main", index=True)
    vehicle_count: int