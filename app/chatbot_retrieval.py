import os
import uuid
import re
from typing import Any, Dict, List, Optional, Tuple, Set

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from sentence_transformers import SentenceTransformer

from .chatbot_schema import (
    get_schema_and_fks,
    get_schema_lexical_index,
    get_fk_edges,            # ✅ NEW (cached JSON-safe FK edges)
    tokenize_free_text,
    ForeignKey,
)
from .llm_gateway import llm_extract_fields  # ✅ field extraction step


# =========================
# Config (single source)
# =========================
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None

SCHEMA_COLLECTION = os.getenv("QDRANT_SCHEMA_COLLECTION", "schema_collection").strip()
INTENT_COLLECTION = os.getenv("QDRANT_INTENT_COLLECTION", "intent_collection").strip()

EMBED_MODEL_NAME = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2").strip()

SCHEMA_MIN_SCORE = float(os.getenv("SCHEMA_RAG_MIN_SCORE", "0.55"))
INTENT_MIN_SCORE = float(os.getenv("INTENT_MIN_SCORE", "0.35"))

# ✅ "strong intent" threshold (prevents generic intents hijacking)
INTENT_STRONG_SCORE = float(os.getenv("INTENT_STRONG_SCORE", "0.55"))

SCHEMA_TOP_K = int(os.getenv("SCHEMA_TOP_K", "6"))
INTENT_TOP_K = int(os.getenv("INTENT_TOP_K", "3"))

# ✅ NEW: always include some top RAG candidates in the schema slice
INCLUDE_TOP_RAG_TABLES = os.getenv("INCLUDE_TOP_RAG_TABLES", "1").strip() in ("1", "true", "True", "yes", "YES")
TOP_RAG_TABLES_TO_INCLUDE = int(os.getenv("TOP_RAG_TABLES_TO_INCLUDE", "6"))

# ✅ NEW: if coverage misses some requested fields, do targeted retrieval per uncovered field
USE_UNCOVERED_FIELD_RAG = os.getenv("USE_UNCOVERED_FIELD_RAG", "1").strip() in ("1", "true", "True", "yes", "YES")
UNCOVERED_FIELD_MAX = int(os.getenv("UNCOVERED_FIELD_MAX", "3"))
UNCOVERED_FIELD_TOPK = int(os.getenv("UNCOVERED_FIELD_TOPK", "2"))

# ✅ Schema-grounded resolver knobs
USE_FIELD_EXTRACT = os.getenv("USE_FIELD_EXTRACT", "1").strip() in ("1", "true", "True", "yes", "YES")
MAX_BASE_TABLES = int(os.getenv("MAX_BASE_TABLES", "4"))
FIELD_MATCH_THRESHOLD = float(os.getenv("FIELD_MATCH_THRESHOLD", "0.34"))  # jaccard-ish
MAX_BRIDGE_TABLES = int(os.getenv("MAX_BRIDGE_TABLES", "4"))               # path intermediates cap
MAX_NEIGHBOR_EXTRAS = int(os.getenv("MAX_NEIGHBOR_EXTRAS", "2"))           # keep slice tight
MAX_FINAL_TABLES = int(os.getenv("MAX_FINAL_TABLES", "12"))                # hard cap for safety/latency

# ✅ Hybrid scoring weights (Qdrant is a signal, not hard routing)
W_VECTOR = float(os.getenv("W_VECTOR", "0.45"))
W_LEXICAL = float(os.getenv("W_LEXICAL", "0.35"))
W_COVERAGE = float(os.getenv("W_COVERAGE", "0.20"))

# ✅ Optional: table description file (helps lexical scoring + ranking)
TABLE_DESCRIPTIONS_PATH = os.getenv("TABLE_DESCRIPTIONS_PATH", "").strip()

_embedder: Optional[SentenceTransformer] = None
_client: Optional[QdrantClient] = None

_TABLE_DESC_CACHE: Optional[Dict[str, str]] = None


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL_NAME)
    return _embedder


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=60)
    return _client


def qdrant_search_compat(
    client: QdrantClient,
    *,
    collection_name: str,
    query_vector: List[float],
    limit: int,
    with_payload: bool = True,
):
    """
    Compatibility layer across qdrant-client versions.
    Returns a list of points with `.payload` and `.score` when possible.
    """
    if hasattr(client, "search"):
        return client.search(
            collection_name=collection_name,
            query_vector=query_vector,
            limit=limit,
            with_payload=with_payload,
        )

    if hasattr(client, "search_points"):
        fn = getattr(client, "search_points")
        try:
            return fn(
                collection_name=collection_name,
                query_vector=query_vector,
                limit=limit,
                with_payload=with_payload,
            )
        except TypeError:
            try:
                return fn(
                    collection_name=collection_name,
                    vector=query_vector,
                    limit=limit,
                    with_payload=with_payload,
                )
            except TypeError:
                return fn(collection_name, query_vector, limit=limit, with_payload=with_payload)

    if hasattr(client, "query_points"):
        try:
            res = client.query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=limit,
                with_payload=with_payload,
            )
        except TypeError:
            res = client.query_points(
                collection_name=collection_name,
                query_vector=query_vector,
                limit=limit,
                with_payload=with_payload,
            )
        return getattr(res, "points", res)

    raise RuntimeError("Unsupported qdrant-client version: cannot perform vector search")


def embed_text(text: str) -> List[float]:
    emb = _get_embedder().encode([text or ""], normalize_embeddings=True)[0]
    return emb.tolist() if hasattr(emb, "tolist") else list(emb)


def _ensure_collection(name: str) -> None:
    client = _get_client()
    dim = int(_get_embedder().get_sentence_embedding_dimension())

    try:
        info = client.get_collection(name)
        try:
            vsize = info.config.params.vectors.size  # type: ignore
            if int(vsize) != dim:
                raise RuntimeError(
                    f"Qdrant collection '{name}' vector size={vsize} != embedder dim={dim}. "
                    "Recreate the collection OR use the matching embedding model."
                )
        except Exception:
            pass
        return
    except Exception:
        client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )


def ensure_collections() -> None:
    _ensure_collection(SCHEMA_COLLECTION)
    _ensure_collection(INTENT_COLLECTION)


def _table_from_payload(payload: Dict[str, Any]) -> str:
    for k in ("table", "table_name", "name"):
        v = payload.get(k)
        if v:
            return str(v)
    return ""


# =========================
# Table descriptions (optional)
# =========================
def _load_table_descriptions() -> Dict[str, str]:
    """
    Loads app/resources/table_descriptions.txt if available.
    Format: table_name: description
    Safe: never raises.
    """
    global _TABLE_DESC_CACHE
    if _TABLE_DESC_CACHE is not None:
        return _TABLE_DESC_CACHE

    out: Dict[str, str] = {}
    try:
        path = TABLE_DESCRIPTIONS_PATH
        if not path:
            # Default to your app structure (safe import)
            try:
                from django.conf import settings  # type: ignore
                path = os.path.join(getattr(settings, "BASE_DIR", ""), "app", "resources", "table_descriptions.txt")
            except Exception:
                path = ""

        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = (line or "").strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    k = (k or "").strip()
                    v = (v or "").strip()
                    if k and v:
                        out[k] = v
    except Exception:
        out = {}

    _TABLE_DESC_CACHE = out
    return out


def _desc_tokens_for_table(table: str) -> List[str]:
    desc = (_load_table_descriptions().get(table) or "").strip()
    return tokenize_free_text(desc) if desc else []


# =========================
# Retrieval
# =========================
def search_intent(question: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Searches intent_collection for a developer-approved mapping.
    Returns (payload_or_None, meta)
    """
    ensure_collections()
    client = _get_client()
    vec = embed_text(question)

    hits = qdrant_search_compat(
        client,
        collection_name=INTENT_COLLECTION,
        query_vector=vec,
        limit=INTENT_TOP_K,
        with_payload=True,
    )

    candidates: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    best_score: float = 0.0

    for h in hits or []:
        payload = getattr(h, "payload", None) or {}
        score = float(getattr(h, "score", 0.0) or 0.0)
        item = {"score": score, "payload": payload, "id": str(getattr(h, "id", ""))}
        candidates.append(item)
        if score > best_score:
            best_score = score
            best = item

    meta = {
        "best_score": best_score,
        "candidates": candidates,
        "used": bool(best and best_score >= INTENT_MIN_SCORE),
        "threshold": INTENT_MIN_SCORE,
        "strong_threshold": INTENT_STRONG_SCORE,
        "collection": INTENT_COLLECTION,
    }

    if best and best_score >= INTENT_MIN_SCORE:
        return best["payload"], meta
    return None, meta


def search_schema(question: str) -> Tuple[List[Dict[str, Any]], float]:
    """
    Searches schema_collection for table/column chunks.
    Returns (ordered_best_per_table, best_score)
    """
    ensure_collections()
    client = _get_client()
    vec = embed_text(question)

    hits = qdrant_search_compat(
        client,
        collection_name=SCHEMA_COLLECTION,
        query_vector=vec,
        limit=SCHEMA_TOP_K,
        with_payload=True,
    )

    candidates: List[Dict[str, Any]] = []
    best_score = 0.0

    for h in hits or []:
        payload = getattr(h, "payload", None) or {}
        score = float(getattr(h, "score", 0.0) or 0.0)
        best_score = max(best_score, score)
        candidates.append(
            {
                "table": _table_from_payload(payload),
                "score": score,
                "payload": payload,
                "id": str(getattr(h, "id", "")),
            }
        )

    best_by_table: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        t = (c.get("table") or "").strip()
        if not t:
            continue
        if t not in best_by_table or float(c["score"]) > float(best_by_table[t]["score"]):
            best_by_table[t] = c

    ordered = sorted(best_by_table.values(), key=lambda x: float(x["score"]), reverse=True)
    return ordered, best_score


# =========================
# Schema-grounded selection (Field coverage + join graph)
# =========================
_STOP = {
    "the", "a", "an", "of", "to", "for", "and", "or", "with", "show", "me", "give", "get",
    "latest", "last", "top", "recent", "all", "list", "please", "in", "on", "at", "by",
    "from", "as", "is", "are", "was", "were", "be", "been", "this", "that", "these", "those",
}

_TOKEN_EXPAND: Dict[str, Set[str]] = {
    "po": {"purchase", "order"},
    "so": {"sales", "order"},
    "no": {"number"},
    "num": {"number"},
    "qty": {"quantity"},
    "dt": {"date"},
    "amt": {"amount"},
    "desc": {"description"},
    "uom": {"unit", "measure"},
    "ref": {"reference"},
}

def _expand_tokens(tokens: Set[str]) -> Set[str]:
    out = set(tokens or set())
    for t in list(tokens or set()):
        exp = _TOKEN_EXPAND.get(t)
        if exp:
            out |= set(exp)
    return out


def _token_set_free_text(s: str) -> Set[str]:
    toks = tokenize_free_text(s or "")
    base = {t for t in toks if t and t not in _STOP}
    return _expand_tokens(base)


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return float(inter) / float(union) if union else 0.0


def _best_column_match_score(field_phrase: str, columns: List[str]) -> float:
    ft = _token_set_free_text(field_phrase)
    if not ft or not columns:
        return 0.0
    best = 0.0
    for c in columns:
        ct = _token_set_free_text(c)
        best = max(best, _jaccard(ft, ct))
        if best >= 0.999:
            return best
    return best


def _coverage_for_table(requested_fields: List[str], columns: List[str]) -> Tuple[int, Dict[str, float]]:
    per: Dict[str, float] = {}
    hit = 0
    for f in requested_fields or []:
        sc = _best_column_match_score(f, columns)
        per[f] = sc
        if sc >= FIELD_MATCH_THRESHOLD:
            hit += 1
    return hit, per


def _intent_tables_have_coverage(
    *,
    intent_tables: List[str],
    requested_fields: List[str],
    tables_all: Dict[str, List[str]],
) -> bool:
    if not intent_tables:
        return False
    if not requested_fields:
        return True
    best = 0
    for t in intent_tables:
        cols = tables_all.get(t) or []
        cnt, _ = _coverage_for_table(requested_fields, cols)
        best = max(best, cnt)
    return best > 0


def _build_adj(fks: List[ForeignKey]) -> Dict[str, Set[str]]:
    adj: Dict[str, Set[str]] = {}
    for fk in fks:
        adj.setdefault(fk.table, set()).add(fk.ref_table)
        adj.setdefault(fk.ref_table, set()).add(fk.table)
    return adj


def _shortest_path(adj: Dict[str, Set[str]], start: str, goal: str) -> List[str]:
    start = (start or "").strip()
    goal = (goal or "").strip()
    if not start or not goal:
        return []
    if start == goal:
        return [start]

    from collections import deque

    q = deque([start])
    prev: Dict[str, Optional[str]] = {start: None}

    while q:
        cur = q.popleft()
        if cur == goal:
            break
        for nb in adj.get(cur, set()):
            if nb not in prev:
                prev[nb] = cur
                q.append(nb)

    if goal not in prev:
        return []

    path = [goal]
    cur = goal
    while prev.get(cur) is not None:
        cur = prev[cur]  # type: ignore
        path.append(cur)
    path.reverse()
    return path


def _neighbors_for_tables(tables: List[str], fks: List[ForeignKey], max_extra: int = 2) -> List[str]:
    wanted = set(tables)
    extras: List[str] = []

    for fk in fks:
        if fk.table in wanted and fk.ref_table not in wanted:
            extras.append(fk.ref_table)
        if fk.ref_table in wanted and fk.table not in wanted:
            extras.append(fk.table)

    seen: Set[str] = set()
    out: List[str] = []
    for t in extras:
        if t in seen or t in wanted:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_extra:
            break
    return out


def _coerce_str_list(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return [str(v).strip() for v in x if str(v).strip()]
    if isinstance(x, str) and x.strip():
        return [x.strip()]
    return []


def _extract_requested_fields(field_extraction: Dict[str, Any]) -> List[str]:
    if not isinstance(field_extraction, dict):
        return []
    for k in ("requested_fields", "fields", "output_fields", "columns_wanted"):
        vals = field_extraction.get(k)
        out = _coerce_str_list(vals)
        if out:
            return out
    return []


def _extract_extra_query_terms(field_extraction: Dict[str, Any]) -> List[str]:
    if not isinstance(field_extraction, dict):
        return []

    extra: List[str] = []

    ent = field_extraction.get("entities")
    if isinstance(ent, dict):
        for k, v in ent.items():
            if v is None:
                continue
            vs = str(v).strip()
            if vs:
                extra.append(vs)
            ks = str(k).strip()
            if ks:
                extra.append(ks)

    for k in ("filters_text", "filters", "keywords", "hints"):
        extra.extend(_coerce_str_list(field_extraction.get(k)))

    seen: Set[str] = set()
    out: List[str] = []
    for t in extra:
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        out.append(t)
    return out


# ✅ NEW: heuristic fallback so “customer name / PO number / qty / price” is not missed
_HEURISTIC_FIELD_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bcustomer\s*name\b", re.I), "customer name"),
    (re.compile(r"\bcustomer\b", re.I), "customer"),
    (re.compile(r"\bpo\s*(number|no\.?|#)\b", re.I), "po number"),
    (re.compile(r"\bpurchase\s*order\s*(number|no\.?|#)\b", re.I), "po number"),
    (re.compile(r"\bpo\s*date\b", re.I), "po date"),
    (re.compile(r"\bstatus\b", re.I), "status"),
    (re.compile(r"\bquantity\b", re.I), "quantity"),
    (re.compile(r"\bqty\b", re.I), "qty"),
    (re.compile(r"\bprice\b", re.I), "price"),
    (re.compile(r"\btotal\s*order\s*value\b", re.I), "total order value"),
    (re.compile(r"\border\s*value\b", re.I), "order value"),
    (re.compile(r"\bcreated\s*(at|date|time)?\b", re.I), "created at"),
]


def _heuristic_fields_from_question(question: str) -> List[str]:
    q = (question or "").strip()
    if not q:
        return []
    out: List[str] = []
    for rx, label in _HEURISTIC_FIELD_PATTERNS:
        if rx.search(q):
            out.append(label)
    # de-dup preserving order
    seen: Set[str] = set()
    dedup: List[str] = []
    for x in out:
        xl = x.lower()
        if xl in seen:
            continue
        seen.add(xl)
        dedup.append(x)
    return dedup


def _lexical_score_for_table(question_tokens: Set[str], table_keywords: List[str], desc_tokens: List[str]) -> float:
    if not question_tokens:
        return 0.0
    tk = {t for t in (table_keywords or []) if t and t not in _STOP}
    dt = {t for t in (desc_tokens or []) if t and t not in _STOP}
    merged = tk | dt
    return _jaccard(question_tokens, merged)


def _choose_tables_by_coverage(
    *,
    tables_all: Dict[str, List[str]],
    requested_fields: List[str],
    anchors: List[str],
    vector_scores: Dict[str, float],
    lexical_scores: Dict[str, float],
    fks: List[ForeignKey],
    max_tables: int,
) -> Tuple[List[str], Dict[str, Any]]:
    debug: Dict[str, Any] = {
        "requested_fields": requested_fields,
        "anchors": anchors,
        "table_scores": {},
        "uncovered_fields": [],
        "selected": [],
        "coverage_ratio": 0.0,
    }

    if not requested_fields:
        out = [t for t in anchors if t in tables_all][:max_tables]
        debug["selected"] = out
        debug["coverage_ratio"] = 0.0
        return out, debug

    coverage_map: Dict[str, Tuple[int, Dict[str, float]]] = {}
    total_fields = max(1, len(requested_fields))

    for t, cols in tables_all.items():
        cnt, per = _coverage_for_table(requested_fields, cols)
        coverage_map[t] = (cnt, per)

        frac = float(cnt) / float(total_fields) if total_fields else 0.0
        vec = float(vector_scores.get(t, 0.0) or 0.0)
        lex = float(lexical_scores.get(t, 0.0) or 0.0)
        hy = (W_VECTOR * vec) + (W_LEXICAL * lex) + (W_COVERAGE * frac)

        debug["table_scores"][t] = {
            "coverage": cnt,
            "coverage_frac": frac,
            "vector": vec,
            "lexical": lex,
            "hybrid": hy,
        }

    selected: List[str] = []
    for t in anchors:
        if t in tables_all and t not in selected:
            selected.append(t)

    def _is_field_covered_by_selected(field: str) -> bool:
        for t in selected:
            _, per = coverage_map.get(t, (0, {}))
            if float(per.get(field, 0.0) or 0.0) >= FIELD_MATCH_THRESHOLD:
                return True
        return False

    remaining = [f for f in requested_fields if not _is_field_covered_by_selected(f)]
    adj = _build_adj(fks)

    while remaining and len(selected) < max_tables:
        best_t: Optional[str] = None
        best_gain = -1
        best_hybrid = -1.0
        best_dist = 10**9

        for t in tables_all.keys():
            if t in selected:
                continue

            _, per = coverage_map.get(t, (0, {}))
            gain = sum(1 for f in remaining if float(per.get(f, 0.0) or 0.0) >= FIELD_MATCH_THRESHOLD)
            if gain <= 0:
                continue

            hy = float((debug["table_scores"].get(t) or {}).get("hybrid") or 0.0)

            if selected:
                dists = []
                for s in selected:
                    path = _shortest_path(adj, s, t)
                    if path:
                        dists.append(len(path) - 1)
                dist = min(dists) if dists else 9999
            else:
                dist = 0

            if (
                gain > best_gain
                or (gain == best_gain and hy > best_hybrid)
                or (gain == best_gain and abs(hy - best_hybrid) < 1e-9 and dist < best_dist)
            ):
                best_t = t
                best_gain = gain
                best_hybrid = hy
                best_dist = dist

        if not best_t:
            break

        selected.append(best_t)
        remaining = [f for f in remaining if not _is_field_covered_by_selected(f)]

    coverage_ratio = 1.0
    if requested_fields:
        coverage_ratio = 1.0 - (float(len(remaining)) / float(len(requested_fields)))

    debug["selected"] = selected[:]
    debug["uncovered_fields"] = remaining[:]
    debug["coverage_ratio"] = max(0.0, min(1.0, coverage_ratio))
    return selected[:max_tables], debug


def _rank_schema_candidates(
    *,
    schema_candidates: List[Dict[str, Any]],
    vector_scores: Dict[str, float],
    lexical_scores: Dict[str, float],
    coverage_debug: Dict[str, Any],
) -> List[Dict[str, Any]]:
    table_scores = (coverage_debug or {}).get("table_scores") if isinstance((coverage_debug or {}).get("table_scores"), dict) else {}

    ranked: List[Dict[str, Any]] = []
    for c in schema_candidates or []:
        t = (c.get("table") or "").strip()
        if not t:
            continue
        vec = float(vector_scores.get(t, c.get("score") or 0.0) or 0.0)
        lex = float(lexical_scores.get(t, 0.0) or 0.0)
        cov_frac = float(((table_scores.get(t) or {}).get("coverage_frac") or 0.0) or 0.0)
        hy = (W_VECTOR * vec) + (W_LEXICAL * lex) + (W_COVERAGE * cov_frac)
        ranked.append({"table": t, "score": hy, "vector": vec, "lexical": lex, "coverage_frac": cov_frac})

    ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    return ranked


def get_schema_slice(question: str, forced_tables: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Returns:
      {
        "schema": {table: [columns...]},
        "retrieval": {...debug...}
      }
    """
    tables_all, fks = get_schema_and_fks()
    forced_tables = [t for t in (forced_tables or []) if t in tables_all]

    schema_lex = get_schema_lexical_index() or {}
    fk_edges_all = get_fk_edges() or []  # ✅ cached JSON-safe edges

    # 1) Intent mapping
    intent_hint, intent_meta = search_intent(question)

    # 2) Field extraction (schema-agnostic)
    field_extraction: Dict[str, Any] = {"requested_fields": [], "constraints": {}, "entities": {}}
    if USE_FIELD_EXTRACT:
        try:
            field_extraction = llm_extract_fields(question, history=None) or field_extraction
        except Exception as e:
            field_extraction = {"requested_fields": [], "constraints": {}, "entities": {}, "error": str(e)}

    requested_fields = _extract_requested_fields(field_extraction)

    # ✅ Heuristic fallback (prevents “customer name” being missed)
    heur = _heuristic_fields_from_question(question)
    for h in heur:
        if h not in requested_fields:
            requested_fields.append(h)
    if heur and isinstance(field_extraction, dict):
        field_extraction["requested_fields"] = requested_fields

    extra_terms = _extract_extra_query_terms(field_extraction)

    # 3) Qdrant schema search (augment)
    augmented_query = question
    if requested_fields:
        augmented_query += "\nrequested_fields: " + ", ".join(requested_fields)
    if extra_terms:
        augmented_query += "\nextra_terms: " + ", ".join(extra_terms)

    schema_candidates_raw, schema_best_raw = search_schema(augmented_query)

    vector_scores: Dict[str, float] = {}
    for c in schema_candidates_raw:
        t = (c.get("table") or "").strip()
        if t:
            vector_scores[t] = float(c.get("score") or 0.0)

    # 4) Anchors (forced_tables > intent tables)
    anchors: List[str] = []
    used_intent = False
    intent_rejected_reason: Optional[str] = None

    intent_best = float(intent_meta.get("best_score") or 0.0)
    intent_is_strong = intent_best >= max(INTENT_MIN_SCORE, INTENT_STRONG_SCORE)

    if intent_hint and intent_meta.get("used") and intent_is_strong:
        hint_tables = intent_hint.get("tables") or intent_hint.get("table") or []
        if isinstance(hint_tables, str):
            hint_tables = [hint_tables]
        hint_tables = [t for t in hint_tables if t in tables_all]

        if hint_tables and _intent_tables_have_coverage(
            intent_tables=hint_tables, requested_fields=requested_fields, tables_all=tables_all
        ):
            anchors = hint_tables[:]
            used_intent = True
        else:
            used_intent = False
            anchors = []
            intent_rejected_reason = "intent tables do not cover requested_fields (or none valid)"

    elif intent_hint and intent_meta.get("used") and not intent_is_strong:
        intent_rejected_reason = f"intent score {intent_best:.3f} below strong threshold {INTENT_STRONG_SCORE:.3f}"

    if forced_tables:
        anchors = forced_tables[:]
        used_intent = False
        intent_rejected_reason = None

    # 5) Lexical scores (+ table_descriptions tokens)
    q_tokens = _token_set_free_text(question)
    lexical_scores: Dict[str, float] = {}
    for t in tables_all.keys():
        kw = (schema_lex.get(t) or {}).get("all_keywords") or []
        desc_toks = _desc_tokens_for_table(t)
        lexical_scores[t] = _lexical_score_for_table(q_tokens, kw, desc_toks)

    # 6) Choose base tables (coverage)
    base_tables: List[str] = []
    coverage_debug: Dict[str, Any] = {}

    if requested_fields:
        base_tables, coverage_debug = _choose_tables_by_coverage(
            tables_all=tables_all,
            requested_fields=requested_fields,
            anchors=anchors,
            vector_scores=vector_scores,
            lexical_scores=lexical_scores,
            fks=fks,
            max_tables=MAX_BASE_TABLES,
        )

    # fallback when no fields or no coverage
    if not base_tables:
        base_tables = anchors[:]
    if not base_tables:
        for c in schema_candidates_raw[:3]:
            t = (c.get("table") or "").strip()
            if t and t in tables_all and t not in base_tables:
                base_tables.append(t)

    # 7) Bridge tables (connect base tables)
    adj = _build_adj(fks)
    bridge_tables: List[str] = []

    root = (anchors[0] if anchors else (base_tables[0] if base_tables else "")).strip()
    if root and len(base_tables) >= 2:
        for t in base_tables:
            if t == root:
                continue
            path = _shortest_path(adj, root, t)
            if len(path) >= 3:
                mids = path[1:-1]
                for m in mids:
                    if m not in bridge_tables and m not in base_tables:
                        bridge_tables.append(m)
                        if len(bridge_tables) >= MAX_BRIDGE_TABLES:
                            break
            if len(bridge_tables) >= MAX_BRIDGE_TABLES:
                break

    # 8) If coverage missed fields, do targeted retrieval for uncovered fields
    uncovered_field_tables_added: List[str] = []
    uncovered_field_search_debug: List[Dict[str, Any]] = []

    uncovered = (coverage_debug or {}).get("uncovered_fields") or []
    if USE_UNCOVERED_FIELD_RAG and requested_fields and isinstance(uncovered, list) and uncovered:
        for f in uncovered[:UNCOVERED_FIELD_MAX]:
            f = str(f).strip()
            if not f:
                continue
            f_query = f"{question}\nmissing_field: {f}"
            cand_f, _ = search_schema(f_query)
            picked: List[str] = []
            for c in (cand_f or [])[:UNCOVERED_FIELD_TOPK]:
                tt = (c.get("table") or "").strip()
                if tt and tt in tables_all and tt not in picked:
                    picked.append(tt)
                    if tt not in uncovered_field_tables_added:
                        uncovered_field_tables_added.append(tt)
            if picked:
                uncovered_field_search_debug.append({"field": f, "tables": picked})

    # 9) Always include some top RAG candidate tables
    rag_top_tables: List[str] = []
    if INCLUDE_TOP_RAG_TABLES:
        for c in schema_candidates_raw[: max(0, TOP_RAG_TABLES_TO_INCLUDE)]:
            t = (c.get("table") or "").strip()
            if t and t in tables_all and t not in rag_top_tables:
                rag_top_tables.append(t)

    # 10) Neighbor extras (only when unsure / missing coverage)
    prelim = base_tables + bridge_tables

    coverage_ratio = float((coverage_debug or {}).get("coverage_ratio") or 0.0)
    uncovered_now = (coverage_debug or {}).get("uncovered_fields") or []
    need_neighbors = (not requested_fields) or (coverage_ratio < 0.75) or (len(uncovered_now) > 0)

    neighbor_extras: List[str] = []
    if need_neighbors and MAX_NEIGHBOR_EXTRAS > 0:
        neighbor_extras = _neighbors_for_tables(prelim, fks, max_extra=MAX_NEIGHBOR_EXTRAS)

    # 11) Final tables order (dedup + hard cap)
    final_tables: List[str] = []
    seen: Set[str] = set()

    def _push_list(items: List[str]) -> None:
        nonlocal final_tables, seen
        for t in items or []:
            if t in tables_all and t not in seen:
                seen.add(t)
                final_tables.append(t)
                if len(final_tables) >= MAX_FINAL_TABLES:
                    return

    _push_list(anchors)
    _push_list(base_tables)
    _push_list(bridge_tables)
    _push_list(uncovered_field_tables_added)
    _push_list(rag_top_tables)
    _push_list(neighbor_extras)

    schema_slice = {t: tables_all[t] for t in final_tables if t in tables_all}

    # ✅ FK edges filtered to slice (JSON-safe)
    table_set = set(schema_slice.keys())
    fk_edges: List[Dict[str, Any]] = [
        e for e in (fk_edges_all or [])
        if (e.get("table") in table_set and e.get("ref_table") in table_set)
    ]

    # 12) Effective score for engine gating
    max_lex_sel = 0.0
    for t in final_tables:
        max_lex_sel = max(max_lex_sel, float(lexical_scores.get(t, 0.0) or 0.0))

    resolver_score = (0.65 * float(coverage_ratio or 0.0)) + (0.35 * float(max_lex_sel or 0.0))
    schema_best_effective = max(float(schema_best_raw or 0.0), float(resolver_score or 0.0))

    # Better candidate ranking for UI/engine (hybrid)
    schema_candidates_ranked = _rank_schema_candidates(
        schema_candidates=schema_candidates_raw,
        vector_scores=vector_scores,
        lexical_scores=lexical_scores,
        coverage_debug=coverage_debug,
    )

    retrieval = {
        "intent": intent_meta,
        "intent_hint": intent_hint if used_intent else None,
        "intent_rejected_reason": intent_rejected_reason,
        "schema_best_score": schema_best_effective,
        "schema_best_score_raw": float(schema_best_raw or 0.0),
        "resolver_score": float(resolver_score or 0.0),
        "schema_threshold": SCHEMA_MIN_SCORE,

        "schema_candidates": [
            {
                "table": c.get("table"),
                "score": c.get("score"),
                "vector": c.get("vector"),
                "lexical": c.get("lexical"),
                "coverage_frac": c.get("coverage_frac"),
            }
            for c in schema_candidates_ranked
        ],

        "selected_tables": list(schema_slice.keys()),
        "fk_edges": fk_edges,  # ✅ critical for join reasoning
        "field_extraction": field_extraction,
        "requested_fields": requested_fields,
        "extra_terms": extra_terms,
        "coverage_debug": coverage_debug,
        "bridge_tables_added": bridge_tables,
        "neighbor_extras_added": neighbor_extras,

        "rag_top_tables_included": rag_top_tables,
        "uncovered_field_tables_added": uncovered_field_tables_added,
        "uncovered_field_searches": uncovered_field_search_debug,

        "signals_preview": [
            {
                "table": t,
                "vector": float(vector_scores.get(t, 0.0) or 0.0),
                "lexical": float(lexical_scores.get(t, 0.0) or 0.0),
                "coverage_frac": float(
                    (((coverage_debug or {}).get("table_scores", {}) or {}).get(t, {}) or {}).get("coverage_frac", 0.0)
                    or 0.0
                ),
            }
            for t in list(schema_slice.keys())[:10]
        ],
    }

    return {"schema": schema_slice, "retrieval": retrieval}


# =========================
# Intent upsert (developer-approved)
# =========================
def upsert_intent_mapping(mapping: Dict[str, Any], point_id: Optional[Any] = None) -> str:
    """
    Upserts a developer-approved mapping into intent_collection.
    Expected fields (recommended):
      - example_question (or question)
      - tables: [..]
      - filter_columns: [..]
      - output_columns: [..]
      - join_hints: [..]
    """
    ensure_collections()
    client = _get_client()

    example = (mapping.get("example_question") or mapping.get("question") or "").strip()
    if not example:
        raise ValueError("intent mapping must include example_question/question")

    vec = embed_text(example)
    payload = dict(mapping)
    payload["example_question"] = example
    payload["kind"] = payload.get("kind") or "intent_mapping"

    pid = point_id if point_id is not None else str(uuid.uuid4())

    client.upsert(
        collection_name=INTENT_COLLECTION,
        points=[qm.PointStruct(id=pid, vector=vec, payload=payload)],
    )
    return str(pid)
