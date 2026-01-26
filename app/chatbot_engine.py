import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .chatbot_memory import append_message, clear_state, get_history, get_state, set_state
from .chatbot_retrieval import get_schema_slice
from .chatbot_schema import get_full_schema, get_schema_types  # ✅ CHANGED
from .chatbot_sql import build_sql_from_queryspec, execute_readonly_sql
from .llm_gateway import llm_format_answer, llm_generate_queryspec

logger = logging.getLogger(__name__)

SCHEMA_RAG_MIN_SCORE = float(os.getenv("SCHEMA_RAG_MIN_SCORE", "0.55"))

# --- DB sanity / debug ---
EXPECTED_DB_NAME = os.getenv("EXPECTED_DB_NAME", "").strip()          # optional hard guard
EXPECTED_DB_SCHEMA = os.getenv("EXPECTED_DB_SCHEMA", "").strip()      # optional hard guard
CHATBOT_DB_DEBUG = os.getenv("CHATBOT_DB_DEBUG", "1").strip() in ("1", "true", "True", "yes", "YES")

# --- Print QuerySpec while running ---
DEBUG_QUERY_SPEC = os.getenv("DEBUG_QUERY_SPEC", "1").strip() in ("1", "true", "True", "yes", "YES")

# --- History gating (prevents old topic hijacking new requests) ---
HISTORY_MAX_FOR_LLM = int(os.getenv("HISTORY_MAX_FOR_LLM", "12"))
HISTORY_FORCE_NEW_TOPIC = os.getenv("HISTORY_FORCE_NEW_TOPIC", "1").strip() in ("1", "true", "True", "yes", "YES")

# ✅ Generic topic drift threshold (no domain keyword routing)
TOPIC_JACCARD_THRESHOLD = float(os.getenv("TOPIC_JACCARD_THRESHOLD", "0.18"))

# ✅ Table-clarification guard (legacy)
CLARIFY_TABLES_COVERAGE_MAX = float(os.getenv("CLARIFY_TABLES_COVERAGE_MAX", "0.60"))

# ✅ NEW: By default, do NOT ask normal users to pick a table
ENABLE_TABLE_PICKER = os.getenv("ENABLE_TABLE_PICKER", "0").strip() in ("1", "true", "True", "yes", "YES")

# ✅ NEW: Schema pack sizing for the LLM (avoid giving huge schema; still enough to decide joins)
LLM_SCHEMA_MAX_TABLES = int(os.getenv("LLM_SCHEMA_MAX_TABLES", "10"))
LLM_SCHEMA_TOP_CANDIDATES = int(os.getenv("LLM_SCHEMA_TOP_CANDIDATES", "6"))

# ✅ NEW: Retry if LLM invents columns/tables (1 retry max)
SPEC_RETRY_ON_SCHEMA_MISS = os.getenv("SPEC_RETRY_ON_SCHEMA_MISS", "1").strip() in ("1", "true", "True", "yes", "YES")
SPEC_MAX_RETRIES = int(os.getenv("SPEC_MAX_RETRIES", "1"))

# ✅ NEW: type-aware coercion/repair guard
ENABLE_TYPE_COERCION = os.getenv("ENABLE_TYPE_COERCION", "1").strip() in ("1", "true", "True", "yes", "YES")

# ✅ NEW: ID resolver (handles JC/PO/etc ambiguity without asking tables)
ENABLE_ID_RESOLVER = os.getenv("ENABLE_ID_RESOLVER", "1").strip() in ("1", "true", "True", "yes", "YES")
ID_RESOLVER_MAX_TOKENS = int(os.getenv("ID_RESOLVER_MAX_TOKENS", "3"))               # max ID-like tokens per message
ID_RESOLVER_MAX_COL_CANDIDATES = int(os.getenv("ID_RESOLVER_MAX_COL_CANDIDATES", "28"))  # max columns to probe per token
ID_RESOLVER_MAX_MATCHES = int(os.getenv("ID_RESOLVER_MAX_MATCHES", "8"))            # store only top matches
ID_RESOLVER_ALLOW_DB_PROBE = os.getenv("ID_RESOLVER_ALLOW_DB_PROBE", "1").strip() in ("1", "true", "True", "yes", "YES")


# -----------------------------
# Table label helper (TXT file)
# -----------------------------
_TABLE_DESC_CACHE: Optional[Dict[str, str]] = None


def _load_table_descriptions() -> Dict[str, str]:
    global _TABLE_DESC_CACHE
    if _TABLE_DESC_CACHE is not None:
        return _TABLE_DESC_CACHE

    out: Dict[str, str] = {}
    try:
        path = os.path.join(settings.BASE_DIR, "app", "resources", "table_descriptions.txt")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = (line or "").strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                if k and v:
                    out[k] = v
    except Exception:
        out = {}

    _TABLE_DESC_CACHE = out
    return _TABLE_DESC_CACHE


def _human_table_label(table_name: str) -> str:
    table_name = (table_name or "").strip()
    if not table_name:
        return "(unknown)"
    desc = _load_table_descriptions().get(table_name)
    return desc or table_name


def _format_table_choices(candidates: List[Dict[str, Any]], max_items: int = 3) -> str:
    lines = []
    for i, c in enumerate(candidates[:max_items], start=1):
        t = (c.get("table") or "").strip() or "(unknown)"
        lines.append(f"{i}. {_human_table_label(t)}")
    return "\n".join(lines)


def _parse_table_choice(user_text: str, candidates: List[Dict[str, Any]]) -> Optional[str]:
    text = (user_text or "").strip()
    if not text:
        return None

    m = re.match(r"^\s*(\d+)\b", text)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1].get("table") or None
        return None

    low = text.lower()
    for c in candidates:
        t = (c.get("table") or "").strip()
        if t and low == t.lower():
            return t

    for c in candidates:
        t = (c.get("table") or "").strip()
        label = _human_table_label(t).lower()
        if t and low == label:
            return t

    for c in candidates:
        t = (c.get("table") or "").strip()
        if t and low in t.lower():
            return t

    for c in candidates:
        t = (c.get("table") or "").strip()
        label = _human_table_label(t).lower()
        if t and low in label:
            return t

    return None


# -----------------------------
# ✅ Business-friendly ID disambiguation (NO tables)
# -----------------------------
def _safe_ident(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", (name or "").strip()))


_ID_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_ID_CODE_RE = re.compile(r"\b[A-Za-z]{1,8}\d{1,12}\b")
_ID_CODE_DASH_RE = re.compile(r"\b[A-Za-z]{1,8}[-_]\d{1,12}\b")
_ID_GENERIC_RE = re.compile(r"\b[A-Za-z0-9]{2,16}[-_][A-Za-z0-9]{2,16}\b")


def _extract_id_tokens(text: str) -> List[str]:
    """
    Extracts "ID-like" tokens from a message:
      - email addresses
      - codes like JC001, PO-12345, DR_1002, etc.
    Avoid pure numbers to prevent treating qty/price as IDs.
    """
    s = (text or "").strip()
    if not s:
        return []

    found: List[str] = []

    # emails
    found += _ID_EMAIL_RE.findall(s)

    # dashed codes first (PO-12345)
    found += _ID_CODE_DASH_RE.findall(s)

    # plain letter+digits codes (JC001)
    found += _ID_CODE_RE.findall(s)

    # generic mixed with separators
    found += _ID_GENERIC_RE.findall(s)

    # de-dupe preserving order
    out: List[str] = []
    seen: Set[str] = set()
    for x in found:
        x = (x or "").strip()
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= ID_RESOLVER_MAX_TOKENS:
            break
    return out


def _id_category_from_table_col(table: str, column: str) -> str:
    t = (table or "").lower()
    c = (column or "").lower()

    # order matters (specific first)
    if "purchaseorder" in t or c.startswith("po_") or c == "po_no" or "po" in c:
        return "purchase_order"
    if "jobcard" in t or "job_card" in c or c.startswith("jc") or "jobcard" in c:
        return "job_card"
    if "customer" in t or "customer" in c:
        return "customer"
    if "die" in t or "die" in c or "mould" in c or "mold" in c:
        return "die"
    if "store" in t or "inventory" in t or "item" in t or "product" in t or "item" in c or "product" in c:
        return "item"
    if "worker" in c or "employee" in c or "staff" in c or "user" in t:
        return "worker"
    return "other"


def _category_label(cat: str) -> str:
    return {
        "customer": "Customer code",
        "job_card": "Job Card number/code",
        "purchase_order": "Purchase Order number",
        "item": "Item / Product code",
        "die": "Die / Mould number",
        "worker": "Worker / Employee",
        "other": "Other ID",
    }.get(cat, "Other ID")


def _score_id_column_candidate(token: str, table: str, column: str, col_type: str) -> float:
    """
    Pure heuristic score to decide which columns to probe.
    """
    tok = (token or "")
    t = (table or "").lower()
    c = (column or "").lower()
    ty = (col_type or "").lower()

    score = 0.0

    # token hints
    prefix = ""
    m = re.match(r"^([A-Za-z]{1,8})", tok)
    if m:
        prefix = m.group(1).lower()

    # prefer text columns for alphanumeric tokens
    if ty == "text":
        score += 3.0
    elif ty == "uuid":
        score += 2.0
    elif ty == "int":
        # allow int columns only if token has digits
        if re.search(r"\d", tok):
            score += 0.8
        else:
            score -= 2.0

    # column naming signals
    for kw, w in (
        ("code", 3.0),
        ("ref", 2.7),
        ("uid", 2.4),
        ("uuid", 2.4),
        ("no", 2.2),
        ("number", 2.2),
        ("id", 1.5),
        ("name", 0.6),
    ):
        if kw in c:
            score += w

    # table/column category signals
    cat = _id_category_from_table_col(table, column)
    if cat in ("customer", "job_card", "purchase_order", "item", "die"):
        score += 0.6

    # prefix match boosts
    if prefix:
        if prefix in c:
            score += 1.6
        if prefix in t:
            score += 1.0

    # exact-ish column patterns
    if tok.upper().startswith("PO") and ("po" in c or "purchase" in t):
        score += 1.2
    if tok.upper().startswith("JC") and ("job" in t or "job" in c or "jc" in c):
        score += 1.2
    if tok.upper().startswith("DR") and ("die" in t or "die" in c):
        score += 1.0

    return score


def _candidate_id_columns_for_token(
    token: str,
    full_schema: Dict[str, List[str]],
    schema_types: Dict[str, Dict[str, str]],
) -> List[Tuple[str, str, str, float]]:
    """
    Returns list of (table, column, type, score), sorted by score desc.
    Only includes columns that *look* like identifiers.
    """
    token = (token or "").strip()
    if not token:
        return []

    out: List[Tuple[str, str, str, float]] = []

    for t, cols in (full_schema or {}).items():
        if not _safe_ident(t):
            continue
        tmap = (schema_types.get(t) or {})
        for c in (cols or []):
            if not _safe_ident(c):
                continue
            low = c.lower()

            # only consider columns that are "identifier-ish"
            if not (
                low.endswith("_code")
                or low.endswith("_ref")
                or low.endswith("_no")
                or low.endswith("_id")
                or "code" in low
                or "ref" in low
                or (low == "id")
                or "uid" in low
                or "uuid" in low
                or "number" in low
            ):
                continue

            col_type = (tmap.get(c) or "").strip().lower()
            if col_type not in ("text", "uuid", "int", "numeric"):
                # still allow if unknown type (some schemas don’t have types)
                if col_type:
                    continue
                col_type = ""

            score = _score_id_column_candidate(token, t, c, col_type)
            if score <= 0.5:
                continue

            out.append((t, c, col_type, score))

    out.sort(key=lambda x: x[3], reverse=True)
    return out[:ID_RESOLVER_MAX_COL_CANDIDATES]


def _db_value_exists(table: str, column: str, value: Any, col_type: str) -> bool:
    """
    Safe existence probe.
    Only runs if table/column are safe identifiers and come from schema lists.
    """
    if not ID_RESOLVER_ALLOW_DB_PROBE:
        return False
    if not _safe_ident(table) or not _safe_ident(column):
        return False

    # Coerce for int columns
    v = value
    try:
        if (col_type or "").lower() == "int" and isinstance(value, str):
            dig = "".join(re.findall(r"\d+", value))
            if dig:
                v = int(dig)
            else:
                return False
    except Exception:
        return False

    sql = f'SELECT 1 FROM "{table}" WHERE "{column}" = %s LIMIT 1;'
    try:
        with connection.cursor() as cur:
            cur.execute(sql, [v])
            row = cur.fetchone()
        return bool(row)
    except Exception:
        return False


def _resolve_ids_from_message(
    question: str,
    full_schema: Dict[str, List[str]],
    schema_types: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """
    Returns:
      {
        "tokens": [...],
        "matches": { token: [ {table,column,type,score,category}, ... ] },
        "best":   { token: {table,column,...} or None },
        "ambiguous": [token,...],
        "hint_text": "ID_HINTS: ...",
        "options": { token: [ {label, table, column, value, category}, ... ] }
      }
    """
    tokens = _extract_id_tokens(question)
    matches_by_token: Dict[str, List[Dict[str, Any]]] = {}
    best_by_token: Dict[str, Optional[Dict[str, Any]]] = {}
    ambiguous: List[str] = []
    options_by_token: Dict[str, List[Dict[str, Any]]] = {}

    for tok in tokens:
        candidates = _candidate_id_columns_for_token(tok, full_schema, schema_types)
        found: List[Dict[str, Any]] = []

        for (t, c, ty, score) in candidates:
            if _db_value_exists(t, c, tok, ty):
                cat = _id_category_from_table_col(t, c)
                found.append(
                    {
                        "table": t,
                        "column": c,
                        "type": ty,
                        "score": score,
                        "category": cat,
                        "value": tok,
                    }
                )
                if len(found) >= ID_RESOLVER_MAX_MATCHES:
                    break

        # sort found by score
        found.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        matches_by_token[tok] = found

        if not found:
            best_by_token[tok] = None
            continue

        # determine ambiguity: if token exists in multiple categories
        cats = []
        seen_cat = set()
        for f in found:
            cc = f.get("category") or "other"
            if cc not in seen_cat:
                seen_cat.add(cc)
                cats.append(cc)

        if len(cats) >= 2:
            ambiguous.append(tok)

            # Build business-friendly options: best match per category
            per_cat: Dict[str, Dict[str, Any]] = {}
            for f in found:
                cc = f.get("category") or "other"
                if cc not in per_cat:
                    per_cat[cc] = f

            opts: List[Dict[str, Any]] = []
            for cc, f in per_cat.items():
                opts.append(
                    {
                        "label": _category_label(cc),
                        "category": cc,
                        "table": f.get("table"),
                        "column": f.get("column"),
                        "value": tok,
                        "score": f.get("score"),
                    }
                )
            # stable sort for display
            opts.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            options_by_token[tok] = opts[:5]
            best_by_token[tok] = None
        else:
            best_by_token[tok] = found[0]

    # create compact hint text for LLM (keep small)
    hint_parts: List[str] = []
    for tok, best in best_by_token.items():
        if best and best.get("table") and best.get("column"):
            # DO NOT leak table names to user; this is only internal hint to LLM
            hint_parts.append(f"{tok} => use {best['table']}.{best['column']}")

    hint_text = ""
    if hint_parts:
        hint_text = "ID_HINTS: " + " | ".join(hint_parts[:3])

    return {
        "tokens": tokens,
        "matches": matches_by_token,
        "best": best_by_token,
        "ambiguous": ambiguous,
        "options": options_by_token,
        "hint_text": hint_text,
    }


def _format_id_disambiguation_prompt(token: str, options: List[Dict[str, Any]]) -> str:
    lines = []
    for i, opt in enumerate(options or [], start=1):
        label = (opt.get("label") or "").strip() or "Other"
        lines.append(f"{i}. {label}")
    choices = "\n".join(lines) if lines else "1. Customer code\n2. Job Card number/code\n3. Purchase Order number\n4. Item / Product code\n5. Die / Mould number"

    return (
        f"I found the ID **{token}** in your message, but it could refer to multiple things.\n\n"
        f"Which one is it?\n{choices}\n\n"
        "Reply with the number (1/2/3...) or the word (customer / job card / PO / item / die)."
    )


def _parse_id_disambiguation_choice(user_text: str, options: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Returns the chosen option dict from options list.
    """
    text = (user_text or "").strip().lower()
    if not text or not options:
        return None

    m = re.match(r"^\s*(\d+)\b", text)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(options):
            return options[idx - 1]
        return None

    # keyword matching
    for opt in options:
        label = (opt.get("label") or "").strip().lower()
        cat = (opt.get("category") or "").strip().lower()
        if text == label or text == cat:
            return opt

    # partial matching (e.g., "job", "customer")
    for opt in options:
        label = (opt.get("label") or "").strip().lower()
        cat = (opt.get("category") or "").strip().lower()
        if text and (text in label or text in cat):
            return opt

    return None


def _apply_id_hint_patch(spec: Any, chosen: Dict[str, Any]) -> Any:
    """
    Post-LLM gentle patch:
    If spec already uses the same ID value in WHERE but on wrong column,
    and the intended table is already in FROM/JOIN set, rewrite that WHERE to the intended column.
    We do NOT add new tables/joins here (avoid breaking SQL).
    """
    if not isinstance(spec, dict) or not chosen:
        return spec

    tok = chosen.get("value")
    ht = (chosen.get("table") or "").strip()
    hc = (chosen.get("column") or "").strip()
    if not tok or not ht or not hc:
        return spec

    base = (spec.get("from") or "").strip()
    join_tables = set()
    for j in (spec.get("joins") or []):
        if isinstance(j, dict) and j.get("table"):
            join_tables.add((j.get("table") or "").strip())

    allowed_tables = {base} | join_tables
    if ht not in allowed_tables:
        return spec

    where = spec.get("where") or []
    if not isinstance(where, list):
        return spec

    for w in where:
        if not isinstance(w, dict):
            continue
        v = w.get("value")
        if isinstance(v, str) and v.strip() == str(tok).strip():
            # rewrite to intended table/col
            w["table"] = ht
            w["column"] = hc
            w["op"] = w.get("op") or "="
            return spec

    return spec


def _allowed_review_fields() -> Set[str]:
    from .models import ChatbotReviewItem
    return {f.name for f in ChatbotReviewItem._meta.fields}


def _safe_create_review_item(fields: Dict[str, Any]) -> Optional[int]:
    try:
        from .models import ChatbotReviewItem
        allowed = _allowed_review_fields()
        clean = {k: v for k, v in (fields or {}).items() if k in allowed}
        obj = ChatbotReviewItem.objects.create(**clean)
        return obj.id
    except Exception:
        return None


def _safe_update_review_item(review_id: Optional[int], fields: Dict[str, Any]) -> None:
    """
    Only update fields that actually exist in ChatbotReviewItem.
    Prevents FieldError from killing the update silently.
    """
    if not review_id or not fields:
        return
    try:
        from .models import ChatbotReviewItem
        allowed = _allowed_review_fields()
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        ChatbotReviewItem.objects.filter(id=review_id).update(**clean)
    except Exception:
        pass


def _intent_payload_from_queryspec(question: str, chosen_table: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    select_cols = []
    for s in (spec.get("select") or []):
        if isinstance(s, dict):
            if s.get("column"):
                select_cols.append({"table": s.get("table"), "column": s.get("column"), "agg": s.get("agg")})
            elif s.get("expr"):
                select_cols.append({"expr": s.get("expr"), "agg": s.get("agg"), "alias": s.get("alias")})

    filter_cols = []
    for w in (spec.get("where") or []):
        if isinstance(w, dict) and w.get("column"):
            filter_cols.append({"table": w.get("table"), "column": w.get("column"), "op": w.get("op")})

    join_hints = []
    for j in (spec.get("joins") or []):
        if isinstance(j, dict) and j.get("table") and j.get("on"):
            join_hints.append({"type": j.get("type"), "table": j.get("table"), "on": j.get("on")})

    tables = []
    if spec.get("from"):
        tables.append(spec.get("from"))
    for j in (spec.get("joins") or []):
        if isinstance(j, dict) and j.get("table"):
            tables.append(j.get("table"))

    seen = set()
    tables = [t for t in tables if t and not (t in seen or seen.add(t))]

    return {
        "kind": "intent_mapping",
        "example_question": question,
        "tables": tables or [chosen_table],
        "preferred_select": select_cols,
        "preferred_filters": filter_cols,
        "join_hints": join_hints,
    }


# -----------------------------
# DB sanity helpers
# -----------------------------
def _db_context() -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    try:
        sd = getattr(connection, "settings_dict", {}) or {}
        ctx.update(
            {
                "ENGINE": sd.get("ENGINE"),
                "NAME": sd.get("NAME"),
                "HOST": sd.get("HOST"),
                "PORT": sd.get("PORT"),
                "USER": sd.get("USER"),
            }
        )
        with connection.cursor() as cur:
            cur.execute("select current_database(), current_schema();")
            row = cur.fetchone() or (None, None)
        ctx["current_database"] = row[0]
        ctx["current_schema"] = row[1]
    except Exception as e:
        ctx["error"] = str(e)
    return ctx


def _db_guard_or_message(user_id: int, session_key: str, dbctx: Dict[str, Any]) -> Optional[str]:
    cur_db = (dbctx.get("current_database") or "").strip()
    cur_schema = (dbctx.get("current_schema") or "").strip()

    if EXPECTED_DB_NAME and cur_db and cur_db != EXPECTED_DB_NAME:
        return (
            f"⚠️ Server is connected to database '{cur_db}' (schema '{cur_schema}'), "
            f"but EXPECTED_DB_NAME is '{EXPECTED_DB_NAME}'.\n"
            f"Fix your Django DATABASES / .env so they point to the same DB you checked in pgAdmin."
        )

    if EXPECTED_DB_SCHEMA and cur_schema and cur_schema != EXPECTED_DB_SCHEMA:
        return (
            f"⚠️ Server is connected to schema '{cur_schema}' in database '{cur_db}', "
            f"but EXPECTED_DB_SCHEMA is '{EXPECTED_DB_SCHEMA}'.\n"
            f"Fix your connection/search_path."
        )

    return None


# -----------------------------
# Generic topic gating
# -----------------------------
_STOPWORDS: Set[str] = {
    "the", "a", "an", "of", "to", "for", "and", "or", "with", "show", "me", "give", "get",
    "latest", "last", "top", "recent", "all", "list", "please", "in", "on", "at", "by", "from",
    "that", "this", "those", "these", "it", "its", "their", "my", "your", "our", "as"
}


def _tokens_for_topic(text: str) -> Set[str]:
    s = (text or "").lower()
    s = s.replace("_", " ").replace("-", " ")
    parts = re.split(r"[^a-z0-9]+", s)
    return {p for p in parts if p and p not in _STOPWORDS}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return float(inter) / float(union) if union else 0.0


def _last_user_message(history: List[Dict[str, Any]]) -> str:
    for m in reversed(history or []):
        if (m.get("role") == "user") and m.get("content"):
            return str(m["content"])
    return ""


def _history_for_llm(history: List[Dict[str, Any]], current_message: str) -> List[Dict[str, Any]]:
    history = (history or [])[-HISTORY_MAX_FOR_LLM:]

    if not HISTORY_FORCE_NEW_TOPIC:
        return history

    prev = _last_user_message(history)
    if not prev:
        return history

    prev_t = _tokens_for_topic(prev)
    cur_t = _tokens_for_topic(current_message)
    sim = _jaccard(prev_t, cur_t)

    if sim < TOPIC_JACCARD_THRESHOLD:
        return []

    return history


# -----------------------------
# Clarification candidate builder (legacy)
# -----------------------------
def _build_clarification_candidates(retrieval: Dict[str, Any], max_items: int = 5) -> List[Dict[str, Any]]:
    cov = retrieval.get("coverage_debug") if isinstance(retrieval.get("coverage_debug"), dict) else {}
    table_scores = cov.get("table_scores") if isinstance(cov.get("table_scores"), dict) else None
    if table_scores:
        ranked: List[Tuple[str, int, float]] = []
        for t, s in table_scores.items():
            if not isinstance(s, dict):
                continue
            coverage = int(s.get("coverage") or 0)
            vec = float(s.get("vector") or s.get("hybrid") or 0.0)
            if coverage <= 0 and vec <= 0.0:
                continue
            ranked.append((t, coverage, vec))

        ranked.sort(key=lambda x: (x[1], x[2]), reverse=True)
        out = [{"table": t, "score": vec, "coverage": covg} for (t, covg, vec) in ranked[:max_items]]
        if out:
            return out

    candidates = retrieval.get("schema_candidates") or []
    out2: List[Dict[str, Any]] = []
    for c in candidates:
        if isinstance(c, dict) and c.get("table"):
            out2.append({"table": c.get("table"), "score": c.get("score")})
        if len(out2) >= max_items:
            break
    return out2


# -----------------------------
# ✅ Expr-aware helpers (IMPORTANT)
# -----------------------------
def _tables_in_expr(expr: Any) -> Set[str]:
    out: Set[str] = set()
    if isinstance(expr, dict):
        if "table" in expr and "column" in expr:
            t = (expr.get("table") or "").strip()
            if t:
                out.add(t)
        if "left" in expr:
            out |= _tables_in_expr(expr.get("left"))
        if "right" in expr:
            out |= _tables_in_expr(expr.get("right"))
    return out


# -----------------------------
# ✅ Join minimizer (drop unnecessary joins) - expr-aware
# -----------------------------
def _collect_tables_from_items(items: Any) -> Set[str]:
    out: Set[str] = set()
    if not items or not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, dict):
            t = (it.get("table") or "").strip()
            if t:
                out.add(t)
            if it.get("expr") is not None:
                out |= _tables_in_expr(it.get("expr"))
    return out


def _tables_referenced_in_spec(spec: Dict[str, Any]) -> Set[str]:
    used: Set[str] = set()

    base = (spec.get("from") or "").strip()
    if base:
        used.add(base)

    used |= _collect_tables_from_items(spec.get("select") or [])
    used |= _collect_tables_from_items(spec.get("where") or [])
    used |= _collect_tables_from_items(spec.get("order_by") or [])
    used |= _collect_tables_from_items(spec.get("group_by") or [])
    used |= _collect_tables_from_items(spec.get("having") or [])

    return {t for t in used if t}


def _extract_tables_from_on(on: Any) -> Set[str]:
    """
    Supports:
      - list-of-dicts join.on format with left_table/right_table
      - dict format {left: "t.c", right:"u.d"}
    """
    out: Set[str] = set()
    if not on:
        return out

    if isinstance(on, list):
        for cond in on:
            if not isinstance(cond, dict):
                continue
            lt = (cond.get("left_table") or "").strip()
            rt = (cond.get("right_table") or "").strip()
            if lt:
                out.add(lt)
            if rt:
                out.add(rt)
        return out

    if isinstance(on, dict):
        left = str(on.get("left") or "")
        right = str(on.get("right") or "")
        for s in (left, right):
            if "." in s:
                t = s.split(".", 1)[0].strip()
                if t:
                    out.add(t)
        return out

    # fallback string scan
    if isinstance(on, str):
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.", on):
            out.add(m.group(1))
    return out


def minimize_queryspec(spec: Any) -> Any:
    if not isinstance(spec, dict):
        return spec

    base = (spec.get("from") or "").strip()
    joins = spec.get("joins") or []
    if not base or not isinstance(joins, list) or not joins:
        return spec

    required = _tables_referenced_in_spec(spec)

    kept_rev: List[Dict[str, Any]] = []
    for j in reversed(joins):
        if not isinstance(j, dict):
            continue
        jt = (j.get("table") or "").strip()
        if not jt:
            continue

        if jt in required:
            kept_rev.append(j)
            required.add(jt)
            required |= _extract_tables_from_on(j.get("on"))

    out = dict(spec)
    out["joins"] = list(reversed(kept_rev))
    return out


# -----------------------------
# ✅ Type-aware patching (prevents "invalid input syntax for integer")
# -----------------------------
_RELATIVE_PREFIXES = ("RELATIVE_",)


def _is_relative_value(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip().upper()
    return any(s.startswith(p) for p in _RELATIVE_PREFIXES)


def _looks_like_int(s: str) -> bool:
    return bool(re.match(r"^\s*\d+\s*$", s or ""))


def _extract_digits(s: str) -> str:
    return "".join(re.findall(r"\d+", s or ""))


def _has_alpha(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s or ""))


def _find_best_text_alt_column(
    table: str,
    int_column: str,
    full_schema: Dict[str, List[str]],
    schema_types: Dict[str, Dict[str, str]],
) -> Optional[str]:
    cols = full_schema.get(table) or []
    tmap = schema_types.get(table) or {}

    if not cols:
        return None

    text_cols = [c for c in cols if (tmap.get(c) == "text")]
    if not text_cols:
        return None

    col = (int_column or "").strip()
    base = col
    if col.endswith("_no"):
        base = col[:-3]

    preferred = [f"{base}_ref", f"{base}_code", f"{base}_name", f"{base}_number"]
    for p in preferred:
        if p in text_cols:
            return p

    for c in text_cols:
        low = c.lower()
        if base and base.lower() in low and ("ref" in low or "code" in low):
            return c

    for c in text_cols:
        low = c.lower()
        if low.endswith("_ref") or low.endswith("_code") or low == "ref" or low == "code":
            return c

    return None


def _coerce_filter_value(col_type: str, value: Any) -> Tuple[Any, Optional[str]]:
    t = (col_type or "").strip().lower()
    if value is None:
        return value, None
    if _is_relative_value(value):
        return value, None

    if t == "int":
        if isinstance(value, int):
            return value, None
        if isinstance(value, float) and value.is_integer():
            return int(value), "coerced float->int"
        if isinstance(value, str):
            s = value.strip()
            if _looks_like_int(s):
                return int(s), "coerced str->int"
            dig = _extract_digits(s)
            if dig and _has_alpha(s):
                try:
                    return int(dig), "extracted digits->int"
                except Exception:
                    return value, None
        return value, None

    if t == "numeric":
        if isinstance(value, (int, float)):
            return value, None
        if isinstance(value, str):
            s = value.strip()
            if re.match(r"^\s*-?\d+(\.\d+)?\s*$", s):
                try:
                    return float(s), "coerced str->float"
                except Exception:
                    return value, None
        return value, None

    if t == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "yes", "y", "1"):
                return True, "coerced str->bool"
            if s in ("false", "no", "n", "0"):
                return False, "coerced str->bool"
        return value, None

    return value, None


def _apply_type_coercions_and_repairs(
    spec: Any,
    full_schema: Dict[str, List[str]],
    schema_types: Dict[str, Dict[str, str]],
) -> Any:
    if not ENABLE_TYPE_COERCION or not isinstance(spec, dict):
        return spec

    base_table = (spec.get("from") or "").strip()

    for key in ("where", "having"):
        items = spec.get(key) or []
        if not isinstance(items, list):
            continue

        for it in items:
            if not isinstance(it, dict):
                continue

            t = (it.get("table") or base_table or "").strip()
            c = (it.get("column") or "").strip()

            if not t or not c or "value" not in it:
                continue

            v = it.get("value")
            col_type = (schema_types.get(t) or {}).get(c)
            if not col_type:
                continue

            if col_type == "int" and isinstance(v, str) and _has_alpha(v) and not _is_relative_value(v):
                alt = _find_best_text_alt_column(t, c, full_schema, schema_types)
                if alt:
                    it["table"] = t
                    it["column"] = alt
                    continue

            new_v, _note = _coerce_filter_value(col_type, v)
            it["value"] = new_v

    return spec


# -----------------------------
# ✅ QuerySpec validator (schema-grounded, expr-aware)
# -----------------------------
def _validate_queryspec_against_schema(spec: Any, full_schema: Dict[str, List[str]]) -> Tuple[bool, List[str], Set[str]]:
    if not isinstance(spec, dict):
        return False, ["Query spec is not a dict."], set()

    errors: List[str] = []
    used_tables = _tables_referenced_in_spec(spec)

    base = (spec.get("from") or "").strip()
    if not base:
        errors.append("Missing FROM table.")
        return False, errors, used_tables

    if base not in full_schema:
        errors.append(f"Unknown FROM table: {base}")

    join_tables: Set[str] = set()
    joins = spec.get("joins") or []
    if isinstance(joins, list):
        for j in joins:
            if not isinstance(j, dict):
                continue
            jt = (j.get("table") or "").strip()
            if jt:
                join_tables.add(jt)
                if jt not in full_schema:
                    errors.append(f"Unknown JOIN table: {jt}")

            on = j.get("on")
            if on and isinstance(on, list):
                for cond in on:
                    if not isinstance(cond, dict):
                        continue
                    lt = (cond.get("left_table") or base).strip()
                    lc = (cond.get("left_column") or "").strip()
                    rt = (cond.get("right_table") or jt).strip()
                    rc = (cond.get("right_column") or "").strip()
                    op = (cond.get("op") or "=").strip()

                    if op != "=":
                        errors.append(f"JOIN {jt}: only '=' allowed in join conditions")
                    if lt and lt not in full_schema:
                        errors.append(f"JOIN {jt}: unknown table '{lt}'")
                    if rt and rt not in full_schema:
                        errors.append(f"JOIN {jt}: unknown table '{rt}'")
                    if lt in full_schema and lc and lc not in (full_schema.get(lt) or []):
                        errors.append(f"JOIN {jt}: unknown column '{lt}.{lc}'")
                    if rt in full_schema and rc and rc not in (full_schema.get(rt) or []):
                        errors.append(f"JOIN {jt}: unknown column '{rt}.{rc}'")

    referenced = _tables_referenced_in_spec(spec)
    allowed = {base} | join_tables
    for t in referenced:
        if t not in full_schema:
            errors.append(f"Unknown referenced table: {t}")
        elif t not in allowed:
            errors.append(f"Referenced table '{t}' is not in FROM/JOIN. FROM='{base}', JOINs={sorted(join_tables)}")

    def check_items(items: Any, label: str) -> None:
        if not items or not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue

            t = (it.get("table") or "").strip()
            c = (it.get("column") or "").strip()
            if t and c:
                if t not in full_schema:
                    errors.append(f"{label}: unknown table '{t}'")
                else:
                    cols = full_schema.get(t) or []
                    if c not in cols:
                        errors.append(f"{label}: unknown column '{t}.{c}'")

            expr = it.get("expr")
            if expr is not None:
                expr_tables = _tables_in_expr(expr)
                for et in expr_tables:
                    if et not in full_schema:
                        errors.append(f"{label}: expr references unknown table '{et}'")

    check_items(spec.get("select"), "SELECT")
    check_items(spec.get("where"), "WHERE")
    check_items(spec.get("order_by"), "ORDER_BY")
    check_items(spec.get("group_by"), "GROUP_BY")
    check_items(spec.get("having"), "HAVING")

    ok = len(errors) == 0
    return ok, errors, used_tables


def _build_llm_schema_payload(
    schema_slice: Dict[str, List[str]],
    retrieval: Dict[str, Any],
    schema_types: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Any]:
    schema = schema_slice or {}
    if not schema:
        return {"schema": {}, "types": {}, "fk_edges": []}

    if schema_types is None:
        try:
            schema_types = get_schema_types()
        except Exception:
            schema_types = {}

    candidates = retrieval.get("schema_candidates") or []
    cand_tables: List[str] = []
    for c in candidates:
        if isinstance(c, dict) and c.get("table"):
            cand_tables.append(str(c.get("table")).strip())
        if len(cand_tables) >= LLM_SCHEMA_TOP_CANDIDATES:
            break

    cand_tables = [t for t in cand_tables if t in schema]

    if cand_tables:
        ordered: List[str] = []
        seen: Set[str] = set()
        for t in cand_tables:
            if t not in seen:
                seen.add(t)
                ordered.append(t)

        for t in schema.keys():
            if t not in seen:
                seen.add(t)
                ordered.append(t)
            if len(ordered) >= LLM_SCHEMA_MAX_TABLES:
                break

        schema_small = {t: schema[t] for t in ordered if t in schema}
    else:
        schema_small = {}
        for t in list(schema.keys())[:LLM_SCHEMA_MAX_TABLES]:
            schema_small[t] = schema[t]

    fk_edges = retrieval.get("fk_edges", []) or []
    included = set(schema_small.keys())
    fk_small: List[Dict[str, Any]] = []
    for e in fk_edges:
        if not isinstance(e, dict):
            continue
        a = (e.get("table") or "").strip()
        b = (e.get("ref_table") or "").strip()
        if a in included and b in included:
            fk_small.append(e)

    types_small: Dict[str, Dict[str, str]] = {}
    for t in included:
        if t in (schema_types or {}):
            types_small[t] = dict((schema_types or {}).get(t) or {})

    return {"schema": schema_small, "types": types_small, "fk_edges": fk_small}


def _augment_question_with_schema_errors(q: str, errors: List[str]) -> str:
    errs = [e for e in (errors or []) if e]
    if not errs:
        return q
    tail = "; ".join(errs[:6])
    return f"{q}\n\n(Important: do not invent tables/columns. Fix these issues: {tail})"


def _log_queryspec(tag: str, attempt: int, spec: Any) -> None:
    if not DEBUG_QUERY_SPEC:
        return
    try:
        pretty = json.dumps(spec, indent=2, default=str)
    except Exception:
        pretty = str(spec)
    msg = f"[QuerySpec:{tag}][attempt={attempt}]\n{pretty}"
    print(msg)
    logger.info(msg)


# -----------------------------
# ✅ Small deterministic patching for your 2 most-tested PO cases
# -----------------------------
def _ensure_join(spec: Dict[str, Any], join_table: str, on: List[Dict[str, Any]], join_type: str = "left") -> None:
    joins = spec.setdefault("joins", [])
    for j in joins:
        if isinstance(j, dict) and (j.get("table") or "").strip() == join_table:
            return
    joins.append({"type": join_type, "table": join_table, "on": on})


def _ensure_select(spec: Dict[str, Any], item: Dict[str, Any]) -> None:
    sel = spec.setdefault("select", [])
    for s in sel:
        if not isinstance(s, dict):
            continue
        if item.get("expr") is not None:
            if s.get("expr") == item.get("expr") and (s.get("agg") or None) == (item.get("agg") or None) and (s.get("alias") or None) == (item.get("alias") or None):
                return
        else:
            if (s.get("table") == item.get("table") and s.get("column") == item.get("column")
                    and (s.get("agg") or None) == (item.get("agg") or None)
                    and (s.get("alias") or None) == (item.get("alias") or None)):
                return
    sel.append(item)


def _patch_spec_for_common_po_patterns(question: str, spec: Any, full_schema: Dict[str, List[str]]) -> Any:
    if not isinstance(spec, dict):
        return spec

    q = (question or "").lower()
    base = (spec.get("from") or "").strip()

    if base != "app_purchaseorder":
        return spec

    po_cols = set(full_schema.get("app_purchaseorder") or [])
    cust_cols = set(full_schema.get("app_customer") or [])

    can_join_customer = (
        "customer_id" in po_cols and
        "customer_code" in cust_cols and
        "customer_name" in cust_cols
    )

    if "purchase order" in q and "customer" in q and ("latest" in q or "last" in q) and ("customer name" in q or "with customer" in q):
        if can_join_customer:
            _ensure_join(
                spec,
                "app_customer",
                on=[{
                    "left_table": "app_purchaseorder",
                    "left_column": "customer_id",
                    "op": "=",
                    "right_table": "app_customer",
                    "right_column": "customer_code",
                }],
                join_type="left",
            )
            _ensure_select(spec, {"table": "app_customer", "column": "customer_name", "agg": None, "alias": "customer_name"})

            sel = spec.get("select") or []
            spec["select"] = [s for s in sel if not (isinstance(s, dict) and s.get("table") == "app_purchaseorder" and s.get("column") == "customer_id")]

        return spec

    if ("grouped by customer" in q or "per customer" in q) and ("total" in q and ("order value" in q or "total value" in q)):
        if can_join_customer:
            _ensure_join(
                spec,
                "app_customer",
                on=[{
                    "left_table": "app_purchaseorder",
                    "left_column": "customer_id",
                    "op": "=",
                    "right_table": "app_customer",
                    "right_column": "customer_code",
                }],
                join_type="left",
            )

            spec["select"] = [
                {"table": "app_customer", "column": "customer_name", "agg": None, "alias": "customer_name"},
                {"table": "app_purchaseorder", "column": "po_no", "agg": "count", "alias": "po_count"},
                {
                    "expr": {
                        "op": "*",
                        "left": {"table": "app_purchaseorder", "column": "quantity"},
                        "right": {"table": "app_purchaseorder", "column": "price"},
                    },
                    "agg": "sum",
                    "alias": "total_order_value",
                },
            ]

            spec["group_by"] = [
                {"table": "app_customer", "column": "customer_name"},
                {"table": "app_customer", "column": "customer_code"},
            ]

            where = spec.get("where") or []
            has_created_filter = any(
                isinstance(w, dict)
                and (w.get("table") or "").strip() == "app_purchaseorder"
                and (w.get("column") or "").strip() == "created_at"
                for w in where
            )
            if not has_created_filter and "created_at" in po_cols:
                where.append({"table": "app_purchaseorder", "column": "created_at", "op": ">=", "value": "RELATIVE_DAYS:30"})
                spec["where"] = where

            spec["order_by"] = [{"table": "app_purchaseorder", "column": "created_at", "dir": "asc"}]
            spec["limit"] = int(spec.get("limit") or 50)

        return spec

    return spec


def answer_user_question(user, message: str, session_key: str) -> str:
    user_id = int(getattr(user, "id", 0) or 0)
    if not user_id:
        raise ValueError("User must be authenticated.")

    message = (message or "").strip()
    if not message:
        return "Please type a message."

    dbctx = _db_context() if CHATBOT_DB_DEBUG or EXPECTED_DB_NAME or EXPECTED_DB_SCHEMA else {}
    db_guard_msg = _db_guard_or_message(user_id, session_key, dbctx) if dbctx else None
    if db_guard_msg:
        append_message(user_id, session_key, "user", message)
        append_message(user_id, session_key, "assistant", db_guard_msg)
        return db_guard_msg

    raw_history = get_history(user_id, session_key)
    state = get_state(user_id, session_key) or {}

    forced_tables: List[str] = []
    clarification_used = False
    effective_question = message
    chosen_table: Optional[str] = None

    # ✅ NEW: handle ID disambiguation state (business-friendly)
    if state.get("mode") == "choose_id_type" and state.get("original_question") and state.get("token") and state.get("options"):
        token = str(state.get("token"))
        options = state.get("options") or []
        chosen = _parse_id_disambiguation_choice(message, options)
        if not chosen:
            reply = _format_id_disambiguation_prompt(token, options)
            append_message(user_id, session_key, "user", message)
            append_message(user_id, session_key, "assistant", reply)
            return reply

        # internal hint to guide LLM
        effective_question = str(state.get("original_question"))
        effective_question = f"{effective_question}\n\n(User clarified: {token} refers to {_category_label(chosen.get('category'))}.)\nID_HINTS: {token} => use {chosen.get('table')}.{chosen.get('column')}"
        forced_tables = [str(chosen.get("table") or "").strip()] if chosen.get("table") else []
        clarification_used = True
        clear_state(user_id, session_key)
        # stash chosen in state-local var for patching after LLM
        chosen_id_option = chosen
    else:
        chosen_id_option = None

    # Existing table picker state
    if state.get("mode") == "choose_table" and state.get("candidates") and state.get("original_question"):
        candidates = state.get("candidates") or []
        chosen_table = _parse_table_choice(message, candidates)
        if not chosen_table:
            reply = (
                "Please reply with the number (1/2/3) or a matching label.\n\n"
                "Which one did you mean?\n"
                f"{_format_table_choices(candidates)}"
            )
            append_message(user_id, session_key, "user", message)
            append_message(user_id, session_key, "assistant", reply)
            return reply

        forced_tables = [chosen_table]
        effective_question = str(state.get("original_question"))
        clarification_used = True
        clear_state(user_id, session_key)

    elif state.get("mode") == "llm_clarify" and state.get("original_question"):
        original_q = str(state.get("original_question"))
        effective_question = f"{original_q}\nUser clarification: {message}"
        clarification_used = True
        clear_state(user_id, session_key)

    history_for_llm = _history_for_llm(raw_history, effective_question)

    # ✅ Load full schema + types once (also needed for ID resolver)
    full_schema = get_full_schema()
    schema_types = get_schema_types()

    # ✅ NEW: Try resolving ID ambiguity BEFORE schema retrieval/LLM
    if ENABLE_ID_RESOLVER and not forced_tables and not clarification_used:
        id_res = _resolve_ids_from_message(effective_question, full_schema, schema_types)

        # If ambiguous and we have options, ask business-friendly clarification
        amb = id_res.get("ambiguous") or []
        options_by_token = id_res.get("options") or {}

        if amb:
            tok = amb[0]
            opts = options_by_token.get(tok) or []
            if opts:
                reply = _format_id_disambiguation_prompt(tok, opts)
                set_state(
                    user_id,
                    session_key,
                    {
                        "mode": "choose_id_type",
                        "original_question": effective_question,
                        "token": tok,
                        "options": opts,
                        "created_at": timezone.now().isoformat(),
                    },
                )
                append_message(user_id, session_key, "user", message)
                append_message(user_id, session_key, "assistant", reply)
                return reply

        # If we found confident “best” mappings, add tiny hint to LLM question
        hint_text = (id_res.get("hint_text") or "").strip()
        if hint_text:
            effective_question = f"{effective_question}\n\n{hint_text}"

    pack = get_schema_slice(effective_question, forced_tables=forced_tables)
    schema_slice = pack.get("schema") or {}
    retrieval = pack.get("retrieval") or {}

    intent_hint = retrieval.get("intent_hint")
    field_extraction = retrieval.get("field_extraction") if isinstance(retrieval.get("field_extraction"), dict) else {}
    needs_clarification = field_extraction.get("needs_clarification") is True
    clarify_q = (field_extraction.get("clarify") or "").strip()

    if (not forced_tables) and (not intent_hint) and needs_clarification:
        reply = clarify_q or "Can you clarify what exactly you want to see?"
        set_state(
            user_id,
            session_key,
            {
                "mode": "llm_clarify",
                "original_question": effective_question,
                "clarify": reply,
                "created_at": timezone.now().isoformat(),
            },
        )
        append_message(user_id, session_key, "user", message)
        append_message(user_id, session_key, "assistant", reply)
        return reply

    if ENABLE_TABLE_PICKER:
        schema_best = float(retrieval.get("schema_best_score") or 0.0)
        coverage_debug = retrieval.get("coverage_debug") if isinstance(retrieval.get("coverage_debug"), dict) else {}
        uncovered = coverage_debug.get("uncovered_fields")
        requested_fields = field_extraction.get("requested_fields") if isinstance(field_extraction.get("requested_fields"), list) else []
        coverage_ratio = float(coverage_debug.get("coverage_ratio") or 0.0)

        should_clarify_tables = (
            (not forced_tables)
            and (not intent_hint)
            and (schema_best < SCHEMA_RAG_MIN_SCORE)
            and (coverage_ratio < CLARIFY_TABLES_COVERAGE_MAX)
            and ((not requested_fields) or (isinstance(uncovered, list) and len(uncovered) > 0))
        )

        if should_clarify_tables:
            candidates = _build_clarification_candidates(retrieval, max_items=5)
            if not candidates:
                reply = (
                    "I couldn't confidently identify which part of the system this is about.\n"
                    "Can you tell me which module/area you mean (e.g., inventory, purchase, production, planning)?"
                )
            else:
                reply = (
                    "I’m not fully sure which area you meant. Is it one of these?\n\n"
                    f"{_format_table_choices(candidates)}\n\n"
                    "Reply with 1 / 2 / 3."
                )

            set_state(
                user_id,
                session_key,
                {
                    "mode": "choose_table",
                    "original_question": effective_question,
                    "candidates": candidates[:5],
                    "created_at": timezone.now().isoformat(),
                },
            )
            append_message(user_id, session_key, "user", message)
            append_message(user_id, session_key, "assistant", reply)
            return reply

    llm_schema_payload = _build_llm_schema_payload(schema_slice, retrieval, schema_types=schema_types)

    review_id = _safe_create_review_item(
        {
            "user": user,
            "session_key": session_key,
            "question": effective_question,
            "history": raw_history,
            "retrieval": retrieval,
            "schema_slice": llm_schema_payload,
            "status": "pending",
            "kind": "clarification" if clarification_used else "query",
        }
    )
    if review_id and dbctx:
        _safe_update_review_item(review_id, {"db_context": dbctx})

    spec: Any = None
    last_errors: List[str] = []
    used_tables: Set[str] = set()

    retries = 0
    while True:
        try:
            spec = llm_generate_queryspec(
                effective_question,
                history=history_for_llm,
                schema_slice=llm_schema_payload,
                intent_hint=intent_hint,
                forced_tables=forced_tables,
            )
        except Exception as e:
            err = f"LLM error: {e}"
            _safe_update_review_item(review_id, {"status": "error", "error": err})
            append_message(user_id, session_key, "user", message)
            append_message(user_id, session_key, "assistant", f"Error: {err}")
            return f"Error: {err}"

        _log_queryspec("RAW", retries, spec)
        _safe_update_review_item(review_id, {"proposed_queryspec": spec})

        if isinstance(spec, dict) and spec.get("clarify"):
            reply = str(spec.get("clarify"))
            set_state(
                user_id,
                session_key,
                {
                    "mode": "llm_clarify",
                    "original_question": effective_question,
                    "clarify": reply,
                    "created_at": timezone.now().isoformat(),
                },
            )
            append_message(user_id, session_key, "user", message)
            append_message(user_id, session_key, "assistant", reply)
            return reply

        # prune + patch + prune again (important)
        spec_pruned = minimize_queryspec(spec)
        spec_patched = _patch_spec_for_common_po_patterns(effective_question, spec_pruned, full_schema)
        spec_final = minimize_queryspec(spec_patched)

        # ✅ type-aware repair/coercion
        spec_typed = _apply_type_coercions_and_repairs(spec_final, full_schema, schema_types)

        # ✅ NEW: if user clarified the ID type, gently patch WHERE if needed
        if chosen_id_option:
            spec_typed = _apply_id_hint_patch(spec_typed, chosen_id_option)

        _log_queryspec("FINAL", retries, spec_typed)

        _safe_update_review_item(review_id, {"approved_queryspec": spec_typed})
        spec = spec_typed

        ok, errors, used_tables = _validate_queryspec_against_schema(spec, full_schema)
        last_errors = errors

        _safe_update_review_item(
            review_id,
            {
                "queryspec_validation_ok": ok,
                "queryspec_validation_errors": errors,
                "queryspec_used_tables": sorted(list(used_tables)),
            },
        )

        if ok:
            break

        if not SPEC_RETRY_ON_SCHEMA_MISS or retries >= SPEC_MAX_RETRIES:
            break

        retries += 1
        effective_question_retry = _augment_question_with_schema_errors(effective_question, errors)

        pack2 = get_schema_slice(effective_question_retry, forced_tables=forced_tables)
        schema_slice2 = pack2.get("schema") or schema_slice
        retrieval2 = pack2.get("retrieval") or retrieval

        llm_schema_payload = _build_llm_schema_payload(schema_slice2, retrieval2, schema_types=schema_types)
        retrieval = retrieval2

        _safe_update_review_item(
            review_id,
            {
                "retrieval_retry": retrieval2,
                "schema_slice_retry": llm_schema_payload,
                "question_retry": effective_question_retry,
            },
        )

    if last_errors:
        reply = (
            "I couldn't confidently map some of the requested fields to the database.\n"
            "Try rephrasing using simpler field names (e.g., 'PO number', 'customer name', 'date'), "
            "or tell me which record type you mean (purchase order, job card, stock item, etc.)."
        )
        append_message(user_id, session_key, "user", message)
        append_message(user_id, session_key, "assistant", reply)
        _safe_update_review_item(review_id, {"status": "error", "error": "Schema validation failed", "error_detail": last_errors})
        return reply

    try:
        sql, params = build_sql_from_queryspec(spec)

        if review_id:
            _safe_update_review_item(review_id, {"sql": sql, "sql_params": params})

        result = execute_readonly_sql(sql, params)

        if review_id:
            preview = None
            try:
                if isinstance(result, dict) and "rows" in result:
                    preview = (result.get("rows") or [])[:3]
                elif isinstance(result, list):
                    preview = result[:3]
            except Exception:
                preview = None
            if preview is not None:
                _safe_update_review_item(review_id, {"result_preview": preview})

    except Exception as e:
        _safe_update_review_item(
            review_id,
            {
                "status": "error",
                "error": f"SQL error: {e}",
            },
        )
        append_message(user_id, session_key, "user", message)
        reply = f"Error: {e}"
        append_message(user_id, session_key, "assistant", reply)
        return reply

    reply = llm_format_answer(effective_question, result, history=history_for_llm)

    append_message(user_id, session_key, "user", message)
    append_message(user_id, session_key, "assistant", reply)

    if clarification_used and chosen_table and isinstance(spec, dict):
        intent_payload = _intent_payload_from_queryspec(effective_question, chosen_table, spec)
        _safe_update_review_item(review_id, {"intent_payload": intent_payload})

    _safe_update_review_item(
        review_id,
        {
            "status": "complete",
            "answer": reply,
            "final_used_tables": sorted(list(used_tables)),
            "llm_schema_tables_count": len((llm_schema_payload.get("schema") or {}).keys()),
        },
    )
    return reply
