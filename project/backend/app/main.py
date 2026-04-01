import hashlib
import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import Message, OutgoingMessage, ReactionEvent

Base.metadata.create_all(bind=engine)


def ensure_messages_schema() -> None:
    with engine.begin() as connection:
        columns = connection.execute(text("PRAGMA table_info(messages)")).fetchall()
        column_names = {row[1] for row in columns}
        required_columns = {
            "group_name": "VARCHAR",
            "normalized_text": "TEXT",
            "wa_message_id": "VARCHAR",
            "has_url": "BOOLEAN NOT NULL DEFAULT 0",
            "has_mention": "BOOLEAN NOT NULL DEFAULT 0",
            "has_hashtag": "BOOLEAN NOT NULL DEFAULT 0",
            "token_count": "INTEGER NOT NULL DEFAULT 0",
            "language": "VARCHAR NOT NULL DEFAULT 'unknown'",
            "metadata_json": "TEXT",
            "duplicate_group_key": "VARCHAR",
            "similarity_to_canonical": "FLOAT NOT NULL DEFAULT 1.0",
            "duplicate_count": "INTEGER NOT NULL DEFAULT 1",
            "reaction_score": "FLOAT NOT NULL DEFAULT 0.0",
            "rank_score": "FLOAT NOT NULL DEFAULT 0.0",
        }

        if columns:
            for name, definition in required_columns.items():
                if name not in column_names:
                    connection.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {definition}"))

        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_group_id ON messages(group_id)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_group_name ON messages(group_name)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_rank_score ON messages(rank_score)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_duplicate_group ON messages(duplicate_group_key)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_wa_message_id ON messages(wa_message_id)")
        )


URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\w+")
HASHTAG_PATTERN = re.compile(r"#\w+")
WHITESPACE_PATTERN = re.compile(r"\s+")
SEARCH_TOKEN_PATTERN = re.compile(r"[\\w@#:/.\\-]+")


def normalize_text(value: str) -> str:
    lowered = (value or "").strip().lower()
    return WHITESPACE_PATTERN.sub(" ", lowered)


def make_duplicate_group_key(group_id: str, normalized_text_value: str) -> str:
    digest = hashlib.sha1(f"{group_id}:{normalized_text_value}".encode("utf-8")).hexdigest()
    return digest


def detect_language(value: str) -> str:
    if not value:
        return "unknown"
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    ratio = ascii_chars / max(1, len(value))
    return "en-like" if ratio >= 0.85 else "unknown"


def compute_rank_score(
    token_count: int,
    has_url: bool,
    has_mention: bool,
    has_hashtag: bool,
    duplicate_count: int,
    reaction_score: float,
    message_timestamp: int,
) -> float:
    now_ts = int(time.time())
    age_hours = max(now_ts - int(message_timestamp or now_ts), 0) / 3600
    recency = max(0.0, 1.0 - min(age_hours / 72.0, 1.0))

    quality = min(max(token_count, 0) / 24.0, 1.0)
    quality += 0.15 if has_url else 0.0
    quality += 0.1 if has_mention else 0.0
    quality += 0.1 if has_hashtag else 0.0

    duplicate_boost = min(max(duplicate_count - 1, 0) * 0.2, 1.5)
    reaction_boost = min(max(reaction_score, 0.0) * 0.15, 1.5)
    final_score = (1.5 * recency) + quality + duplicate_boost + reaction_boost
    return round(final_score, 4)


def extract_metadata(message_text: str, incoming_metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    text_value = message_text or ""
    urls = URL_PATTERN.findall(text_value)
    mentions = MENTION_PATTERN.findall(text_value)
    hashtags = HASHTAG_PATTERN.findall(text_value)
    tokens = [token for token in WHITESPACE_PATTERN.split(text_value.strip()) if token]

    metadata = {
        "urls": urls,
        "mentions": mentions,
        "hashtags": hashtags,
        "token_count": len(tokens),
        "language": detect_language(text_value),
    }

    if incoming_metadata:
        metadata["bot_metadata"] = incoming_metadata

    return metadata


def find_duplicate_group_for_message(
    db: Session,
    group_id: str,
    normalized_text_value: str,
    threshold: float = 0.8,
) -> tuple[str, float]:
    if not normalized_text_value:
        return make_duplicate_group_key(group_id, "empty"), 1.0

    candidates = (
        db.query(Message)
        .filter(Message.group_id == group_id)
        .filter(Message.normalized_text.isnot(None))
        .order_by(Message.id.desc())
        .limit(250)
        .all()
    )

    best_candidate: Optional[Message] = None
    best_similarity = 0.0

    for candidate in candidates:
        candidate_text = candidate.normalized_text or ""
        similarity = SequenceMatcher(None, normalized_text_value, candidate_text).ratio()
        if similarity > best_similarity:
            best_similarity = similarity
            best_candidate = candidate

    if best_candidate and best_similarity >= threshold:
        cluster_key = best_candidate.duplicate_group_key
        if not cluster_key:
            cluster_key = make_duplicate_group_key(group_id, best_candidate.normalized_text or normalized_text_value)
        return cluster_key, round(best_similarity, 4)

    return make_duplicate_group_key(group_id, normalized_text_value), 1.0


def recalculate_cluster_scores(db: Session, group_id: str, duplicate_group_key: str) -> int:
    cluster_rows = (
        db.query(Message)
        .filter(Message.group_id == group_id)
        .filter(Message.duplicate_group_key == duplicate_group_key)
        .all()
    )
    cluster_size = len(cluster_rows)
    if cluster_size == 0:
        return 0

    for row in cluster_rows:
        row.duplicate_count = cluster_size
        row.rank_score = compute_rank_score(
            token_count=row.token_count,
            has_url=bool(row.has_url),
            has_mention=bool(row.has_mention),
            has_hashtag=bool(row.has_hashtag),
            duplicate_count=cluster_size,
            reaction_score=float(row.reaction_score or 0.0),
            message_timestamp=row.timestamp,
        )

    return cluster_size


def load_metadata(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def build_metadata_terms(metadata: dict[str, Any]) -> str:
    terms: list[str] = []

    language = metadata.get("language")
    if isinstance(language, str) and language:
        terms.append(language)

    for key in ("urls", "mentions", "hashtags"):
        values = metadata.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value:
                terms.append(value)

    bot_metadata = metadata.get("bot_metadata")
    if isinstance(bot_metadata, dict):
        bot_type = bot_metadata.get("type")
        if isinstance(bot_type, str) and bot_type:
            terms.append(bot_type)

        mentioned_ids = bot_metadata.get("mentioned_ids", [])
        if isinstance(mentioned_ids, list):
            for mentioned in mentioned_ids:
                if isinstance(mentioned, str) and mentioned:
                    terms.append(mentioned)

    return " ".join(terms)


def sync_message_to_fts(db: Session, row: Message) -> None:
    metadata_terms = build_metadata_terms(load_metadata(row.metadata_json))
    db.execute(
        text(
            """
            INSERT OR REPLACE INTO messages_fts (
                rowid,
                message_id,
                group_id,
                group_name,
                sender,
                text,
                normalized_text,
                metadata_terms
            ) VALUES (
                :rowid,
                :message_id,
                :group_id,
                :group_name,
                :sender,
                :text,
                :normalized_text,
                :metadata_terms
            )
            """
        ),
        {
            "rowid": row.id,
            "message_id": row.id,
            "group_id": row.group_id,
            "group_name": row.group_name or "",
            "sender": row.sender,
            "text": row.text,
            "normalized_text": row.normalized_text or "",
            "metadata_terms": metadata_terms,
        },
    )


def ensure_search_schema() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    message_id UNINDEXED,
                    group_id UNINDEXED,
                    group_name,
                    sender,
                    text,
                    normalized_text,
                    metadata_terms
                )
                """
            )
        )

        # Backfill existing messages so search works for already ingested data.
        connection.execute(
            text(
                """
                INSERT INTO messages_fts (
                    rowid,
                    message_id,
                    group_id,
                    group_name,
                    sender,
                    text,
                    normalized_text,
                    metadata_terms
                )
                SELECT
                    m.id,
                    m.id,
                    m.group_id,
                    COALESCE(m.group_name, ''),
                    m.sender,
                    m.text,
                    COALESCE(m.normalized_text, ''),
                    COALESCE(m.metadata_json, '')
                FROM messages m
                WHERE NOT EXISTS (
                    SELECT 1 FROM messages_fts f
                    WHERE f.rowid = m.id
                )
                """
            )
        )


def build_fts_match_query(raw_query: str) -> str:
    tokens = [token for token in SEARCH_TOKEN_PATTERN.findall((raw_query or "").strip()) if token]
    if not tokens:
        raise HTTPException(status_code=400, detail="query must contain searchable terms")

    limited_tokens = tokens[:20]
    return " AND ".join(f'"{token}"' for token in limited_tokens)


ensure_messages_schema()
ensure_search_schema()

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
    wa_message_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class OutgoingMessageIn(BaseModel):
    target_group_id: str
    text: str
    target_group_name: Optional[str] = None


class OutgoingMessageResultIn(BaseModel):
    status: Literal["sent", "failed"]
    error_message: Optional[str] = None
    wa_message_id: Optional[str] = None
    sent_at: Optional[int] = None


class ReactionIn(BaseModel):
    wa_message_id: str
    reactor: str
    emoji: str
    event_type: Literal["add", "remove"] = "add"
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    timestamp: Optional[int] = None


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
def list_messages(
    group_id: Optional[str] = Query(default=None),
    group_name: Optional[str] = Query(default=None),
    sort_by: Literal["newest", "oldest", "rank", "duplicates"] = Query(default="newest"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Message)

    if group_id:
        query = query.filter(Message.group_id == group_id)

    if group_name:
        query = query.filter(Message.group_name.ilike(f"%{group_name}%"))

    if sort_by == "oldest":
        query = query.order_by(Message.id.asc())
    elif sort_by == "rank":
        query = query.order_by(Message.rank_score.desc(), Message.id.desc())
    elif sort_by == "duplicates":
        query = query.order_by(Message.duplicate_count.desc(), Message.id.desc())
    else:
        query = query.order_by(Message.id.desc())

    rows = query.offset(offset).limit(limit).all()

    return [
        {
            "id": row.id,
            "text": row.text,
            "normalized_text": row.normalized_text,
            "sender": row.sender,
            "group_id": row.group_id,
            "group_name": row.group_name,
            "wa_message_id": row.wa_message_id,
            "timestamp": row.timestamp,
            "metadata": load_metadata(row.metadata_json),
            "duplicate_group_key": row.duplicate_group_key,
            "similarity_to_canonical": row.similarity_to_canonical,
            "duplicate_count": row.duplicate_count,
            "reaction_score": row.reaction_score,
            "rank_score": row.rank_score,
        }
        for row in rows
    ]


@app.get("/messages/merged")
def list_merged_messages(
    group_id: Optional[str] = Query(default=None),
    group_name: Optional[str] = Query(default=None),
    sort_by: Literal["newest", "rank", "duplicates"] = Query(default="duplicates"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Message)
    if group_id:
        query = query.filter(Message.group_id == group_id)
    if group_name:
        query = query.filter(Message.group_name.ilike(f"%{group_name}%"))

    rows = query.order_by(Message.id.desc()).limit(2000).all()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        cluster_key = row.duplicate_group_key or f"single-{row.id}"
        current = grouped.get(cluster_key)
        if not current:
            grouped[cluster_key] = {
                "duplicate_group_key": cluster_key,
                "group_id": row.group_id,
                "group_name": row.group_name,
                "canonical_message_id": row.id,
                "canonical_text": row.text,
                "latest_timestamp": row.timestamp,
                "duplicate_count": row.duplicate_count or 1,
                "rank_score": row.rank_score,
                "message_ids": [row.id],
            }
            continue

        current["message_ids"].append(row.id)
        current["duplicate_count"] = max(current["duplicate_count"], row.duplicate_count or 1)
        if (row.rank_score or 0.0) > (current["rank_score"] or 0.0):
            current["rank_score"] = row.rank_score
            current["canonical_message_id"] = row.id
            current["canonical_text"] = row.text
        current["latest_timestamp"] = max(current["latest_timestamp"], row.timestamp)

    clusters = list(grouped.values())
    if sort_by == "rank":
        clusters.sort(key=lambda item: (item["rank_score"], item["latest_timestamp"]), reverse=True)
    elif sort_by == "newest":
        clusters.sort(key=lambda item: item["latest_timestamp"], reverse=True)
    else:
        clusters.sort(
            key=lambda item: (item["duplicate_count"], item["rank_score"], item["latest_timestamp"]),
            reverse=True,
        )

    paginated = clusters[offset : offset + limit]
    return {
        "total_clusters": len(clusters),
        "offset": offset,
        "limit": limit,
        "items": paginated,
    }


@app.get("/search")
def search_messages(
    q: str = Query(min_length=1),
    group_id: Optional[str] = Query(default=None),
    group_name: Optional[str] = Query(default=None),
    sort_by: Literal["relevance", "newest", "oldest", "rank", "duplicates"] = Query(default="relevance"),
    merged: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    match_query = build_fts_match_query(q)
    group_name_like = f"%{group_name}%" if group_name else None
    where_clause = """
        messages_fts MATCH :match_query
        AND (:group_id IS NULL OR m.group_id = :group_id)
        AND (:group_name_like IS NULL OR m.group_name LIKE :group_name_like)
    """

    params = {
        "match_query": match_query,
        "group_id": group_id,
        "group_name_like": group_name_like,
        "limit": limit,
        "offset": offset,
    }

    if merged:
        cluster_pick_order_map = {
            "relevance": "h.fts_rank ASC, h.id DESC",
            "newest": "h.timestamp DESC, h.id DESC",
            "oldest": "h.timestamp ASC, h.id ASC",
            "rank": "h.rank_score DESC, h.id DESC",
            "duplicates": "h.duplicate_count DESC, h.id DESC",
        }
        cluster_sort_order_map = {
            "relevance": "fts_rank ASC, latest_timestamp DESC",
            "newest": "latest_timestamp DESC",
            "oldest": "latest_timestamp ASC",
            "rank": "rank_score DESC, latest_timestamp DESC",
            "duplicates": "duplicate_count DESC, rank_score DESC, latest_timestamp DESC",
        }

        cluster_pick_order = cluster_pick_order_map.get(sort_by, cluster_pick_order_map["relevance"])
        cluster_sort_order = cluster_sort_order_map.get(sort_by, cluster_sort_order_map["relevance"])

        count_sql = text(
            f"""
            WITH search_hits AS (
                SELECT
                    m.id,
                    COALESCE(m.duplicate_group_key, 'single-' || m.id) AS cluster_key
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE {where_clause}
            )
            SELECT COUNT(DISTINCT cluster_key) AS total
            FROM search_hits
            """
        )

        select_sql = text(
            f"""
            WITH search_hits AS (
                SELECT
                    m.id,
                    m.text,
                    m.normalized_text,
                    m.sender,
                    m.group_id,
                    m.group_name,
                    m.wa_message_id,
                    m.timestamp,
                    m.metadata_json,
                    m.duplicate_group_key,
                    m.similarity_to_canonical,
                    m.duplicate_count,
                    m.reaction_score,
                    m.rank_score,
                    bm25(messages_fts) AS fts_rank,
                    COALESCE(m.duplicate_group_key, 'single-' || m.id) AS cluster_key
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE {where_clause}
            ),
            clustered AS (
                SELECT
                    h.*,
                    MAX(h.timestamp) OVER (PARTITION BY h.cluster_key) AS latest_timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY h.cluster_key
                        ORDER BY {cluster_pick_order}
                    ) AS cluster_row_num
                FROM search_hits h
            )
            SELECT *
            FROM clustered
            WHERE cluster_row_num = 1
            ORDER BY {cluster_sort_order}
            LIMIT :limit OFFSET :offset
            """
        )
    else:
        sort_order_map = {
            "relevance": "fts_rank ASC, m.id DESC",
            "newest": "m.timestamp DESC, m.id DESC",
            "oldest": "m.timestamp ASC, m.id ASC",
            "rank": "m.rank_score DESC, m.id DESC",
            "duplicates": "m.duplicate_count DESC, m.id DESC",
        }
        sort_order = sort_order_map.get(sort_by, sort_order_map["relevance"])

        count_sql = text(
            f"""
            SELECT COUNT(1) AS total
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE {where_clause}
            """
        )

        select_sql = text(
            f"""
            SELECT
                m.id,
                m.text,
                m.normalized_text,
                m.sender,
                m.group_id,
                m.group_name,
                m.wa_message_id,
                m.timestamp,
                m.metadata_json,
                m.duplicate_group_key,
                m.similarity_to_canonical,
                m.duplicate_count,
                m.reaction_score,
                m.rank_score,
                bm25(messages_fts) AS fts_rank,
                m.timestamp AS latest_timestamp
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE {where_clause}
            ORDER BY {sort_order}
            LIMIT :limit OFFSET :offset
            """
        )

    try:
        total = db.execute(count_sql, params).scalar() or 0
        rows = db.execute(select_sql, params).mappings().all()
    except OperationalError as exc:
        raise HTTPException(status_code=400, detail="invalid search query syntax") from exc

    items = [
        {
            "id": row["id"],
            "text": row["text"],
            "normalized_text": row["normalized_text"],
            "sender": row["sender"],
            "group_id": row["group_id"],
            "group_name": row["group_name"],
            "wa_message_id": row["wa_message_id"],
            "timestamp": row["timestamp"],
            "metadata": load_metadata(row["metadata_json"]),
            "duplicate_group_key": row["duplicate_group_key"],
            "similarity_to_canonical": row["similarity_to_canonical"],
            "duplicate_count": row["duplicate_count"],
            "reaction_score": row["reaction_score"],
            "rank_score": row["rank_score"],
            "search_rank": row["fts_rank"],
            "latest_timestamp": row["latest_timestamp"],
        }
        for row in rows
    ]

    return {
        "query": q,
        "sort_by": sort_by,
        "merged": merged,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
    }


@app.post("/ingest")
def ingest(message: MessageIn, db: Session = Depends(get_db)):
    metadata = extract_metadata(message.text, incoming_metadata=message.metadata)
    normalized_text_value = normalize_text(message.text)
    duplicate_group_key, similarity = find_duplicate_group_for_message(
        db,
        group_id=message.group_id,
        normalized_text_value=normalized_text_value,
    )

    db_message = Message(
        text=message.text,
        normalized_text=normalized_text_value,
        sender=message.sender,
        group_id=message.group_id,
        group_name=message.group_name,
        wa_message_id=message.wa_message_id,
        timestamp=message.timestamp,
        has_url=len(metadata["urls"]) > 0,
        has_mention=len(metadata["mentions"]) > 0,
        has_hashtag=len(metadata["hashtags"]) > 0,
        token_count=metadata["token_count"],
        language=metadata["language"],
        metadata_json=json.dumps(metadata, ensure_ascii=True),
        duplicate_group_key=duplicate_group_key,
        similarity_to_canonical=similarity,
        duplicate_count=1,
        reaction_score=0.0,
        rank_score=0.0,
    )
    db.add(db_message)
    db.flush()
    sync_message_to_fts(db, db_message)

    duplicate_count = recalculate_cluster_scores(
        db,
        group_id=message.group_id,
        duplicate_group_key=duplicate_group_key,
    )
    db.commit()

    db.refresh(db_message)

    return {
        "status": "ok",
        "message_id": db_message.id,
        "duplicate_group_key": duplicate_group_key,
        "duplicate_count": duplicate_count,
        "rank_score": db_message.rank_score,
    }


@app.post("/bot/send")
def enqueue_outgoing_message(payload: OutgoingMessageIn, db: Session = Depends(get_db)):
    row = OutgoingMessage(
        target_group_id=payload.target_group_id,
        target_group_name=payload.target_group_name,
        text=payload.text,
        status="pending",
        created_at=int(time.time()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "status": "queued",
        "id": row.id,
        "target_group_id": row.target_group_id,
    }


@app.get("/bot/commands/next")
def get_next_outgoing_message(db: Session = Depends(get_db)):
    row = (
        db.query(OutgoingMessage)
        .filter(OutgoingMessage.status == "pending")
        .order_by(OutgoingMessage.id.asc())
        .first()
    )
    if not row:
        return {"status": "empty"}

    row.status = "processing"
    db.commit()
    return {
        "status": "ok",
        "command": {
            "id": row.id,
            "target_group_id": row.target_group_id,
            "target_group_name": row.target_group_name,
            "text": row.text,
        },
    }


@app.post("/bot/commands/{command_id}/result")
def complete_outgoing_message(
    command_id: int,
    payload: OutgoingMessageResultIn,
    db: Session = Depends(get_db),
):
    row = db.query(OutgoingMessage).filter(OutgoingMessage.id == command_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="command not found")

    row.status = payload.status
    row.error_message = payload.error_message
    row.wa_message_id = payload.wa_message_id
    row.sent_at = payload.sent_at or int(time.time())
    db.commit()

    return {"status": "ok", "command_id": command_id, "final_status": row.status}


@app.post("/reactions/ingest")
def ingest_reaction(payload: ReactionIn, db: Session = Depends(get_db)):
    event_ts = payload.timestamp or int(time.time())

    event = ReactionEvent(
        wa_message_id=payload.wa_message_id,
        reactor=payload.reactor,
        emoji=payload.emoji,
        event_type=payload.event_type,
        group_id=payload.group_id,
        group_name=payload.group_name,
        timestamp=event_ts,
    )
    db.add(event)
    db.flush()

    matched_message = (
        db.query(Message)
        .filter(Message.wa_message_id == payload.wa_message_id)
        .order_by(Message.id.desc())
        .first()
    )

    if matched_message:
        add_count = (
            db.query(ReactionEvent)
            .filter(ReactionEvent.wa_message_id == payload.wa_message_id)
            .filter(ReactionEvent.event_type == "add")
            .count()
        )
        remove_count = (
            db.query(ReactionEvent)
            .filter(ReactionEvent.wa_message_id == payload.wa_message_id)
            .filter(ReactionEvent.event_type == "remove")
            .count()
        )
        matched_message.reaction_score = float(max(add_count - remove_count, 0))
        matched_message.rank_score = compute_rank_score(
            token_count=matched_message.token_count,
            has_url=bool(matched_message.has_url),
            has_mention=bool(matched_message.has_mention),
            has_hashtag=bool(matched_message.has_hashtag),
            duplicate_count=matched_message.duplicate_count,
            reaction_score=matched_message.reaction_score,
            message_timestamp=matched_message.timestamp,
        )

    db.commit()
    return {
        "status": "ok",
        "event_id": event.id,
        "matched_message_id": matched_message.id if matched_message else None,
    }
