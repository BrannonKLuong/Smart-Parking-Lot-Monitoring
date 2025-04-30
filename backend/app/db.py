from sqlmodel import create_engine, Session
from typing import Generator
import os

# Try to load DATABASE_URL from environment, fallback to local default
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://parking_user:parking_pass@localhost:5432/parking")
engine = create_engine(DATABASE_URL, echo=True)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session