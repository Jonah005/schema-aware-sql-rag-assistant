import os
import uuid
from django.core.management.base import BaseCommand
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct

from app.chatbot_schema import get_schema_and_fks, build_table_chunks
from app.chatbot_retrieval import embed_text


def get_qdrant_client() -> QdrantClient:
    url = os.getenv("QDRANT_URL", "http://127.0.0.1:6333").strip()
    api_key = os.getenv("QDRANT_API_KEY", "").strip() or None
    return QdrantClient(url=url, api_key=api_key)


def stable_uuid_from_key(key: str) -> str:
    """
    Deterministic UUID so reindexing updates same points instead of duplicating.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class Command(BaseCommand):
    help = "Indexes DB schema into Qdrant (persistent). Safe to run multiple times."

    def handle(self, *args, **options):
        client = get_qdrant_client()

        collection = os.getenv("QDRANT_SCHEMA_COLLECTION", "schema_collection").strip()

        # Embedding dimension (must match your embedding model)
        # all-MiniLM-L6-v2 => 384
        dim = int(os.getenv("EMBEDDING_DIM", "384"))

        # Ensure collection exists
        try:
            info = client.get_collection(collection)
            existing_dim = info.config.params.vectors.size  # type: ignore
            if int(existing_dim) != dim:
                raise RuntimeError(
                    f"Qdrant collection '{collection}' has dim={existing_dim} but EMBEDDING_DIM={dim}. "
                    "Either recreate the collection or set EMBEDDING_DIM to match."
                )
        except Exception:
            self.stdout.write(self.style.WARNING(f"Creating Qdrant collection: {collection}"))
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

        # ✅ IMPORTANT FIX:
        # get_schema_and_fks() returns (tables, fks)
        tables, fks = get_schema_and_fks()
        chunks = build_table_chunks(tables, fks)

        self.stdout.write(f"Found {len(chunks)} schema chunks. Indexing into Qdrant...")

        points = []
        for ch in chunks:
            key = ch["key"]  # e.g. "table::storeitem"
            text = ch["text"]

            vec = embed_text(text)

            payload = {
                "key": key,
                "table": ch.get("table"),
                "columns": ch.get("columns", []),
                "text": text,
            }

            pid = stable_uuid_from_key(key)
            points.append(PointStruct(id=pid, vector=vec, payload=payload))

        # Upsert in batches (avoid huge requests)
        batch_size = 128
        for i in range(0, len(points), batch_size):
            client.upsert(collection_name=collection, points=points[i : i + batch_size])

        self.stdout.write(self.style.SUCCESS(f"✅ Indexed {len(points)} schema chunks into '{collection}'"))
