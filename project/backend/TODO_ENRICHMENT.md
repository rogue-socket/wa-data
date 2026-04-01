# Enrichment Roadmap TODOs

## Metadata Extraction

- [x] Extract deterministic metadata at ingest time: URLs, mentions, hashtags, token count, language heuristic.
- [x] Store metadata in `messages.metadata_json`.
- [x] Persist metadata flags in columns (`has_url`, `has_mention`, `has_hashtag`).
- [ ] Add stronger language detection using dedicated package (langdetect or fasttext).
- [ ] Add extraction for attachment metadata and quoted/reply context.
- [ ] Add metadata quality score for ranking explainability.

## Ranking Model

- [x] Add rank feature columns (`duplicate_count`, `reaction_score`, `rank_score`).
- [x] Implement deterministic v1 scoring function (recency + quality + duplicates + reactions).
- [x] Recompute rank when dedupe cluster size changes.
- [x] Recompute rank when reaction score changes.
- [ ] Add topic relevance feature for contextual ranking.
- [ ] Add per-group sender authority and trust score.
- [ ] Add versioned ranking model metadata in API responses.
- [ ] Add offline evaluation script for ranking quality validation.

## Searchable Index

- [ ] Add SQLite FTS5 table for message text and selected metadata fields.
- [ ] Add synchronization path from ingest updates to FTS index.
- [ ] Add `/search` endpoint with group filters, sorting, and pagination.
- [ ] Add merged-cluster search mode to reduce duplicate noise.
- [ ] Add query analytics table for search relevance tuning.

## Aggregation

- [ ] Add daily sender activity rollup.
- [ ] Add daily group activity rollup.
- [ ] Add reaction trend aggregation by emoji and time bucket.
- [ ] Add top duplicate clusters endpoint (themes repeated in group).
- [ ] Add entity/keyword trend endpoint over rolling windows.
- [ ] Add materialized rollup refresh command for heavier datasets.

## Testing and Operability

- [ ] Add API tests for `/bot/send`, `/bot/commands/next`, `/bot/commands/{id}/result`.
- [ ] Add API tests for `/messages` filters and `/messages/merged` output.
- [ ] Add ingestion tests for similarity threshold behavior (`>= 0.80`).
- [ ] Add reaction event tests for add/remove behavior.
- [ ] Add performance checks for list/sort operations with larger datasets.
