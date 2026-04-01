import argparse
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Message

WHITESPACE_PATTERN = re.compile(r"\s+")

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TAXONOMY_PATH = BACKEND_DIR / "classifier" / "taxonomy.jsonl"
DEFAULT_PROMPT_PATH = BACKEND_DIR / "classifier" / "prompt_skeleton.txt"


@dataclass
class TaxonomyEntry:
    code: str
    category: str
    hint: str


def normalize_text(value: str) -> str:
    lowered = (value or "").strip().lower()
    return WHITESPACE_PATTERN.sub(" ", lowered)


def slugify(value: str) -> str:
    lowered = (value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    return cleaned.strip("-")


def load_metadata(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_string_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if isinstance(item, str) and item]


def compact_text(value: str, max_chars: int = 260) -> str:
    return normalize_text((value or "")[:max_chars])


def estimate_tokens(text_value: str) -> int:
    if not text_value:
        return 0
    return max(1, math.ceil(len(text_value) / 4))


def get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_taxonomy(taxonomy_path: Path) -> list[TaxonomyEntry]:
    if not taxonomy_path.exists():
        raise FileNotFoundError(f"taxonomy file missing: {taxonomy_path}")

    entries: list[TaxonomyEntry] = []
    with taxonomy_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            payload = json.loads(line)
            code = str(payload.get("code") or "").strip()
            category = str(payload.get("category") or "").strip()
            hint = str(payload.get("hint") or "").strip()
            if not code or not category:
                continue
            entries.append(TaxonomyEntry(code=code, category=category, hint=hint))

    if not entries:
        raise ValueError("taxonomy file has no usable entries")
    return entries


def load_prompt_template(prompt_path: Path) -> str:
    if not prompt_path.exists():
        raise FileNotFoundError(f"prompt skeleton missing: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


def build_prompt(template: str, taxonomy: list[TaxonomyEntry], items: list[dict[str, Any]]) -> str:
    compact_taxonomy = [{"c": row.code, "h": row.hint} for row in taxonomy]
    payload = {
        "taxonomy": compact_taxonomy,
        "items": items,
    }

    return "\n".join(
        [
            template,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        ]
    )


def select_candidate_rows(
    db: Session,
    since_ts: int,
    limit: int,
    only_with_urls: bool,
) -> list[Message]:
    query = db.query(Message).filter(Message.timestamp >= since_ts)
    if only_with_urls:
        query = query.filter(Message.has_url.is_(True))

    return query.order_by(Message.id.asc()).limit(limit).all()


def build_batch_items(rows: list[Message]) -> tuple[list[dict[str, Any]], dict[int, Message]]:
    items: list[dict[str, Any]] = []
    row_lookup: dict[int, Message] = {}

    for row in rows:
        metadata = load_metadata(row.metadata_json)
        urls = metadata.get("urls", [])
        cleaned_urls = [value for value in urls if isinstance(value, str) and value][:1] if isinstance(urls, list) else []

        item = {
            "id": row.id,
            "txt": compact_text(row.text, max_chars=280),
            "cat": row.category,
        }
        if cleaned_urls:
            item["u"] = cleaned_urls
        if row.source_domain:
            item["d"] = row.source_domain

        items.append(item)
        row_lookup[row.id] = row

    return items, row_lookup


def parse_gemini_labels(raw_output: str, code_to_category: dict[str, str]) -> list[dict[str, Any]]:
    decoded = json.loads(raw_output)
    labels: Any = None

    if isinstance(decoded, dict):
        labels = decoded.get("labels")
    elif isinstance(decoded, list):
        labels = decoded

    if not isinstance(labels, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in labels:
        if not isinstance(item, dict):
            continue

        raw_id = item.get("id")
        if raw_id is None:
            continue
        try:
            message_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        code = str(item.get("c") or "").strip()
        if code not in code_to_category:
            continue

        try:
            confidence = float(item.get("conf", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        tags_raw = item.get("t", [])
        tags: list[str] = []
        if isinstance(tags_raw, list):
            for tag in tags_raw:
                if not isinstance(tag, str):
                    continue
                cleaned = slugify(tag)
                if cleaned:
                    tags.append(cleaned)
        tags = list(dict.fromkeys(tags))[:4]

        normalized.append(
            {
                "id": message_id,
                "code": code,
                "category": code_to_category[code],
                "confidence": confidence,
                "tags": tags,
            }
        )

    return normalized


def update_fts_metadata_terms(db: Session, row: Message) -> None:
    metadata = load_metadata(row.metadata_json)
    terms: list[str] = []

    language = metadata.get("language")
    if isinstance(language, str) and language:
        terms.append(language)

    urls = metadata.get("urls", [])
    if isinstance(urls, list):
        terms.extend(value for value in urls if isinstance(value, str) and value)

    mentions = metadata.get("mentions", [])
    if isinstance(mentions, list):
        terms.extend(value for value in mentions if isinstance(value, str) and value)

    hashtags = metadata.get("hashtags", [])
    if isinstance(hashtags, list):
        terms.extend(value for value in hashtags if isinstance(value, str) and value)

    if row.category:
        terms.append(row.category)
        terms.extend(row.category.split("-"))

    for tag in load_string_list(row.tags_json):
        terms.append(tag)
        terms.extend(tag.split("-"))

    metadata_terms = " ".join(terms)
    db.execute(
        text("UPDATE messages_fts SET metadata_terms = :metadata_terms WHERE rowid = :rowid"),
        {"metadata_terms": metadata_terms, "rowid": row.id},
    )


def apply_labels(
    db: Session,
    labels: list[dict[str, Any]],
    row_lookup: dict[int, Message],
    category_version: str,
    dry_run: bool,
) -> int:
    updated_count = 0

    for label in labels:
        row = row_lookup.get(label["id"])
        if row is None:
            continue

        row.category = label["category"]
        row.category_confidence = float(label["confidence"])
        row.tags_json = json.dumps(label["tags"], ensure_ascii=True)
        row.category_version = category_version

        metadata = load_metadata(row.metadata_json)
        metadata["classification"] = {
            "category": row.category,
            "confidence": row.category_confidence,
            "source": "gemini-batch",
            "version": category_version,
            "classified_at": int(time.time()),
        }
        row.metadata_json = json.dumps(metadata, ensure_ascii=True)

        if not dry_run:
            try:
                update_fts_metadata_terms(db, row)
            except OperationalError:
                pass
        updated_count += 1

    return updated_count


def call_gemini_batch(
    api_key: str,
    model: str,
    prompt_text: str,
    max_output_tokens: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], str]:
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    )

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt_text}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        },
    }

    request = urlrequest.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urlrequest.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))

    text_output = payload["candidates"][0]["content"]["parts"][0]["text"]
    return payload, text_output


def chunked(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def run_job(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for batch classification")

    taxonomy = load_taxonomy(Path(args.taxonomy_path))
    template = load_prompt_template(Path(args.prompt_path))
    code_to_category = {row.code: row.category for row in taxonomy}

    now_ts = int(time.time())
    since_ts = now_ts - (args.days * 86400)

    db = SessionLocal()
    try:
        rows = select_candidate_rows(
            db=db,
            since_ts=since_ts,
            limit=args.limit,
            only_with_urls=args.only_with_urls,
        )

        items, row_lookup = build_batch_items(rows)
        batches = chunked(items, args.chunk_size)

        classified_total = 0
        updated_total = 0
        error_count = 0
        input_tokens_est = 0
        output_tokens_est = 0

        for batch in batches:
            prompt = build_prompt(template, taxonomy, batch)
            input_tokens_est += estimate_tokens(prompt)

            try:
                _, response_text = call_gemini_batch(
                    api_key=api_key,
                    model=args.model,
                    prompt_text=prompt,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout_seconds,
                )
            except (urlerror.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                error_count += 1
                continue

            output_tokens_est += estimate_tokens(response_text)
            try:
                labels = parse_gemini_labels(response_text, code_to_category)
            except json.JSONDecodeError:
                error_count += 1
                continue

            classified_total += len(labels)
            updated_total += apply_labels(
                db=db,
                labels=labels,
                row_lookup=row_lookup,
                category_version=args.category_version,
                dry_run=args.dry_run,
            )

            if not args.dry_run:
                db.commit()

        if args.dry_run:
            db.rollback()

        input_cost_per_mtokens = get_env_float("GEMINI_BATCH_INPUT_COST_PER_MTOKENS_USD", 0.0)
        output_cost_per_mtokens = get_env_float("GEMINI_BATCH_OUTPUT_COST_PER_MTOKENS_USD", 0.0)
        estimated_cost_usd = (
            (input_tokens_est / 1_000_000.0) * input_cost_per_mtokens
            + (output_tokens_est / 1_000_000.0) * output_cost_per_mtokens
        )

        return {
            "status": "ok",
            "days": args.days,
            "since_ts": since_ts,
            "model": args.model,
            "category_version": args.category_version,
            "rows_scanned": len(rows),
            "batch_count": len(batches),
            "chunk_size": args.chunk_size,
            "classified_count": classified_total,
            "updated_count": updated_total,
            "error_batches": error_count,
            "estimated_input_tokens": input_tokens_est,
            "estimated_output_tokens": output_tokens_est,
            "estimated_total_tokens": input_tokens_est + output_tokens_est,
            "pricing": {
                "input_cost_per_mtokens_usd": input_cost_per_mtokens,
                "output_cost_per_mtokens_usd": output_cost_per_mtokens,
                "estimated_cost_usd": round(estimated_cost_usd, 8),
            },
            "dry_run": args.dry_run,
        }
    finally:
        db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2-day Gemini batch re-classification job")

    parser.add_argument("--days", type=int, default=int(os.getenv("GEMINI_BATCH_DAYS", "2")))
    parser.add_argument("--limit", type=int, default=int(os.getenv("GEMINI_BATCH_LIMIT", "1200")))
    parser.add_argument("--chunk-size", type=int, default=int(os.getenv("GEMINI_BATCH_CHUNK_SIZE", "30")))
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("GEMINI_BATCH_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-2.0-flash-lite",
    )
    parser.add_argument(
        "--taxonomy-path",
        type=str,
        default=os.getenv("GEMINI_BATCH_TAXONOMY_PATH") or str(DEFAULT_TAXONOMY_PATH),
    )
    parser.add_argument(
        "--prompt-path",
        type=str,
        default=os.getenv("GEMINI_BATCH_PROMPT_PATH") or str(DEFAULT_PROMPT_PATH),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=int(os.getenv("GEMINI_BATCH_MAX_OUTPUT_TOKENS", "900")),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("GEMINI_BATCH_TIMEOUT_SECONDS", "12")),
    )
    parser.add_argument(
        "--category-version",
        type=str,
        default=os.getenv("GEMINI_BATCH_CATEGORY_VERSION", "v1-gemini-batch-lite"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-with-urls", dest="only_with_urls", action="store_true")
    parser.add_argument("--include-no-url", dest="only_with_urls", action="store_false")
    parser.set_defaults(only_with_urls=True)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_job(args)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
