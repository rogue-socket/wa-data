from sqlalchemy import Boolean, Column, Float, Integer, String, Text

from .database import Base


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    text = Column(String, nullable=False)
    normalized_text = Column(Text, nullable=True)
    sender = Column(String, nullable=False)
    group_id = Column(String, nullable=False)
    group_name = Column(String, nullable=True)
    wa_message_id = Column(String, nullable=True, index=True)
    timestamp = Column(Integer, nullable=False)
    has_url = Column(Boolean, nullable=False, default=False)
    has_mention = Column(Boolean, nullable=False, default=False)
    has_hashtag = Column(Boolean, nullable=False, default=False)
    token_count = Column(Integer, nullable=False, default=0)
    language = Column(String, nullable=False, default="unknown")
    metadata_json = Column(Text, nullable=True)
    duplicate_group_key = Column(String, nullable=True, index=True)
    similarity_to_canonical = Column(Float, nullable=False, default=1.0)
    duplicate_count = Column(Integer, nullable=False, default=1)
    reaction_score = Column(Float, nullable=False, default=0.0)
    rank_score = Column(Float, nullable=False, default=0.0)


class OutgoingMessage(Base):
    __tablename__ = "outgoing_messages"

    id = Column(Integer, primary_key=True, index=True)
    target_group_id = Column(String, nullable=False, index=True)
    target_group_name = Column(String, nullable=True)
    text = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)
    error_message = Column(Text, nullable=True)
    wa_message_id = Column(String, nullable=True)
    created_at = Column(Integer, nullable=False, index=True)
    sent_at = Column(Integer, nullable=True)


class ReactionEvent(Base):
    __tablename__ = "reaction_events"

    id = Column(Integer, primary_key=True, index=True)
    wa_message_id = Column(String, nullable=False, index=True)
    reactor = Column(String, nullable=False, index=True)
    emoji = Column(String, nullable=False)
    event_type = Column(String, nullable=False, default="add")
    group_id = Column(String, nullable=True, index=True)
    group_name = Column(String, nullable=True)
    timestamp = Column(Integer, nullable=False, index=True)
