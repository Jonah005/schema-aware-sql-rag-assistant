# Schema-Aware SQL Chatbot (Django + Postgres + Qdrant + Redis + LLM)

This repository contains a **schema-aware, read-only SQL chatbot** for your ERP database.

Users chat in a web UI → backend retrieves the most relevant schema slice (RAG) → an LLM generates a strict **QuerySpec JSON** → backend validates/repairs it against the real schema → compiles it into **safe SQL** → executes **read-only** query → returns a formatted answer.

---

## What this chatbot does

- ✅ Answers natural-language questions by querying your Postgres DB
- ✅ Uses **schema RAG** (Qdrant vector search + lexical + coverage scoring) to keep LLM grounded
- ✅ LLM outputs **strict JSON** (QuerySpec) instead of raw SQL
- ✅ Converts QuerySpec → SQL with FK-aware joins + guardrails
- ✅ Stores chat history + clarification state in **Redis**
- ✅ Optional: publish “approved intent mappings” into Qdrant to improve routing over time

---

## End-to-end request flow

1. **UI** (`chat.html`) sends `POST` JSON `{ message, conversation_id }` to Django `chat_api` with CSRF.
2. **View** (`chatbot_views.py`) validates input and calls:
   - `answer_user_question(request.user, message, session_key=request.session.session_key)`
3. **Engine** (`chatbot_engine.py`):
   - Loads history/state from Redis (`chatbot_memory.py`)
   - Applies history/topic drift gating (prevents old topic hijacking new query)
   - (Optional) resolves ambiguous IDs by probing DB (ID resolver)
   - Retrieves a **schema slice** via Qdrant (`chatbot_retrieval.py`)
   - Calls LLM to generate QuerySpec (`llm_gateway.py`)
   - Validates/repairs spec against schema/types (`chatbot_schema.py`)
   - Builds safe SQL + executes read-only (`chatbot_sql.py`)
   - Formats and returns answer (`llm_gateway.py`)
   - Saves the new turn back into Redis

---

## File guide (what each file does)

### UI + Django views
- **`chat.html`**
  - Frontend chat UI template.
  - Posts to `{% url 'chat_api' %}` with `X-CSRFToken` header.
- **`chatbot_views.py`**
  - `chat_page`: serves UI and sets CSRF cookie (`ensure_csrf_cookie`)
  - `chat_api`: POST endpoint → calls `answer_user_question()` and returns `{reply, conversation_id}`

### Orchestration / brain
- **`chatbot_engine.py`**
  - Main orchestrator: `answer_user_question()`
  - Controls:
    - history gating / topic drift detection
    - table clarification logic (disabled by default via `ENABLE_TABLE_PICKER=0`)
    - ID resolver (optional DB probe)
    - schema retrieval + LLM QuerySpec loop
    - type-aware repair
    - SQL execution and answer formatting

### Memory / session state (Redis)
- **`chatbot_memory.py`**
  - Stores per-user + per-session:
    - message history
    - pending clarification state
  - TTL + max-history trimming

### Schema + docs
- **`chatbot_schema.py`**
  - Introspects DB schema (tables, columns, types, foreign keys)
  - Builds schema chunks for embedding/indexing
  - Provides cached FK edges + types for validation
- **`chatbot_table_docs.py`** + **`table_descriptions.txt`**
  - Loads human descriptions per table (helps retrieval + clarity)
  - IMPORTANT: `chatbot_engine.py` expects this file at:
    - `BASE_DIR/app/resources/table_descriptions.txt`

### Retrieval / RAG (Qdrant)
- **`chatbot_retrieval.py`**
  - Uses SentenceTransformers embeddings + Qdrant vector search
  - Combines signals:
    - vector similarity
    - lexical matching
    - requested-field coverage (via LLM field extraction)
  - Adds bridge tables using FK paths to keep joins possible
  - Also supports “intent_collection” lookups (developer-approved mappings)

### QuerySpec → SQL (safe execution)
- **`chatbot_sql.py`**
  - Converts QuerySpec JSON into safe SQL with quoting/guardrails
  - Uses FK edges to build safe joins
  - Executes read-only SQL

### Qdrant utilities + management commands
- **`reindex_schema.py`**
  - Django management command: embeds schema chunks and indexes them into Qdrant
  - Safe to re-run (stable UUID keys)
- **`publish_intents.py`**
  - Django management command: publishes approved intent mappings from `ChatbotReviewItem`
  - Writes them into Qdrant `intent_collection`
- **`chatbot_qdrant.py`**
  - Helper for intent upserts (older/smaller helper; overlaps with retrieval upsert)

### LLM gateway
- **`llm_gateway.py`**
  - Talks to LLM provider (defaults: Hugging Face Router)
  - Generates strict JSON QuerySpec (`llm_generate_queryspec`)
  - Extracts requested fields (`llm_extract_fields`) used by retrieval
  - Optional LLM answer formatting (`ANSWER_WITH_LLM=1`)
  - NOTE: Contains “anchor date/time” (Dubai) computed at import-time:
    - `ANCHOR_TIMEZONE`, `ANCHOR_NOW`, etc.

### Local services
- **`docker-compose.yml`**
  - Starts only:
    - Qdrant (6333/6334)
    - Redis (6379)
  - Postgres is assumed to be configured externally (or run separately)

### Optional sample DB data
- **`data.json`**
  - Django fixture you can load with `loaddata` (optional)

---

## Required services

You need:
- **Postgres** (your ERP DB; configured in Django `DATABASES`)
- **Redis** (chat memory/state)
- **Qdrant** (schema & intents vector store)
- **LLM access** (Hugging Face Router by default)

---

## Environment variables (.env)

Create a `.env` in your project root (or export env vars).

### Redis
- `REDIS_URL=redis://localhost:6379/0`
- `CHAT_TTL_SECONDS=86400`
- `CHAT_MAX_HISTORY=30`

### Qdrant
- `QDRANT_URL=http://localhost:6333`
- `QDRANT_API_KEY=` (optional)
- `QDRANT_SCHEMA_COLLECTION=schema_collection`
- `QDRANT_INTENT_COLLECTION=intent_collection`
- `EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2`
- `EMBEDDING_DIM=384`

### LLM (Hugging Face Router default)
- `LLM_PROVIDER=hf`
- `HF_TOKEN=...`
- `HF_BASE_URL=https://router.huggingface.co/v1`
- `HF_MODEL=HuggingFaceTB/SmolLM3-3B:hf-inference`

### Engine knobs (optional)
- `SCHEMA_RAG_MIN_SCORE=0.55`
- `ENABLE_TABLE_PICKER=0`
- `ENABLE_ID_RESOLVER=1`
- `HISTORY_MAX_FOR_LLM=12`
- `TOPIC_JACCARD_THRESHOLD=0.18`
- `DEBUG_QUERY_SPEC=1`

### LLM gateway knobs (optional)
- `CHAT_HISTORY_MAX_FOR_LLM=12`
- `ANSWER_WITH_LLM=0`
- `LLM_TIMEOUT_SECONDS=60`
- `LLM_RETRY_MAX=4`

### Table descriptions
Engine reads from:
- `BASE_DIR/app/resources/table_descriptions.txt`

(Optionally used by gateway too)
- `TABLE_DESCRIPTIONS_PATH=/absolute/path/to/table_descriptions.txt`

---

## How to run locally (fresh clone)

### 1) Start Redis + Qdrant (Docker)
From the folder containing `docker-compose.yml`:
'''
docker compose up -d
'''
Verify:

Qdrant: http://localhost:6333

Redis: localhost:6379

2) Install Python dependencies

Create a virtual env and install your project requirements. At minimum you’ll need:

django

redis

qdrant-client

sentence-transformers

requests

python-dotenv (optional)

Example:
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
# or:
pip install django redis qdrant-client sentence-transformers requests python-dotenv


3) Configure Postgres in Django + migrate

Ensure DATABASES is configured in settings.py, then:


python manage.py migrate

Optional: load sample data fixture:
python manage.py loaddata data.json

4) Place table_descriptions.txt where the engine expects it

Create this path in your project if it doesn’t exist:

<BASE_DIR>/app/resources/table_descriptions.txt


Put your table descriptions file there.

5) Index schema into Qdrant (REQUIRED)

This builds the schema vector index used by retrieval:

python manage.py reindex_schema


Run this whenever schema changes (migrations / new tables / new columns).

6) Create a user and run server (views require login)

python manage.py createsuperuser
python manage.py runserver





7) Ensure URLs are wired (example)

Your chat.html uses {% url 'chat_api' %} so your urls must define a chat_api name.

Example urls.py:

from django.urls import path
from app.chatbot_views import chat_page, chat_api

urlpatterns = [
    path("chat/", chat_page, name="chat_page"),
    path("api/chat/", chat_api, name="chat_api"),
]


Now open:

http://127.0.0.1:8000/chat/

Publishing approved intents (optional)

If you use a ChatbotReviewItem model and approve intent payloads, publish them to Qdrant:
python manage.py publish_intents --limit 100

This:

reads approved items from DB (status="approved", kind="clarification")

upserts to Qdrant intent_collection

marks them as published to avoid duplicates

Troubleshooting
Qdrant dimension mismatch error during reindex_schema

If you changed embedding model/dim:

Either delete/recreate the Qdrant collection, OR

Set EMBEDDING_DIM to match the existing collection.

First run is slow

SentenceTransformer model loads/downloads on first run. Normal.

Retrieval feels “empty” / bad table selection

Confirm python manage.py reindex_schema completed successfully.

Confirm Qdrant is reachable at QDRANT_URL.

Confirm your DB schema is accessible (permissions to read schema metadata).
