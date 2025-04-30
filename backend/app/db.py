from sqlmodel import create_engine, Session
from typing import Generator

DATABASE_URL = "postgresql://parking_user:parking_pass@db:5432/parking"
engine = create_engine(DATABASE_URL, echo=True)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session