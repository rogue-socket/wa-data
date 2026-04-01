import hashlib
import json
import os
import re
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import CategoryProposal, Message, OutgoingMessage, ReactionEvent

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
            "category": "VARCHAR NOT NULL DEFAULT 'facts-and-insights'",
            "category_confidence": "FLOAT NOT NULL DEFAULT 0.0",
            "tags_json": "TEXT",
            "source_platform": "VARCHAR",
            "source_domain": "VARCHAR",
            "category_version": "VARCHAR NOT NULL DEFAULT 'v1'",
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
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_category ON messages(category)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_source_platform ON messages(source_platform)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_messages_source_domain ON messages(source_domain)")
        )


def ensure_category_proposals_schema() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS category_proposals (
                    id INTEGER PRIMARY KEY,
                    proposal_slug VARCHAR NOT NULL,
                    display_name VARCHAR NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'proposed',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    trigger_terms_json TEXT,
                    sample_text TEXT,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    reviewed_at INTEGER
                )
                """
            )
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_category_proposals_slug ON category_proposals(proposal_slug)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_category_proposals_status ON category_proposals(status)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS idx_category_proposals_last_seen ON category_proposals(last_seen_at)")
        )


URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\w+")
HASHTAG_PATTERN = re.compile(r"#\w+")
WHITESPACE_PATTERN = re.compile(r"\s+")
SEARCH_TOKEN_PATTERN = re.compile(r"[\\w@#:/.\\-]+")
WORD_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9+.-]{2,}")

CATEGORY_VERSION = "v1"
PRIMARY_CATEGORIES = (
    "opportunities",
    "startup-funding-news",
    "events-hackathons-meetups",
    "learning-and-research",
    "open-source-and-repos",
    "tools-and-libraries",
    "product-launches",
    "articles-and-industry-news",
    "ai-ml",
    "facts-and-insights",
)

PROPOSAL_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "been",
    "from",
    "have",
    "here",
    "http",
    "https",
    "into",
    "just",
    "more",
    "that",
    "their",
    "there",
    "this",
    "what",
    "with",
    "your",
}

CATEGORY_RULES: dict[str, list[str]] = {
    "opportunities": [
        "internship",
        "intern",
        "job",
        "hiring",
        "career",
        "apply",
        "opening",
        "role",
        "referral",
    ],
    "startup-funding-news": [
        "funding",
        "seed",
        "series a",
        "series b",
        "valuation",
        "raised",
        "acquired",
        "acquisition",
        "startup",
        "vc",
    ],
    "events-hackathons-meetups": [
        "hackathon",
        "meetup",
        "event",
        "workshop",
        "summit",
        "conference",
        "webinar",
        "deadline",
        "register",
    ],
    "learning-and-research": [
        "course",
        "tutorial",
        "learn",
        "curriculum",
        "guide",
        "resource",
        "paper",
        "arxiv",
        "study",
    ],
    "open-source-and-repos": [
        "open source",
        "opensource",
        "github",
        "repo",
        "repository",
        "workflow",
        "prompt",
    ],
    "tools-and-libraries": [
        "tool",
        "library",
        "framework",
        "sdk",
        "package",
        "pip",
        "npm",
        "plugin",
        "api",
    ],
    "product-launches": [
        "launch",
        "launched",
        "release",
        "released",
        "beta",
        "waitlist",
        "ship",
        "product hunt",
    ],
    "articles-and-industry-news": [
        "article",
        "news",
        "thread",
        "newsletter",
        "post",
        "read",
        "hacker news",
    ],
    "ai-ml": [
        "ai",
        "ml",
        "llm",
        "gpt",
        "gemini",
        "agent",
        "rag",
        "embedding",
        "transformer",
    ],
}

DOMAIN_CATEGORY_HINTS: dict[str, str] = {
    "github.com": "open-source-and-repos",
    "arxiv.org": "learning-and-research",
    "youtube.com": "learning-and-research",
    "youtu.be": "learning-and-research",
    "news.ycombinator.com": "articles-and-industry-news",
    "x.com": "articles-and-industry-news",
    "twitter.com": "articles-and-industry-news",
    "t.co": "articles-and-industry-news",
    "producthunt.com": "product-launches",
}


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


def load_string_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            return []
        return [str(item) for item in decoded if isinstance(item, str) and item]
    except json.JSONDecodeError:
        return []


def slugify(value: str) -> str:
    lowered = (value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    return cleaned.strip("-")


def display_name_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def extract_source_metadata(metadata: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    urls = metadata.get("urls", [])
    if not isinstance(urls, list) or not urls:
        return None, None

    first_url = urls[0]
    if not isinstance(first_url, str) or not first_url:
        return None, None

    parsed = urlparse.urlparse(first_url)
    domain = parsed.netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]

    if not domain:
        return None, None

    if domain in {"x.com", "twitter.com", "t.co"} or domain.endswith(".twitter.com"):
        return "twitter", domain
    if domain in {"youtube.com", "youtu.be"} or domain.endswith(".youtube.com"):
        return "youtube", domain
    if domain == "github.com":
        return "github", domain
    if domain == "news.ycombinator.com":
        return "hacker-news", domain
    if domain == "arxiv.org":
        return "arxiv", domain

    base_label = domain.split(".")[0] if "." in domain else domain
    return base_label, domain


def classify_rule_based(message_text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    text_value = message_text or ""
    lowered = text_value.lower()
    source_platform, source_domain = extract_source_metadata(metadata)
    scores: dict[str, float] = {category: 0.0 for category in PRIMARY_CATEGORIES}
    matched_terms: dict[str, list[str]] = {category: [] for category in PRIMARY_CATEGORIES}

    for category, terms in CATEGORY_RULES.items():
        for term in terms:
            if term in lowered:
                scores[category] += 0.28
                matched_terms[category].append(term)

    if source_domain:
        hinted_category = DOMAIN_CATEGORY_HINTS.get(source_domain)
        if hinted_category:
            scores[hinted_category] += 0.45

    if metadata.get("urls"):
        scores["articles-and-industry-news"] += 0.12

    bot_metadata = metadata.get("bot_metadata")
    if isinstance(bot_metadata, dict):
        msg_type = str(bot_metadata.get("type") or "").lower()
        has_media = bool(bot_metadata.get("has_media"))
        if msg_type in {"video", "ptt", "audio"}:
            scores["learning-and-research"] += 0.25
        if has_media:
            scores["facts-and-insights"] += 0.05

    top_category = "facts-and-insights"
    top_score = 0.0
    for category, score in scores.items():
        if score > top_score:
            top_score = score
            top_category = category

    confidence = 0.35 if top_score <= 0 else min(0.95, 0.35 + top_score)

    tags: list[str] = []
    hashtags = metadata.get("hashtags", [])
    if isinstance(hashtags, list):
        for hashtag in hashtags[:5]:
            if isinstance(hashtag, str):
                cleaned = slugify(hashtag.replace("#", ""))
                if cleaned:
                    tags.append(cleaned)

    if source_platform:
        tags.append(f"source-{source_platform}")

    for term in matched_terms.get(top_category, [])[:4]:
        cleaned = slugify(term)
        if cleaned:
            tags.append(cleaned)

    # Preserve insertion order while deduplicating tags.
    tags = list(dict.fromkeys(tags))[:10]

    candidate_terms: list[str] = []
    token_counts = Counter(WORD_TOKEN_PATTERN.findall(lowered))
    for token, _ in token_counts.most_common(8):
        if token in PROPOSAL_STOPWORDS or token in {"twitter", "thread", "link"}:
            continue
        if len(token) < 4:
            continue
        candidate_terms.append(token)

    return {
        "category": top_category,
        "confidence": round(confidence, 4),
        "tags": tags,
        "source": "rules",
        "source_platform": source_platform,
        "source_domain": source_domain,
        "matched_terms": matched_terms.get(top_category, [])[:8],
        "candidate_terms": candidate_terms[:5],
    }


def should_use_gemini(rule_confidence: float) -> bool:
    if os.getenv("ENABLE_GEMINI_CLASSIFIER", "false").strip().lower() != "true":
        return False
    if not os.getenv("GEMINI_API_KEY"):
        return False
    threshold = float(os.getenv("GEMINI_CONFIDENCE_THRESHOLD", "0.62"))
    return rule_confidence < threshold


def classify_with_gemini(message_text: str, metadata: dict[str, Any]) -> Optional[dict[str, Any]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    compact_text = (message_text or "").strip()[:800]
    urls = metadata.get("urls", [])
    context_urls = urls[:3] if isinstance(urls, list) else []

    prompt = {
        "task": "Classify one WhatsApp message into exactly one allowed category.",
        "allowed_categories": list(PRIMARY_CATEGORIES),
        "instructions": [
            "Return strict JSON only.",
            "Keep tags short and lowercase slugs.",
            "Use category facts-and-insights if uncertain.",
        ],
        "message_text": compact_text,
        "urls": context_urls,
    }
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(prompt, ensure_ascii=True)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 180,
            "responseMimeType": "application/json",
        },
    }

    request = urlrequest.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlrequest.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
    except (urlerror.URLError, TimeoutError, json.JSONDecodeError):
        return None

    try:
        text_output = payload["candidates"][0]["content"]["parts"][0]["text"]
        decoded = json.loads(text_output)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None

    category = str(decoded.get("category") or "").strip()
    if category not in PRIMARY_CATEGORIES:
        return None

    try:
        confidence = float(decoded.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    tags = decoded.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    cleaned_tags: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = slugify(tag)
        if cleaned:
            cleaned_tags.append(cleaned)

    return {
        "category": category,
        "confidence": max(0.0, min(1.0, confidence)),
        "tags": list(dict.fromkeys(cleaned_tags))[:10],
        "source": "gemini",
    }


def classify_message(message_text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    rule_result = classify_rule_based(message_text, metadata)
    if not should_use_gemini(rule_result["confidence"]):
        return rule_result

    gemini_result = classify_with_gemini(message_text, metadata)
    if not gemini_result:
        return rule_result

    merged_tags = list(dict.fromkeys(rule_result["tags"] + gemini_result["tags"]))[:10]
    return {
        "category": gemini_result["category"],
        "confidence": round(float(gemini_result["confidence"]), 4),
        "tags": merged_tags,
        "source": gemini_result["source"],
        "source_platform": rule_result.get("source_platform"),
        "source_domain": rule_result.get("source_domain"),
        "matched_terms": rule_result.get("matched_terms", []),
        "candidate_terms": rule_result.get("candidate_terms", []),
    }


def update_category_proposals(
    db: Session,
    message_text: str,
    message_timestamp: int,
    category: str,
    confidence: float,
    candidate_terms: list[str],
) -> None:
    if category != "facts-and-insights" and confidence >= 0.58:
        return

    for term in candidate_terms[:3]:
        slug = slugify(term)
        if not slug or len(slug) < 4 or slug in PRIMARY_CATEGORIES:
            continue

        existing = (
            db.query(CategoryProposal)
            .filter(CategoryProposal.proposal_slug == slug)
            .order_by(CategoryProposal.id.desc())
            .first()
        )
        if existing:
            if existing.status == "rejected":
                continue
            existing.occurrence_count += 1
            existing.last_seen_at = int(message_timestamp)
            if not existing.sample_text:
                existing.sample_text = message_text[:280]
            continue

        proposal = CategoryProposal(
            proposal_slug=slug,
            display_name=display_name_from_slug(slug),
            status="proposed",
            occurrence_count=1,
            trigger_terms_json=json.dumps([term], ensure_ascii=True),
            sample_text=message_text[:280],
            first_seen_at=int(message_timestamp),
            last_seen_at=int(message_timestamp),
        )
        db.add(proposal)


def build_metadata_terms(metadata: dict[str, Any], category: Optional[str] = None, tags_json: Optional[str] = None) -> str:
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

    if category:
        terms.append(category)
        terms.extend(category.split("-"))

    for tag in load_string_list(tags_json):
        terms.append(tag)
        terms.extend(tag.split("-"))

    return " ".join(terms)


def sync_message_to_fts(db: Session, row: Message) -> None:
    metadata_terms = build_metadata_terms(
        load_metadata(row.metadata_json),
        category=row.category,
        tags_json=row.tags_json,
    )
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
                    COALESCE(m.metadata_json, '') || ' ' || COALESCE(m.category, '') || ' ' || COALESCE(m.tags_json, '')
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
ensure_category_proposals_schema()
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


class CategoryProposalReviewIn(BaseModel):
    status: Literal["approved", "rejected"]


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
    category: Optional[str] = Query(default=None),
    source_platform: Optional[str] = Query(default=None),
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

    if category:
        query = query.filter(Message.category == category)

    if source_platform:
        query = query.filter(Message.source_platform == source_platform)

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
            "category": row.category,
            "category_confidence": row.category_confidence,
            "category_version": row.category_version,
            "tags": load_string_list(row.tags_json),
            "source_platform": row.source_platform,
            "source_domain": row.source_domain,
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
    category: Optional[str] = Query(default=None),
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
    if category:
        query = query.filter(Message.category == category)

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
                "category": row.category,
                "category_confidence": row.category_confidence,
                "tags": load_string_list(row.tags_json),
                "source_platform": row.source_platform,
                "source_domain": row.source_domain,
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
            current["category"] = row.category
            current["category_confidence"] = row.category_confidence
            current["tags"] = load_string_list(row.tags_json)
            current["source_platform"] = row.source_platform
            current["source_domain"] = row.source_domain
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
    category: Optional[str] = Query(default=None),
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
        AND (:category IS NULL OR m.category = :category)
    """

    params = {
        "match_query": match_query,
        "group_id": group_id,
        "group_name_like": group_name_like,
        "category": category,
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
                    m.category,
                    m.category_confidence,
                    m.tags_json,
                    m.source_platform,
                    m.source_domain,
                    m.category_version,
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
                m.category,
                m.category_confidence,
                m.tags_json,
                m.source_platform,
                m.source_domain,
                m.category_version,
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
            "category": row["category"],
            "category_confidence": row["category_confidence"],
            "category_version": row["category_version"],
            "tags": load_string_list(row["tags_json"]),
            "source_platform": row["source_platform"],
            "source_domain": row["source_domain"],
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


@app.get("/categories")
def list_categories():
    return {
        "version": CATEGORY_VERSION,
        "primary_categories": list(PRIMARY_CATEGORIES),
        "gemini_classifier": {
            "enabled": os.getenv("ENABLE_GEMINI_CLASSIFIER", "false").strip().lower() == "true",
            "model": os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite"),
        },
    }


@app.get("/categories/proposals")
def list_category_proposals(
    status: Literal["proposed", "approved", "rejected", "all"] = Query(default="proposed"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(CategoryProposal)
    if status != "all":
        query = query.filter(CategoryProposal.status == status)

    rows = (
        query.order_by(CategoryProposal.occurrence_count.desc(), CategoryProposal.last_seen_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "status": status,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": row.id,
                "proposal_slug": row.proposal_slug,
                "display_name": row.display_name,
                "status": row.status,
                "occurrence_count": row.occurrence_count,
                "trigger_terms": load_string_list(row.trigger_terms_json),
                "sample_text": row.sample_text,
                "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at,
                "reviewed_at": row.reviewed_at,
            }
            for row in rows
        ],
    }


@app.post("/categories/proposals/{proposal_id}/review")
def review_category_proposal(
    proposal_id: int,
    payload: CategoryProposalReviewIn,
    db: Session = Depends(get_db),
):
    row = db.query(CategoryProposal).filter(CategoryProposal.id == proposal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="category proposal not found")

    row.status = payload.status
    row.reviewed_at = int(time.time())
    db.commit()

    return {
        "status": "ok",
        "proposal_id": row.id,
        "proposal_slug": row.proposal_slug,
        "review_status": row.status,
    }


@app.post("/ingest")
def ingest(message: MessageIn, db: Session = Depends(get_db)):
    metadata = extract_metadata(message.text, incoming_metadata=message.metadata)
    classification = classify_message(message.text, metadata)
    metadata["classification"] = {
        "category": classification["category"],
        "confidence": classification["confidence"],
        "source": classification["source"],
        "version": CATEGORY_VERSION,
    }
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
        category=classification["category"],
        category_confidence=float(classification["confidence"]),
        tags_json=json.dumps(classification["tags"], ensure_ascii=True),
        source_platform=classification.get("source_platform"),
        source_domain=classification.get("source_domain"),
        category_version=CATEGORY_VERSION,
        duplicate_group_key=duplicate_group_key,
        similarity_to_canonical=similarity,
        duplicate_count=1,
        reaction_score=0.0,
        rank_score=0.0,
    )
    db.add(db_message)
    db.flush()
    update_category_proposals(
        db,
        message_text=message.text,
        message_timestamp=message.timestamp,
        category=classification["category"],
        confidence=float(classification["confidence"]),
        candidate_terms=classification.get("candidate_terms", []),
    )
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
        "category": db_message.category,
        "category_confidence": db_message.category_confidence,
        "tags": load_string_list(db_message.tags_json),
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
