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

Expected payload:

```json
{
	"text": "hello",
	"sender": "user123",
	"group_id": "group123",
	"timestamp": 1234567890
}
```

### 2. Start Bot

Open a second terminal:

```bash
cd project/bot
node index.js
```

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
- Stored fields are exactly: `text`, `sender`, `group_id`, `timestamp`.