from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import Message

Base.metadata.create_all(bind=engine)

app = FastAPI()


class MessageIn(BaseModel):
    text: str
    sender: str
    group_id: str
    timestamp: int


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/ingest")
def ingest(message: MessageIn, db: Session = Depends(get_db)):
    db_message = Message(
        text=message.text,
        sender=message.sender,
        group_id=message.group_id,
        timestamp=message.timestamp,
    )
    db.add(db_message)
    db.commit()

    return {"status": "ok"}
