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
	- `sort_by` in `newest|oldest|rank|duplicates`
	- `limit`, `offset`
- Full-text search support for `GET /search`:
	- `q` (query string, required)
	- `group_id`
	- `group_name` (partial match)
	- `sort_by` in `relevance|newest|oldest|rank|duplicates`
	- `merged` (`true|false`) to return one representative per duplicate cluster
	- `limit`, `offset`

Roadmap TODOs for ranking, searchable index, and aggregation are tracked in `project/backend/TODO_ENRICHMENT.md`.

## Engineering Workflow

- Repository working practices are defined in `WORKING_AGREEMENT.md`.
- Top-level categorized TODO navigation is in `TODO_MASTER.md`.
- Keep README updated whenever setup, env variables, or run flows change.