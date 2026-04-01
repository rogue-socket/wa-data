# Minimal WhatsApp -> DB MVP

This repository contains a minimal system that:

1. Connects to WhatsApp Web
2. Reads messages from WhatsApp groups
3. Sends those messages to a backend API
4. Stores them in SQLite

## Project Layout

```text
project/
├── bot/
│   ├── index.js
│   └── package.json
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── models.py
│   │   └── database.py
│   ├── requirements.txt
│   └── venv/
```

## Tech Stack

- Bot: Node.js LTS, whatsapp-web.js, qrcode-terminal, axios
- Backend: Python 3.14, FastAPI
- Database: SQLite via SQLAlchemy
- Python environment: existing conda env `wa-data`

## Setup

### 0. Create Local Config File (`user.env`)

This repo uses a local `user.env` file for environment configuration.

```bash
cp user.env.example user.env
```

Update values in `user.env` as needed for your machine.

For the 2-day batch classifier, keep these keys present in `user.env` (you can fill API key and model later):

```bash
GEMINI_API_KEY=
GEMINI_BATCH_ENABLED=true
GEMINI_BATCH_DAYS=2
GEMINI_BATCH_LIMIT=1200
GEMINI_BATCH_CHUNK_SIZE=30
GEMINI_BATCH_MODEL=gemini-2.0-flash-lite
GEMINI_BATCH_MAX_OUTPUT_TOKENS=900
GEMINI_BATCH_TIMEOUT_SECONDS=12
GEMINI_BATCH_CATEGORY_VERSION=v1-gemini-batch-lite
GEMINI_BATCH_INPUT_COST_PER_MTOKENS_USD=0
GEMINI_BATCH_OUTPUT_COST_PER_MTOKENS_USD=0
```

### 1. Backend Dependencies (conda env `wa-data`)

```bash
conda run -n wa-data pip install -r project/backend/requirements.txt
```

### 2. Bot Dependencies

```bash
cd project/bot
npm install
```

## Run

### 1. Start Backend

```bash
cd project/backend
conda run -n wa-data uvicorn app.main:app --reload
```

Backend endpoint:

- `POST http://localhost:8000/ingest`
- `GET http://localhost:8000/messages`
- `GET http://localhost:8000/messages/merged`
- `GET http://localhost:8000/search`
- `GET http://localhost:8000/categories`
- `GET http://localhost:8000/categories/proposals`
- `POST http://localhost:8000/categories/proposals/{id}/review`
- `POST http://localhost:8000/bot/send`
- `GET http://localhost:8000/bot/commands/next`
- `POST http://localhost:8000/bot/commands/{id}/result`
- `POST http://localhost:8000/reactions/ingest`
- `GET http://localhost:8000/` (live message dashboard)

Expected payload:

```json
{
	"text": "hello",
	"sender": "user123",
	"group_id": "group123",
	"group_name": "My WhatsApp Group",
	"timestamp": 1234567890
}
```

Enriched payload fields supported:

```json
{
	"wa_message_id": "true_12345@g.us_ABCD...",
	"metadata": {
		"type": "chat",
		"has_media": false,
		"from_me": false,
		"mentioned_ids": []
	}
}
```

Messages are now enriched on ingest with metadata extraction, dedupe grouping (similarity threshold `>= 0.80` within the same group), and a deterministic `rank_score`.
Messages are also classified with a rule-first category model, with optional Gemini fallback for low-confidence messages.

### 2. Start Bot

Open a second terminal:

```bash
cd project/bot
node index.js
```

Optional bot environment variables:

```bash
BACKEND_URL=http://127.0.0.1:8000/ingest
BACKEND_REACTIONS_URL=http://127.0.0.1:8000/reactions/ingest
BACKEND_COMMAND_NEXT_URL=http://127.0.0.1:8000/bot/commands/next
BACKEND_COMMAND_RESULT_URL=http://127.0.0.1:8000/bot/commands
COMMAND_POLL_INTERVAL_MS=3000

# Optional low-cost Gemini fallback for classification
ENABLE_GEMINI_CLASSIFIER=false
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash-lite
GEMINI_CONFIDENCE_THRESHOLD=0.62

# Optional 2-day Gemini batch re-classifier
GEMINI_BATCH_ENABLED=true
GEMINI_BATCH_DAYS=2
GEMINI_BATCH_LIMIT=1200
GEMINI_BATCH_CHUNK_SIZE=30
GEMINI_BATCH_MODEL=gemini-2.0-flash-lite
GEMINI_BATCH_MAX_OUTPUT_TOKENS=900
GEMINI_BATCH_TIMEOUT_SECONDS=12
GEMINI_BATCH_CATEGORY_VERSION=v1-gemini-batch-lite
GEMINI_BATCH_INPUT_COST_PER_MTOKENS_USD=0
GEMINI_BATCH_OUTPUT_COST_PER_MTOKENS_USD=0
```

You can export variables from `user.env` before running services:

```bash
set -a
source user.env
set +a
```

### 3. Open the Dashboard

In your browser, open:

```bash
http://127.0.0.1:8000/
```

The page polls every 2 seconds and shows all stored messages.

## End-to-End Test

1. Start backend.
2. Start bot.
3. Scan QR code in terminal.
4. Ensure bot account is in a WhatsApp group.
5. Send group messages.
6. Verify bot logs API success/failure.
7. Verify data in SQLite:

```bash
sqlite3 project/backend/messages.db "SELECT id,text,sender,group_id,timestamp FROM messages ORDER BY id DESC LIMIT 20;"
```

## Notes

- Bot ingests only group messages (`msg.from.includes('@g.us')`).
- Stored fields are: `text`, `sender`, `group_id`, `group_name`, `timestamp`.
- Outbound message sending is queue-based:
	1. Backend enqueues via `POST /bot/send`.
	2. Bot polls `GET /bot/commands/next`.
	3. Bot reports result to `POST /bot/commands/{id}/result`.
- Reaction events are captured via WhatsApp `message_reaction` and forwarded to `POST /reactions/ingest`.
- Filter and sorting support for `GET /messages`:
	- `group_id`
	- `group_name` (partial match)
	- `category`
	- `source_platform`
	- `sort_by` in `newest|oldest|rank|duplicates`
	- `limit`, `offset`
- Full-text search support for `GET /search`:
	- `q` (query string, required)
	- `group_id`
	- `group_name` (partial match)
	- `category`
	- `sort_by` in `relevance|newest|oldest|rank|duplicates`
	- `merged` (`true|false`) to return one representative per duplicate cluster
	- `limit`, `offset`
- Primary categories (`GET /categories`):
	- opportunities
	- startup-funding-news
	- events-hackathons-meetups
	- learning-and-research
	- open-source-and-repos
	- tools-and-libraries
	- product-launches
	- articles-and-industry-news
	- ai-ml
	- facts-and-insights
- Dynamic proposals (`GET /categories/proposals`):
	- low-confidence/fallback messages can propose new categories over time
	- approve/reject with `POST /categories/proposals/{id}/review`
- Batch classifier endpoints:
	- `GET /categories/batch-config`
	- `POST /categories/batch-classify`

## 2-Day Gemini Batch Re-Classifier

For consistent category refresh with low cost, run a batch recategorization every 2 days.

Classifier spec files:

- `project/backend/classifier/taxonomy.jsonl` (compact taxonomy with short category codes)
- `project/backend/classifier/prompt_skeleton.txt` (strict JSON output prompt skeleton)

Run manually:

```bash
cd project/backend
set -a
source ../../user.env
set +a
conda run -n wa-data python -m app.batch_classifier --days 2 --limit 1200 --chunk-size 30
```

Dry-run (no DB writes):

```bash
cd project/backend
set -a
source ../../user.env
set +a
conda run -n wa-data python -m app.batch_classifier --days 2 --limit 1200 --chunk-size 30 --dry-run
```

Run from API (manual trigger, no cron needed):

```bash
curl -X POST http://127.0.0.1:8000/categories/batch-classify \
	-H "Content-Type: application/json" \
	-d '{
		"days": 2,
		"limit": 200,
		"chunk_size": 25,
		"dry_run": true,
		"only_with_urls": true
	}'
```

The response includes token estimates and an optional USD estimate if pricing env keys are set.

Cron setup example (every 2 days):

```bash
crontab -e
```

Add this line:

```cron
10 2 */2 * * cd /Users/yashagrawal/Documents/wa-data/project/backend && /Users/yashagrawal/Documents/wa-data/project/backend/scripts/run_batch_classifier.sh --days 2 --limit 1200 --chunk-size 30 >> /Users/yashagrawal/Documents/wa-data/project/backend/batch_classifier.log 2>&1
```

Notes:

- `*/2` on day-of-month runs roughly 3 to 4 times per week.
- Keep model at `gemini-2.0-flash-lite` for cheapest routine classification.
- Batch job prints token estimates (`estimated_input_tokens`, `estimated_output_tokens`, `estimated_total_tokens`) for budget tracking.
- Low-token strategy: compact taxonomy codes, truncated message text, URL cap, strict JSON schema, temperature 0.

Roadmap TODOs for ranking, searchable index, and aggregation are tracked in `project/backend/TODO_ENRICHMENT.md`.

## Engineering Workflow

- Repository working practices are defined in `WORKING_AGREEMENT.md`.
- Top-level categorized TODO navigation is in `TODO_MASTER.md`.
- Keep README updated whenever setup, env variables, or run flows change.