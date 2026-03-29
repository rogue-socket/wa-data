from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import Message

Base.metadata.create_all(bind=engine)


def ensure_messages_schema() -> None:
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(messages)")).fetchall()
        column_names = {row[1] for row in columns}
        if columns and "group_name" not in column_names:
            connection.execute(text("ALTER TABLE messages ADD COLUMN group_name VARCHAR"))


ensure_messages_schema()

app = FastAPI()

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class MessageIn(BaseModel):
    text: str
    sender: str
    group_id: str
    group_name: Optional[str] = None
    timestamp: int


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/messages")
def list_messages(db: Session = Depends(get_db)):
    rows = db.query(Message).order_by(Message.id.desc()).all()

    return [
        {
            "id": row.id,
            "text": row.text,
            "sender": row.sender,
            "group_id": row.group_id,
            "group_name": row.group_name,
            "timestamp": row.timestamp,
        }
        for row in rows
    ]


@app.post("/ingest")
def ingest(message: MessageIn, db: Session = Depends(get_db)):
    db_message = Message(
        text=message.text,
        sender=message.sender,
        group_id=message.group_id,
        group_name=message.group_name,
        timestamp=message.timestamp,
    )
    db.add(db_message)
    db.commit()

    return {"status": "ok"}
