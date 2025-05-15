from sqlmodel import SQLModel
from app.db import engine 
from app.schemas import Occupancy 

def init():
    SQLModel.metadata.create_all(engine)

if __name__ == "__main__":
    init()
