import os
import uuid
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct

from app.chatbot_retrieval import embed_text


def get_qdrant_client() -> QdrantClient:
    url = os.getenv("QDRANT_URL", "http://127.0.0.1:6333").strip()
    api_key = os.getenv("QDRANT_API_KEY", "").strip() or None
    return QdrantClient(url=url, api_key=api_key)


def stable_uuid_from_key(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def ensure_collection(client: QdrantClient, collection: str, dim: int = 384):
    try:
        info = client.get_collection(collection)
        existing_dim = info.config.params.vectors.size  # type: ignore
        if int(existing_dim) != int(dim):
            raise RuntimeError(
                f"Collection '{collection}' dim={existing_dim} but expected dim={dim}."
            )
    except Exception:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


def upsert_intent_mapping(intent_payload: dict) -> str:
    """
    Upserts a developer-approved intent mapping into Qdrant intent collection.
    Returns point_id.
    """
    client = get_qdrant_client()
    collection = os.getenv("QDRANT_INTENT_COLLECTION", "intent_collection").strip()
    dim = int(os.getenv("EMBEDDING_DIM", "384"))

    ensure_collection(client, collection, dim)

    # Build a searchable text for embeddings
    example_q = (intent_payload or {}).get("example_question", "") or ""
    table_hints = (intent_payload or {}).get("tables", []) or []
    col_hints = (intent_payload or {}).get("filter_columns", []) or []

    text = "Intent mapping\n"
    text += f"Example question: {example_q}\n"
    text += f"Tables: {', '.join(table_hints)}\n"
    text += f"Filter columns: {', '.join(col_hints)}\n"

    vec = embed_text(text)

    key = (intent_payload or {}).get("key") or f"intent::{example_q.strip()[:80]}"
    point_id = stable_uuid_from_key(key)

    payload = {
        "key": key,
        "intent": intent_payload,
        "text": text,
    }

    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vec, payload=payload)],
    )
    return point_id
