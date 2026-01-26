import os
import time
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple, Any, Set, Optional

from django.db import connection

# =========================
# Cache
# =========================
_SCHEMA_CACHE_TTL_SECONDS = int(os.getenv("SCHEMA_CACHE_TTL_SECONDS", "3600"))
_DB_SCHEMA_NAME = os.getenv("DB_SCHEMA", "public").strip() or "public"

# ✅ optional app-level excludes (generic, configurable)
# - EXCLUDE_TABLES="app_chatbotreviewitem,app_schemaembedding"
# - EXCLUDE_TABLE_REGEX=".*(review|embedding).*"
_EXCLUDE_TABLES = {
    t.strip() for t in (os.getenv("EXCLUDE_TABLES", "") or "").split(",") if t.strip()
}
_EXCLUDE_TABLE_REGEX = (os.getenv("EXCLUDE_TABLE_REGEX", "") or "").strip()
_EXCLUDE_TABLE_RE = re.compile(_EXCLUDE_TABLE_REGEX, re.IGNORECASE) if _EXCLUDE_TABLE_REGEX else None

# Cache stores: tables + types + fks + fk_edges (JSON-safe) + lexical index
_schema_cache: Dict[str, Any] = {
    "ts": 0.0,
    "tables": None,   # Dict[str, List[str]]
    "types": None,    # Dict[str, Dict[str, str]]  <-- NEW
    "fks": None,      # List[ForeignKey]
    "fk_edges": None,
    "lex": None,
}


@dataclass(frozen=True)
class ForeignKey:
    table: str
    column: str
    ref_table: str
    ref_column: str


SYSTEM_TABLE_PREFIXES = ("django_", "auth_", "sessions_", "admin_", "contenttypes_", "pg_")


def _is_system_table(table: str) -> bool:
    t = (table or "").lower()
    return t.startswith(SYSTEM_TABLE_PREFIXES)


def _is_excluded_table(table: str) -> bool:
    """
    Generic exclusion hook so internal/utility tables don't pollute RAG selection.
    Controlled by env vars; no hardcoding.
    """
    t = (table or "").strip()
    if not t:
        return True
    if _is_system_table(t):
        return True
    if t in _EXCLUDE_TABLES:
        return True
    if _EXCLUDE_TABLE_RE and _EXCLUDE_TABLE_RE.match(t):
        return True
    return False


# =========================
# Type normalization
# =========================
def _normalize_db_type(data_type: Optional[str], udt_name: Optional[str]) -> str:
    """
    Normalize Postgres information_schema column types into stable buckets.
    This is used for value/type checking (int vs text vs date etc.)
    """
    dt = (data_type or "").strip().lower()
    udt = (udt_name or "").strip().lower()

    # Arrays show up as data_type='ARRAY' with udt_name like '_int4'
    if dt == "array":
        elem = udt[1:] if udt.startswith("_") else udt
        elem = elem or "unknown"
        return f"{elem}[]"

    # Common canonical buckets
    if dt in ("smallint", "integer", "bigint"):
        return "int"
    if dt in ("numeric", "decimal", "real", "double precision"):
        return "numeric"
    if dt in ("character varying", "character", "varchar", "char", "text"):
        return "text"
    if dt in ("boolean",):
        return "bool"
    if dt in ("date",):
        return "date"
    if dt.startswith("timestamp"):
        return "timestamp"
    if dt.startswith("time"):
        return "time"
    if dt in ("uuid",):
        return "uuid"

    # fallback to raw dt, then udt
    return dt or udt or "unknown"


# =========================
# Normalization / tokenization helpers
# =========================
_GENERIC_PREFIXES = ("app_", "tbl_", "table_", "t_")

_TOKEN_EXPANSIONS = {
    # common ERP-ish abbreviations (generic)
    "qty": ["quantity"],
    "no": ["number"],
    "num": ["number"],
    "dt": ["date"],
    "amt": ["amount"],
    "desc": ["description"],
    "addr": ["address"],
    "uom": ["unit", "measure"],
    "ref": ["reference"],
    "po": ["purchase", "order"],
    "so": ["sales", "order"],
    "id": ["id"],
}

_COMMON_SEG_WORDS: Set[str] = {
    "purchase", "sales", "order", "invoice", "payment", "receipt", "customer", "supplier", "vendor",
    "item", "product", "material", "stock", "store", "warehouse", "batch", "lot",
    "job", "card", "jobcard", "progress", "plan", "planning", "entry", "pending",
    "register", "history", "mapping", "map", "detail", "line",
    "date", "time", "status", "type", "code", "name", "number", "no", "qty", "quantity",
    "price", "rate", "amount", "total", "balance", "tax", "gst", "vat",
    "created", "updated", "modified", "active", "enabled",
    "address", "phone", "mobile", "email", "city", "state", "country",
}


def _strip_generic_prefix(name: str) -> str:
    s = (name or "").strip()
    low = s.lower()
    for p in _GENERIC_PREFIXES:
        if low.startswith(p):
            return s[len(p):]
    return s


def _split_camel(s: str) -> str:
    # "PurchaseOrder" -> "Purchase Order"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or "")


def _segment_compound_token(token: str) -> List[str]:
    """
    Best-effort segmentation of glued identifiers into common words.
    Example: "purchaseorder" -> ["purchase","order"]
    """
    w = (token or "").lower().strip()
    if not w or len(w) < 8:
        return []

    n = len(w)
    max_word_len = min(20, n)

    dp: List[Optional[Tuple[int, int]]] = [None] * (n + 1)
    dp[0] = (0, -1)

    for i in range(1, n + 1):
        best: Optional[Tuple[int, int]] = None
        start_j = max(0, i - max_word_len)
        for j in range(start_j, i):
            if dp[j] is None:
                continue
            piece = w[j:i]
            if piece in _COMMON_SEG_WORDS:
                segs = dp[j][0] + 1
                cand = (segs, j)
                if best is None or cand[0] > best[0]:
                    best = cand
        dp[i] = best

    if dp[n] is None:
        return []

    parts: List[str] = []
    i = n
    while i > 0:
        entry = dp[i]
        if entry is None:
            return []
        _, j = entry
        if j < 0:
            break
        parts.append(w[j:i])
        i = j
    parts.reverse()

    return parts if len(parts) >= 2 else []


def _tokenize_identifier(name: str) -> List[str]:
    s = _strip_generic_prefix(name)
    s = _split_camel(s)
    s = s.replace("_", " ").replace("-", " ")
    parts = re.split(r"[^a-zA-Z0-9]+", s)

    out: List[str] = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        low = p.lower()
        out.append(low)

        seg = _segment_compound_token(low)
        if seg:
            out.extend(seg)

    return out


def _expand_tokens(tokens: List[str]) -> List[str]:
    out: List[str] = []
    for t in tokens or []:
        out.append(t)
        if t in _TOKEN_EXPANSIONS:
            out.extend(_TOKEN_EXPANSIONS[t])

    seen: Set[str] = set()
    deduped: List[str] = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(t)
    return deduped


def tokenize_free_text(text: str) -> List[str]:
    s = (text or "").strip().lower()
    if not s:
        return []
    parts = re.split(r"[^a-z0-9]+", s)
    raw = [p for p in parts if p]

    norm: List[str] = []
    for w in raw:
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            norm.append(w[:-1])
        else:
            norm.append(w)

    return _expand_tokens(norm)


def _keywords_for_table_and_columns(table: str, cols: List[str]) -> Tuple[List[str], List[str]]:
    t_tokens = _expand_tokens(_tokenize_identifier(table))
    c_tokens: List[str] = []
    for c in cols or []:
        c_tokens.extend(_expand_tokens(_tokenize_identifier(c)))

    def _dedup(xs: List[str]) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        for x in xs:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    return _dedup(t_tokens), _dedup(c_tokens)


def build_schema_lexical_index(tables: Dict[str, List[str]]) -> Dict[str, Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {}
    for table, cols in (tables or {}).items():
        t_kw, c_kw = _keywords_for_table_and_columns(table, cols)

        seen: Set[str] = set()
        all_kw: List[str] = []
        for x in (t_kw + c_kw):
            if x in seen:
                continue
            seen.add(x)
            all_kw.append(x)

        idx[table] = {
            "columns": cols,
            "table_keywords": t_kw,
            "column_keywords": c_kw,
            "all_keywords": all_kw,
        }
    return idx


# =========================
# FK edges (JSON-safe)
# =========================
def _fks_to_edges(fks: List[ForeignKey]) -> List[Dict[str, str]]:
    """
    Convert ForeignKey dataclasses into JSON-serializable edges.
    This is what you should pass to the LLM / store in review logs.
    """
    edges: List[Dict[str, str]] = []
    for fk in fks or []:
        edges.append(
            {
                "table": fk.table,
                "column": fk.column,
                "ref_table": fk.ref_table,
                "ref_column": fk.ref_column,
            }
        )
    return edges


# =========================
# Schema load
# =========================
def _load_schema_from_information_schema() -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, str]], List[ForeignKey]]:
    tables: Dict[str, List[str]] = {}
    types: Dict[str, Dict[str, str]] = {}  # NEW: table -> col -> normalized_type
    fks: List[ForeignKey] = []

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            [_DB_SCHEMA_NAME],
        )

        seen_cols: Dict[str, Set[str]] = {}
        for table_name, column_name, data_type, udt_name in cur.fetchall():
            if _is_excluded_table(table_name):
                continue
            col = (column_name or "").strip()
            if not col:
                continue

            tables.setdefault(table_name, [])
            types.setdefault(table_name, {})
            seen_cols.setdefault(table_name, set())

            if col not in seen_cols[table_name]:
                tables[table_name].append(col)
                seen_cols[table_name].add(col)

            # store normalized type
            types[table_name][col] = _normalize_db_type(data_type, udt_name)

        cur.execute(
            """
            SELECT
              tc.table_name,
              kcu.column_name,
              ccu.table_name AS foreign_table_name,
              ccu.column_name AS foreign_column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s
            ORDER BY tc.table_name, kcu.column_name
            """,
            [_DB_SCHEMA_NAME],
        )

        fk_seen: Set[Tuple[str, str, str, str]] = set()
        for table, col, ref_table, ref_col in cur.fetchall():
            if _is_excluded_table(table) or _is_excluded_table(ref_table):
                continue

            # ✅ extra safety: only keep FKs for tables we actually included in "tables"
            if str(table) not in tables or str(ref_table) not in tables:
                continue

            k = (str(table), str(col), str(ref_table), str(ref_col))
            if k in fk_seen:
                continue
            fk_seen.add(k)
            fks.append(
                ForeignKey(
                    table=str(table),
                    column=str(col),
                    ref_table=str(ref_table),
                    ref_column=str(ref_col),
                )
            )

    return tables, types, fks


def get_schema_and_fks(force_refresh: bool = False) -> Tuple[Dict[str, List[str]], List[ForeignKey]]:
    """
    Backward-compatible: still returns (tables, fks).
    Types are now cached and retrievable via get_schema_types().
    """
    now = time.time()
    if (
        not force_refresh
        and _schema_cache["tables"] is not None
        and _schema_cache["fks"] is not None
        and _schema_cache["types"] is not None
        and (now - float(_schema_cache["ts"])) < _SCHEMA_CACHE_TTL_SECONDS
    ):
        return _schema_cache["tables"], _schema_cache["fks"]  # type: ignore

    tables, types, fks = _load_schema_from_information_schema()

    _schema_cache["ts"] = now
    _schema_cache["tables"] = tables
    _schema_cache["types"] = types  # NEW
    _schema_cache["fks"] = fks
    _schema_cache["fk_edges"] = _fks_to_edges(fks)

    # keep lexical index in sync
    try:
        _schema_cache["lex"] = build_schema_lexical_index(tables)
    except Exception:
        _schema_cache["lex"] = None

    return tables, fks


def get_schema_types(force_refresh: bool = False) -> Dict[str, Dict[str, str]]:
    """
    NEW: Returns table->column->normalized_type map.
    Useful for type-aware validation and LLM prompting.
    """
    now = time.time()
    if (
        not force_refresh
        and _schema_cache.get("types") is not None
        and _schema_cache.get("tables") is not None
        and (now - float(_schema_cache["ts"])) < _SCHEMA_CACHE_TTL_SECONDS
    ):
        return _schema_cache["types"]  # type: ignore

    # force reload
    get_schema_and_fks(force_refresh=True)
    return _schema_cache["types"] or {}  # type: ignore


def get_full_schema(force_refresh: bool = False) -> Dict[str, List[str]]:
    tables, _ = get_schema_and_fks(force_refresh=force_refresh)
    return tables


def get_fk_edges(force_refresh: bool = False) -> List[Dict[str, str]]:
    """
    ✅ Main helper you need for the LLM:
    returns JSON-safe FK edges: [{table, column, ref_table, ref_column}, ...]
    """
    now = time.time()
    if (
        not force_refresh
        and _schema_cache.get("fk_edges") is not None
        and _schema_cache.get("tables") is not None
        and (now - float(_schema_cache["ts"])) < _SCHEMA_CACHE_TTL_SECONDS
    ):
        return _schema_cache["fk_edges"]  # type: ignore

    _, fks = get_schema_and_fks(force_refresh=True)
    edges = _fks_to_edges(fks)
    _schema_cache["fk_edges"] = edges
    return edges


def get_schema_lexical_index(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    now = time.time()
    if (
        not force_refresh
        and _schema_cache.get("lex") is not None
        and _schema_cache.get("tables") is not None
        and (now - float(_schema_cache["ts"])) < _SCHEMA_CACHE_TTL_SECONDS
    ):
        return _schema_cache["lex"]  # type: ignore

    tables, _ = get_schema_and_fks(force_refresh=True)
    lex = build_schema_lexical_index(tables)
    _schema_cache["lex"] = lex
    return lex


# =========================
# Chunk builder for Qdrant indexing
# =========================
def build_table_chunks(
    tables: Dict[str, List[str]],
    fks: List[ForeignKey],
    types: Optional[Dict[str, Dict[str, str]]] = None,  # NEW optional param (backward-compatible)
) -> List[dict]:
    """
    Builds schema chunks for embedding / retrieval.

    1) Stronger keyword tokens:
       - snake/camel splits + abbreviation expansions
       - generic compound segmentation (purchaseorder -> purchase + order)
    2) Relationship formatting:
       - always emits "src_table.src_col -> dst_table.dst_col"
    3) Optional type info included in text if available.
    """
    if types is None:
        # best-effort: use cached types (if schema was loaded), else empty
        types = _schema_cache.get("types") or {}

    fk_by_table: Dict[str, List[ForeignKey]] = {}
    for fk in fks:
        fk_by_table.setdefault(fk.table, []).append(fk)
        fk_by_table.setdefault(fk.ref_table, []).append(fk)

    chunks: List[dict] = []
    for table, cols in sorted((tables or {}).items()):
        if _is_excluded_table(table):
            continue

        rel_lines: List[str] = []
        for fk in fk_by_table.get(table, []):
            rel_lines.append(f"{fk.table}.{fk.column} -> {fk.ref_table}.{fk.ref_column}")

        rel = "; ".join(rel_lines) if rel_lines else "None"
        table_kw, col_kw = _keywords_for_table_and_columns(table, cols)

        # Optional: render types in a compact way
        type_map = (types or {}).get(table, {})
        typed_cols = []
        for c in cols:
            t = type_map.get(c)
            typed_cols.append(f"{c}:{t}" if t else c)

        text = (
            f"Table: {table}\n"
            f"Table keywords: {', '.join(table_kw)}\n"
            f"Columns: {', '.join(cols)}\n"
            f"Column types: {', '.join(typed_cols)}\n"
            f"Column keywords: {', '.join(col_kw)}\n"
            f"Relationships (FK): {rel}"
        )

        chunks.append(
            {
                "key": f"table::{table}",
                "table": table,
                "columns": cols,
                "table_keywords": table_kw,
                "column_keywords": col_kw,
                "text": text,
            }
        )
    return chunks
