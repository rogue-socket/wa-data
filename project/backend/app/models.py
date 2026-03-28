from sqlalchemy import Column, Integer, String

from .database import Base


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    text = Column(String, nullable=False)
    sender = Column(String, nullable=False)
    group_id = Column(String, nullable=False)
    timestamp = Column(Integer, nullable=False)
