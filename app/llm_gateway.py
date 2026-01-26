# llm_gateway.py
import json
import os
import random
import re
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


# -------------------------
# Anchor date/time (Dubai by default)
# -------------------------
ANCHOR_TIMEZONE = (os.getenv("ANCHOR_TIMEZONE", "Asia/Dubai") or "Asia/Dubai").strip()
try:
    _ANCHOR_TZ = ZoneInfo(ANCHOR_TIMEZONE)
except Exception:
    _ANCHOR_TZ = timezone.utc

ANCHOR_NOW = datetime.now(_ANCHOR_TZ)
ANCHOR_DATE_STR = ANCHOR_NOW.strftime("%A, %B %d, %Y")
ANCHOR_DATE_ISO = ANCHOR_NOW.date().isoformat()


# -------------------------
# Logging / Debug
# -------------------------
LOG_QUERYSPEC = os.getenv("LOG_QUERYSPEC", "0").strip() in ("1", "true", "True", "yes", "YES")
LOG_QUERYSPEC_MAX_CHARS = int(os.getenv("LOG_QUERYSPEC_MAX_CHARS", "8000"))

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)


def _qs_dump(obj: Any) -> str:
    try:
        s = json.dumps(obj, indent=2, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > LOG_QUERYSPEC_MAX_CHARS:
        s = s[:LOG_QUERYSPEC_MAX_CHARS] + "\n...(truncated)"
    return s


def _log_queryspec(stage: str, spec: Any, attempt: Optional[int] = None) -> None:
    if not LOG_QUERYSPEC:
        return

    prefix = f"[QuerySpec:{stage}]"
    if attempt is not None:
        prefix += f"[attempt={attempt}]"

    msg = _qs_dump(spec)
    print(f"{prefix}\n{msg}\n", flush=True)
    logger.info("%s\n%s", prefix, msg)


# -------------------------
# Helps prevent "history pollution"
# -------------------------
HISTORY_DROP_TECH_ASSISTANT = os.getenv("HISTORY_DROP_TECH_ASSISTANT", "1").strip() in (
    "1",
    "true",
    "True",
    "yes",
    "YES",
)
HISTORY_TOPIC_FILTER = os.getenv("HISTORY_TOPIC_FILTER", "1").strip() in (
    "1",
    "true",
    "True",
    "yes",
    "YES",
)

# -------------------------
# Provider config (HF Router)
# -------------------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "hf").strip().lower()

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_BASE_URL = os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1").strip().rstrip("/")
HF_MODEL = os.getenv("HF_MODEL", "HuggingFaceTB/SmolLM3-3B:hf-inference").strip()

LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
LLM_RETRY_MAX = int(os.getenv("LLM_RETRY_MAX", "4"))
LLM_RETRY_BASE_SLEEP = float(os.getenv("LLM_RETRY_BASE_SLEEP", "1.2"))

HISTORY_MAX_FOR_LLM = int(os.getenv("CHAT_HISTORY_MAX_FOR_LLM", "12"))
LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "1").strip() in ("1", "true", "True", "yes", "YES")

TABLE_DESCRIPTIONS_PATH = os.getenv("TABLE_DESCRIPTIONS_PATH", "").strip()
TABLE_DESCRIPTIONS_RELOAD = os.getenv("TABLE_DESCRIPTIONS_RELOAD", "0").strip() in (
    "1",
    "true",
    "True",
    "yes",
    "YES",
)

ANSWER_WITH_LLM = os.getenv("ANSWER_WITH_LLM", "0").strip() in ("1", "true", "True", "yes", "YES")
ANSWER_MAX_ROWS = int(os.getenv("ANSWER_MAX_ROWS", "25"))
ANSWER_DEDUP_ROWS = os.getenv("ANSWER_DEDUP_ROWS", "1").strip() in ("1", "true", "True", "yes", "YES")

QUERY_SPEC_MAX_RETRIES = int(os.getenv("QUERY_SPEC_MAX_RETRIES", "2"))
QUERY_SPEC_MAX_REPAIR_TRIES = int(os.getenv("QUERY_SPEC_MAX_REPAIR_TRIES", "1"))
CLARIFY_LOOP_MAX = int(os.getenv("CLARIFY_LOOP_MAX", "1"))

# --- keep ID candidate payload small ---
ID_HINTS_MAX_IDS = int(os.getenv("ID_HINTS_MAX_IDS", "4"))
ID_HINTS_MAX_CANDIDATES_PER_ID = int(os.getenv("ID_HINTS_MAX_CANDIDATES_PER_ID", "10"))

# --- NEW: LLM mismatch repair + conservative fallbacks (configurable) ---
LLM_REPAIR_ON_MISMATCH = os.getenv("LLM_REPAIR_ON_MISMATCH", "1").strip() in ("1", "true", "True", "yes", "YES")
LLM_REPAIR_MAX_TRIES = int(os.getenv("LLM_REPAIR_MAX_TRIES", "2"))

# last-resort deterministic patches if LLM doesn't repair (still schema/type-safe)
EXEC_FALLBACK_CASE_INSENSITIVE = os.getenv("EXEC_FALLBACK_CASE_INSENSITIVE", "1").strip() in (
    "1",
    "true",
    "True",
    "yes",
    "YES",
)
EXEC_FALLBACK_AGG = os.getenv("EXEC_FALLBACK_AGG", "1").strip() in ("1", "true", "True", "yes", "YES")

_TABLE_DESC_CACHE: Optional[Dict[str, str]] = None


# -------------------------
# Clarify / hallucination guards
# -------------------------
_TECH_RE = re.compile(r"\b(table|column|schema|join|sql|database)\b|app_[a-z0-9_]+", re.I)
_PO_RE = re.compile(r"\b(po|purchase\s*order|purchase\s*orders)\b", re.I)

_PO_DETAIL_CLARIFY_RE = re.compile(
    r"(including\s+all\s+line\s+items|line\s+items?|purchase\s+orders?\s+themselves|one\s+row\s+per\s+po|totals?|summary|detailed)",
    re.I,
)

_STOCK_RE = re.compile(r"\b(stock|current\s+stock|inventory|store\s*item|m\s*seal)\b", re.I)


# -------------------------
# Relative time handling
# -------------------------
_RELATIVE_DAYS_TOKEN_RE = re.compile(r"^\s*(?:RELATIVE_DAYS)\s*:\s*(\d{1,6})\s*$", re.I)
_RELATIVE_HOURS_TOKEN_RE = re.compile(r"^\s*(?:RELATIVE_HOURS)\s*:\s*(\d{1,7})\s*$", re.I)

_SQL_NOW_INTERVAL_DAYS_RE = re.compile(
    r"^\s*(?:now(?:\(\))?[0-9]*|current_timestamp|current_date)\s*-\s*interval\s*'(\d{1,6})\s*day(?:s)?'\s*$",
    re.I,
)
_SQL_NOW_INTERVAL_HOURS_RE = re.compile(
    r"^\s*(?:now(?:\(\))?[0-9]*|current_timestamp)\s*-\s*interval\s*'(\d{1,7})\s*hour(?:s)?'\s*$",
    re.I,
)

_LAST_N_DAYS_RE = re.compile(r"\b(?:last|past|previous|within\s+the\s+last)\s+(\d{1,6})\s+day(?:s)?\b", re.I)
_LAST_N_HOURS_RE = re.compile(r"\b(?:last|past|previous|within\s+the\s+last)\s+(\d{1,7})\s+hour(?:s)?\b", re.I)


def _dt_ago(amount: int, *, unit: str, start_of_day: bool = False) -> datetime:
    dt = datetime.now(_ANCHOR_TZ)
    if start_of_day:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "days":
        return dt - timedelta(days=amount)
    if unit == "hours":
        return dt - timedelta(hours=amount)
    return dt


def _normalize_relative_value(v: Any) -> Any:
    if isinstance(v, list):
        return [_normalize_relative_value(x) for x in v]

    if isinstance(v, dict):
        if "relative_days" in v:
            try:
                return _dt_ago(int(v["relative_days"]), unit="days")
            except Exception:
                return v
        if "relative_hours" in v:
            try:
                return _dt_ago(int(v["relative_hours"]), unit="hours")
            except Exception:
                return v

        kind = str(v.get("kind") or "").strip().lower()
        if kind == "relative_days" and "days" in v:
            try:
                return _dt_ago(int(v["days"]), unit="days")
            except Exception:
                return v
        if kind == "relative_hours" and "hours" in v:
            try:
                return _dt_ago(int(v["hours"]), unit="hours")
            except Exception:
                return v
        return v

    if isinstance(v, str):
        s = v.strip()

        m = _RELATIVE_DAYS_TOKEN_RE.match(s)
        if m:
            return _dt_ago(int(m.group(1)), unit="days")

        m = _RELATIVE_HOURS_TOKEN_RE.match(s)
        if m:
            return _dt_ago(int(m.group(1)), unit="hours")

        m = _SQL_NOW_INTERVAL_DAYS_RE.match(s)
        if m:
            days = int(m.group(1))
            start_of_day = "current_date" in s.lower()
            return _dt_ago(days, unit="days", start_of_day=start_of_day)

        m = _SQL_NOW_INTERVAL_HOURS_RE.match(s)
        if m:
            hours = int(m.group(1))
            return _dt_ago(hours, unit="hours")

        m = _LAST_N_DAYS_RE.search(s)
        if m:
            return _dt_ago(int(m.group(1)), unit="days")

        m = _LAST_N_HOURS_RE.search(s)
        if m:
            return _dt_ago(int(m.group(1)), unit="hours")

    return v


def _normalize_relative_time_filters_in_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        return spec

    out = dict(spec)
    for key in ("where", "having"):
        arr = out.get(key)
        if isinstance(arr, list):
            norm = []
            for cond in arr:
                if isinstance(cond, dict) and "value" in cond:
                    cc = dict(cond)
                    cc["value"] = _normalize_relative_value(cc.get("value"))
                    norm.append(cc)
                else:
                    norm.append(cond)
            out[key] = norm
    return out


def _is_technical_text(text: str) -> bool:
    return bool(_TECH_RE.search(text or ""))


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _tokenize(s: str) -> List[str]:
    s = (s or "").lower()
    words = re.findall(r"[a-z0-9]+", s)
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "for",
        "in",
        "on",
        "with",
        "me",
        "show",
        "latest",
        "last",
        "top",
        "please",
        "get",
        "give",
        "list",
        "all",
        "only",
        "just",
        "is",
        "are",
        "was",
        "were",
        "be",
        "as",
        "by",
        "from",
        "this",
        "that",
        "it",
    }
    return [w for w in words if w not in stop]


def _overlap_ratio(a: str, b: str) -> float:
    ta = set(_tokenize(a))
    tb = set(_tokenize(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    denom = max(len(ta), len(tb))
    return inter / float(denom)


def _clarify_seen_before(clarify: str, history: Optional[List[Dict[str, Any]]]) -> bool:
    if not clarify:
        return False
    c = _normalize_ws(clarify)
    for m in (history or []):
        if (m.get("role") or "").strip() != "assistant":
            continue
        if c and c in _normalize_ws(m.get("content") or ""):
            return True
    return False


def _last_po_detail_clarify_and_prior_question(history: Optional[List[Dict[str, Any]]]) -> Tuple[bool, Optional[str]]:
    if not history:
        return False, None

    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if (msg.get("role") or "").strip() != "assistant":
            continue
        content = msg.get("content") or ""
        if _PO_DETAIL_CLARIFY_RE.search(content or ""):
            for j in range(i - 1, -1, -1):
                if (history[j].get("role") or "").strip() == "user":
                    return True, (history[j].get("content") or "")
            return True, None
        break

    return False, None


def _user_answered_po_detail(user_text: str) -> Optional[str]:
    t = (user_text or "").lower()
    if any(k in t for k in ("line item", "line items", "detailed", "per item", "items")):
        return "line_items"
    if any(
        k in t
        for k in ("summary", "totals", "one row per po", "purchase orders themselves", "just the po", "only po")
    ):
        return "po_headers"
    return None


def _po_default_assumption(question: str) -> Optional[str]:
    q = (question or "").lower()
    if not _PO_RE.search(q):
        return None
    has_qty = ("quantity" in q) or ("qty" in q)
    has_price = ("price" in q) or ("rate" in q) or ("amount" in q) or ("value" in q)
    if has_qty or has_price:
        return "line_items"
    return "po_headers"


def _infer_limit(question: str, default: int = 25) -> int:
    q = (question or "").lower()
    m = re.search(r"\b(latest|last|top)\s+(\d{1,3})\b", q)
    if m:
        try:
            n = int(m.group(2))
            return max(1, min(50, n))
        except Exception:
            return default
    return default


def _fallback_business_clarify(question: str) -> str:
    if _po_default_assumption(question) == "line_items":
        return ""
    if _PO_RE.search(question or ""):
        return "Quick check: do you want **one row per PO** (summary) or **one row per PO item** (detailed line items)?"
    return "Quick check: do you want a **summary** (totals) or a **detailed list** (individual records)?"


def _load_table_descriptions() -> Dict[str, str]:
    global _TABLE_DESC_CACHE

    if _TABLE_DESC_CACHE is not None and not TABLE_DESCRIPTIONS_RELOAD:
        return _TABLE_DESC_CACHE

    if TABLE_DESCRIPTIONS_PATH:
        p = Path(TABLE_DESCRIPTIONS_PATH).expanduser().resolve()
    else:
        p = Path(__file__).resolve().parent / "table_descriptions.txt"

    if not p.exists():
        _TABLE_DESC_CACHE = {}
        return _TABLE_DESC_CACHE

    out: Dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = (k or "").strip()
        v = (v or "").strip()
        if k and v:
            out[k] = v

    _TABLE_DESC_CACHE = out
    return _TABLE_DESC_CACHE


def _table_meanings_for_schema(schema: Dict[str, Any]) -> Dict[str, str]:
    desc = _load_table_descriptions()
    out: Dict[str, str] = {}
    for t in (schema or {}).keys():
        if t in desc:
            out[t] = desc[t]
    return out


# -------------------------
# ✅ ID / ambiguity helpers (business-friendly)
# -------------------------
_ID_TOKEN_RE = re.compile(r"\b([A-Za-z]{1,12}[-_ ]?\d{1,12}[A-Za-z0-9]{0,8})\b")


def _extract_id_tokens(text: str) -> List[str]:
    """
    Extract business IDs like JC001, PO-500, INV 12, etc.
    Keep small to prevent prompt bloat.
    """
    if not text:
        return []
    found = []
    for m in _ID_TOKEN_RE.finditer(text):
        tok = (m.group(1) or "").strip()
        tok = re.sub(r"\s+", "", tok)  # normalize "PO 12" -> "PO12"
        if tok and tok not in found:
            found.append(tok)
        if len(found) >= max(1, ID_HINTS_MAX_IDS):
            break
    return found


def _id_has_alpha_and_digits(tok: str) -> bool:
    return bool(re.search(r"[A-Za-z]", tok or "")) and bool(re.search(r"\d", tok or ""))


def _col_is_identifier_like(col: str) -> bool:
    c = (col or "").lower()
    if not c:
        return False
    return any(
        p in c
        for p in (
            "_no",
            "_num",
            "_number",
            "_ref",
            "_code",
            "_id",
            "pono",
            "po_no",
            "job",
            "jc",
            "invoice",
            "inv",
            "item_code",
            "drawing",
        )
    )


def _extract_types(schema_slice: Any) -> Dict[str, Dict[str, str]]:
    """
    schema_slice may look like:
      {"schema": {...}, "types": {...}, "fk_edges": [...]}
    """
    if isinstance(schema_slice, dict) and isinstance(schema_slice.get("types"), dict):
        out = schema_slice.get("types") or {}
        cleaned: Dict[str, Dict[str, str]] = {}
        for t, m in out.items():
            if isinstance(m, dict):
                cleaned[str(t)] = {str(k): str(v) for k, v in m.items() if k and v}
        return cleaned
    return {}


def _safe_label_from_meaning(s: str) -> str:
    x = re.sub(r"\s+", " ", (s or "").strip())
    x = re.sub(r"[\[\]\(\)\{\}]+", " ", x).strip()
    if len(x) > 46:
        x = x[:46].rstrip() + "…"
    return x


def _record_type_options(table_meanings: Dict[str, str]) -> List[str]:
    seen = set()
    out = []
    for _, v in (table_meanings or {}).items():
        lab = _safe_label_from_meaning(v)
        if not lab:
            continue
        low = lab.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(lab)
        if len(out) >= 8:
            break
    return out


def _find_id_candidates_in_schema(
    tok: str,
    *,
    schema: Dict[str, Any],
    types: Dict[str, Dict[str, str]],
    table_meanings: Dict[str, str],
    forced_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    forced = [t for t in (forced_tables or []) if isinstance(t, str) and t.strip()]
    tables = list(schema.keys())
    if forced:
        tables = [t for t in tables if t in forced] or tables

    candidates: List[Dict[str, Any]] = []

    for t in tables:
        cols = schema.get(t)
        if not isinstance(cols, list):
            continue
        t_types = types.get(t) or {}
        for c in cols:
            col = str(c)
            if not _col_is_identifier_like(col):
                continue
            ctype = str(t_types.get(col) or "").lower()

            score = 0
            if _id_has_alpha_and_digits(tok):
                if ctype in ("text", "varchar", "character varying", "char", "citext", "uuid"):
                    score += 5
                elif ctype == "int":
                    score -= 1
            if any(x in col.lower() for x in ("_ref", "_code", "_no", "_num", "_number")):
                score += 2
            meaning = (table_meanings.get(t) or "").lower()
            if meaning and tok[:2].lower() in meaning:
                score += 1

            candidates.append(
                {
                    "table": t,
                    "table_label": _safe_label_from_meaning(table_meanings.get(t) or t),
                    "column": col,
                    "type": ctype or None,
                    "score": score,
                }
            )

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    candidates = candidates[: max(1, ID_HINTS_MAX_CANDIDATES_PER_ID)]

    opts: List[str] = []
    seen = set()
    for c in candidates:
        lab = (c.get("table_label") or "").strip()
        low = lab.lower()
        if lab and low not in seen and not low.startswith("app_"):
            seen.add(low)
            opts.append(lab)
        if len(opts) >= 5:
            break

    return {
        "id": tok,
        "candidates": candidates,
        "record_type_options_from_candidates": opts,
    }


def _fallback_id_clarify(tok: str, record_type_options: List[str]) -> str:
    opts = [o for o in (record_type_options or []) if o][:5]
    if opts:
        joined = " / ".join(opts[:5])
        return f"Quick check: does **{tok}** refer to {joined}?"
    return f"Quick check: what does **{tok}** refer to (e.g., Purchase Order / Job Card / Customer / Item)?"


def _pick_best_id_to_clarify(id_hints: List[Dict[str, Any]]) -> Optional[str]:
    if not id_hints:
        return None
    best = None
    best_score = -1
    for h in id_hints:
        opts = h.get("record_type_options_from_candidates") or []
        score = len({(o or "").lower() for o in opts if o})
        if score > best_score:
            best_score = score
            best = h.get("id")
    return best


# -------------------------
# Prompts
# -------------------------
FIELD_EXTRACT_SYSTEM = f"""You extract what the user is asking for (requested output fields + constraints).
Return ONLY valid JSON. No markdown. No extra text.

You will receive JSON with:
- question: current user message
- history: recent chat messages (may be empty)

Hard rules:
- DO NOT mention database tables or database column names.
- DO NOT generate SQL.
- DO NOT assume any schema.
- Keep it short and literal. Prefer copying the user's wording.

ANCHOR DATE: {ANCHOR_DATE_STR} (Timezone: {ANCHOR_TIMEZONE})

DATE RESOLUTION RULES:
- Today is {ANCHOR_DATE_STR}.
- Resolve relative phrases like "today", "yesterday", "last month", "last 30 days" against the anchor date.
- If the user mentions a day number (e.g., "the 12th") and that day has NOT happened yet in the current month, interpret it as the 12th of the previous month.
- If the user mentions a month (e.g., "August") and that month has NOT happened yet this year, interpret it as August of the previous year.
- If a year is given, respect that year strictly.

IDENTIFIER RECOGNITION:
- Treat alphanumeric strings (e.g., "D100", "PO-500", "JC12") as unique IDs.

SPECIFICITY & CLARIFICATION RULES:
- If the user asks a broad question (e.g., "Which customers..."), DO NOT set needs_clarification=true.
- Only set needs_clarification=true if the question is missing a core entity (e.g., "Show me the status" but doesn't say of what).

IMPORTANT:
- The JSON format below is ONLY A SAMPLE SHAPE, not a strict schema.
- You MUST include at least:
  requested_fields, constraints, filters_text, is_followup, needs_clarification, clarify.
"""

DATE_HEADER = f"""
ANCHOR DATE: {ANCHOR_DATE_STR} (Timezone: {ANCHOR_TIMEZONE})
ANCHOR DATE (ISO): {ANCHOR_DATE_ISO}

DATE RESOLUTION RULES:
- Resolve relative terms (today/yesterday/last N days) using the anchor date and timezone.
- Return date values as ISO timestamps or ISO dates (YYYY-MM-DD) when appropriate.
"""

QUERY_SPEC_SYSTEM_BASE = """You convert user questions into a SAFE JSON QuerySpec for PostgreSQL.
Return ONLY valid JSON. No markdown. No extra text.

NON-TECHNICAL USER RULE:
- The end user is NOT technical. NEVER ask them about tables, columns, schema, joins, SQL, or databases.
- Do NOT use the words: "table", "column", "schema", "join", "sql", "database" in ANY clarification.
- You MUST choose the correct source + relationships automatically using the provided schema + fk_edges.

PAYLOAD YOU RECEIVE (JSON):
- question: string
- history: list of {role, content}
- schema: { "<table>": ["col1","col2", ...], ... }  (ONLY use what is provided)
- types: { "<table>": { "<col>": "<type>" } }      (use to avoid type mistakes)
- fk_edges: list of relationships
- intent_hint: optional guidance (may be empty)
- forced_tables: list of allowed base tables (may be empty)
- table_meanings: { "<table>": "<business label>", ... } (use labels for business wording; DO NOT show raw table names)
- id_hints: optional list of hints about IDs found in the question

FORCED TABLE RULE:
- If forced_tables is not empty, the FROM table MUST be one of forced_tables.

DEFAULTS (avoid pointless clarification):
- If question is about Purchase Orders and asks for "quantity" and/or "price", assume the user wants
  LINE ITEMS (detailed rows). Do NOT ask.
- If the question is broad, assume they want ALL relevant records.
- If the user does not specify a limit or sort, default to the most recent 50 records.

IMPORTANT FOR TIME RANGES:
- If the question says "last N days/hours", DO NOT put SQL expressions in a string like "now() - interval '30 days'".
- Instead, set the filter value to a token like:
    "RELATIVE_DAYS:N" or "RELATIVE_HOURS:N"

TEXT MATCHING RULES (IMPORTANT):
- PostgreSQL '=' for text is case-sensitive.
- For human names (worker/customer/operator) and similar text labels, use ILIKE for CASE-INSENSITIVE matching.
- If user is asking for an exact name match, use ILIKE WITHOUT adding '%' wildcards (example: worker_name ILIKE "sreyas").
- Use wildcards ONLY if the user asks for contains/search/matching or explicitly provides wildcards ('*','?','%','_').

AGGREGATION RULES (IMPORTANT):
- If the user asks for totals/sum/how many/count/average/min/max, you MUST use select[].agg accordingly
  (sum/count/avg/min/max).
- If the question is aggregate-only (just asking for a single total/count), return only the aggregated select,
  set limit to 1, and do NOT include pointless order_by.

ID / CODE RULES (IMPORTANT):
- If the user provides an alphanumeric ID (letters+digits), prefer matching it to TEXT-like identifier fields
  (e.g., *_ref, *_code, *_no stored as text). Avoid forcing it into an integer field.
- Use types to avoid mistakes (e.g., don't compare a string like "JC001" to an int column).
- If the business meaning is genuinely ambiguous (e.g., the ID could refer to different record types),
  ask ONE business clarification using record-type labels (from table_meanings), NOT table names.
  Example style: "Quick check: does JC001 refer to Purchase Order / Job Card / Customer / Item?"

Clarification rule:
- If you truly must clarify, return exactly:
  {"clarify":"<one short business question>"}

QuerySpec format you MUST output:
{
  "from": "<table>",
  "select": [{"table":"<table>","column":"<col>","agg":null,"alias":null}],
  "joins": [
    {
      "type": "left",
      "table": "<table>",
      "on": [
        {
          "left_table": "<table>",
          "left_column": "<col>",
          "op": "=",
          "right_table": "<table>",
          "right_column": "<col>"
        }
      ]
    }
  ],
  "where": [],
  "group_by": [],
  "having": [],
  "order_by": [{"table":"<table>","column":"<col>","dir":"asc"}],
  "distinct": false,
  "limit": 50
}

Hard rules:
- ONLY SELECT-style queries.
- Use ONLY tables/columns from schema.
- Always include limit <= 50.
- Use the FEWEST tables possible; add relationships only when required.
"""

QUERY_SPEC_SYSTEM = DATE_HEADER + "\n" + QUERY_SPEC_SYSTEM_BASE

QUERY_SPEC_REPAIR_SYSTEM = DATE_HEADER + """
You repair an existing QuerySpec to match the user's question more accurately.
Return ONLY a valid QuerySpec JSON object. No markdown. No extra text.

You will receive JSON with:
- question
- history
- schema, types, fk_edges
- forced_tables
- table_meanings
- id_hints
- previous_spec: the earlier QuerySpec
- issue: a short description of what is wrong and what to fix

Rules:
- Make the SMALLEST change needed to fix the issue.
- Keep the same intent and filters unless the issue explicitly requires changing them.
- Follow the same non-technical user rule: never ask about tables/columns in clarify.
- If you must clarify, return only {"clarify":"..."} (business wording only).
"""

ANSWER_SYSTEM = """You are the company's internal assistant.
Return a helpful, concise answer.

CRITICAL: NEVER INVENT VALUES.
- Use ONLY the provided result rows and columns.
- If a field is not present in result.columns, say "Not available" (do NOT guess).

You will receive JSON with:
- question: current user message
- history: recent chat messages (may be empty)
- result: SQL output with {columns: [...], rows: [[...], ...]}

Do NOT mention database table names unless the user explicitly asks about tables/schema.
"""


# -------------------------
# Helpers
# -------------------------
def _extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise
        obj = json.loads(m.group(0))

    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object, got {type(obj).__name__}")
    return obj


def _trim_history(history: Optional[List[Dict[str, Any]]], *, question: Optional[str] = None) -> List[Dict[str, Any]]:
    hist = history or []
    cleaned: List[Dict[str, str]] = []

    q = (question or "").strip()
    is_po = bool(_PO_RE.search(q))
    is_stock = bool(_STOCK_RE.search(q))

    for m in hist:
        role = (m.get("role") or "").strip()
        content = (m.get("content") or "").strip()
        if not role or not content:
            continue

        if HISTORY_DROP_TECH_ASSISTANT and role == "assistant" and _is_technical_text(content):
            continue

        if HISTORY_TOPIC_FILTER and q:
            if is_po:
                if _STOCK_RE.search(content) and not _PO_RE.search(content):
                    continue
            elif is_stock:
                if _PO_RE.search(content) and not _STOCK_RE.search(content):
                    continue

        cleaned.append({"role": role, "content": content})

    return cleaned[-HISTORY_MAX_FOR_LLM:]


def _hf_call(messages: List[Dict[str, str]], temperature: float = 0.1, expect_json: bool = False) -> str:
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is not set (Hugging Face access token required).")

    url = f"{HF_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }

    payload: Dict[str, Any] = {
        "model": HF_MODEL,
        "messages": messages,
        "temperature": temperature,
    }

    if expect_json and LLM_JSON_MODE:
        payload["response_format"] = {"type": "json_object"}

    last_err: Optional[str] = None

    for attempt in range(LLM_RETRY_MAX + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=LLM_TIMEOUT_SECONDS)

            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"{r.status_code} {r.text}"
                if attempt >= LLM_RETRY_MAX:
                    r.raise_for_status()
                sleep_s = (LLM_RETRY_BASE_SLEEP * (2**attempt)) + random.uniform(0, 0.25)
                time.sleep(sleep_s)
                continue

            if r.status_code >= 400:
                raise RuntimeError(f"HF Router error {r.status_code}: {r.text}")

            data = r.json()
            try:
                return data["choices"][0]["message"]["content"]
            except Exception:
                raise RuntimeError(f"Unexpected HF Router response shape: {data}")

        except requests.RequestException as e:
            last_err = str(e)
            if attempt >= LLM_RETRY_MAX:
                raise
            sleep_s = (LLM_RETRY_BASE_SLEEP * (2**attempt)) + random.uniform(0, 0.25)
            time.sleep(sleep_s)

    raise RuntimeError(last_err or "Unknown HF Router error")


def _llm_call(messages: List[Dict[str, str]], temperature: float, expect_json: bool) -> str:
    if LLM_PROVIDER != "hf":
        raise RuntimeError(f"Unsupported LLM_PROVIDER={LLM_PROVIDER}. Expected 'hf'.")
    return _hf_call(messages, temperature=temperature, expect_json=expect_json)


def llm_call_json(system_prompt: str, user_payload: Dict[str, Any], temperature: float = 0.1) -> Dict[str, Any]:
    content = _llm_call(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ],
        temperature=temperature,
        expect_json=True,
    )

    try:
        return _extract_json(content)
    except Exception:
        repair_prompt = "Fix the following into ONLY valid JSON object. No markdown. No explanation.\n\n" f"{content}"
        repaired = _llm_call(
            [
                {"role": "system", "content": "Return ONLY valid JSON object."},
                {"role": "user", "content": repair_prompt},
            ],
            temperature=0.0,
            expect_json=True,
        )
        return _extract_json(repaired)


def _extract_schema(schema_slice: Any) -> Dict[str, Any]:
    if isinstance(schema_slice, dict) and "schema" in schema_slice:
        return schema_slice.get("schema") or {}
    return schema_slice if isinstance(schema_slice, dict) else {}


def _extract_fk_edges(schema_slice: Any) -> List[Dict[str, Any]]:
    if not isinstance(schema_slice, dict):
        return []
    if "fk_edges" in schema_slice and isinstance(schema_slice.get("fk_edges"), list):
        return schema_slice.get("fk_edges") or []
    retrieval = schema_slice.get("retrieval") if isinstance(schema_slice.get("retrieval"), dict) else {}
    edges = retrieval.get("fk_edges")
    return edges if isinstance(edges, list) else []


def _is_valid_queryspec(spec: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(spec, dict):
        return False, "spec is not a dict"
    if spec.get("clarify"):
        if not isinstance(spec.get("clarify"), str):
            return False, "clarify is not a string"
        return True, "clarify"
    from_t = spec.get("from")
    if not isinstance(from_t, str) or not from_t.strip():
        return False, "missing from"
    if not isinstance(spec.get("select"), list) or not spec["select"]:
        return False, "missing select"
    lim = spec.get("limit")
    if not isinstance(lim, int) or lim < 1 or lim > 50:
        return False, "invalid limit"
    for k in ("joins", "where", "group_by", "having", "order_by"):
        if k in spec and not isinstance(spec.get(k), list):
            return False, f"{k} not list"
    return True, "ok"


def _sanitize_intent_hint(intent_hint: Optional[Dict[str, Any]], question: str) -> Optional[Dict[str, Any]]:
    if not isinstance(intent_hint, dict) or not intent_hint:
        return None

    exq = str(intent_hint.get("example_question") or "").strip()
    q = (question or "").strip()

    if exq and q:
        if _overlap_ratio(exq, q) < 0.20:
            return None

    pf = intent_hint.get("preferred_filters")
    if isinstance(pf, list) and q:
        ql = q.lower()
        kept = []
        for f in pf:
            if not isinstance(f, dict):
                continue
            val = f.get("value")
            if isinstance(val, str) and val.strip():
                if val.strip().lower() not in ql:
                    continue
            kept.append(f)
        intent_hint = dict(intent_hint)
        intent_hint["preferred_filters"] = kept

    return intent_hint


def _parse_qualified_col(expr: str) -> Optional[Tuple[str, str]]:
    expr = (expr or "").strip()
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$", expr)
    if not m:
        return None
    return m.group(1), m.group(2)


_ALLOWED_AGGS_GATEWAY = {"count", "sum", "avg", "min", "max"}
_ALLOWED_FILTER_OPS_GATEWAY = {
    "=",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "ilike",
    "like",
    "in",
    "between",
    "is_null",
    "is_not_null",
}

_ALIAS_CLEAN_RE = re.compile(r"[^A-Za-z0-9_]+")
_MULTI_US_RE = re.compile(r"_+")


def _norm_alias(a: Any) -> Optional[str]:
    if not isinstance(a, str):
        return None
    s = a.strip()
    if not s:
        return None
    s = _ALIAS_CLEAN_RE.sub("_", s)
    s = _MULTI_US_RE.sub("_", s).strip("_").lower()
    return s or None


def _question_implies_text_search(question: str) -> bool:
    q = (question or "").lower()
    return bool(re.search(r"\b(contains?|like|search|matching|match|similar)\b", q))


def _looks_like_exact_id(val: str) -> bool:
    if not isinstance(val, str):
        return False
    s = val.strip()
    return bool(re.search(r"[A-Za-z]", s)) and bool(re.search(r"\d", s))


def _normalize_pattern_value(v: Any) -> Any:
    """
    - supports '*' wildcard -> '%'
    - supports '?' wildcard -> '_'
    - if no wildcards at all, keep as plain
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return s
    if "*" in s or "?" in s:
        s = s.replace("*", "%").replace("?", "_")
    return s


def _wrap_like_value(v: Any, *, force_contains: bool) -> Any:
    """
    If force_contains is True and value has no SQL wildcards, treat as contains: '%v%'.
    If force_contains is False, keep exact (unless user already provided wildcards).
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return s
    s = _normalize_pattern_value(s)
    if force_contains and "%" not in s and "_" not in s:
        return f"%{s}%"
    return s


_TEXT_TYPES = {"text", "varchar", "character varying", "char", "citext", "uuid"}


def _col_is_name_like(col: str) -> bool:
    c = (col or "").lower().strip()
    if not c:
        return False
    # conservative: only "name" fields, not codes/refs
    return ("name" in c) and not any(x in c for x in ("code", "ref", "no", "num", "number", "id"))


def _value_looks_like_human_name(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s:
        return False
    if re.search(r"\d", s):
        return False
    # allow spaces, dots, hyphens (e.g., "A. B", "Mary-Jane")
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,64}", s))


def _normalize_filter_ops_and_ilike(
    spec: Dict[str, Any],
    *,
    schema: Dict[str, Any],
    types: Dict[str, Dict[str, str]],
    question: str,
) -> Dict[str, Any]:
    """
    Normalize filter ops + ensure ilike/like patterns have wildcards ONLY when intended.
    Also upgrades '=' -> 'ilike' for contains-search ONLY when question implies text search.
    (Exact case-insensitive name matching is handled via LLM repair loop + optional fallback.)
    """
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    q_search = _question_implies_text_search(question)

    out = dict(spec)
    base = str(out.get("from") or "").strip()

    def _ctype(table: str, col: str) -> str:
        return str((types or {}).get(table, {}).get(col) or "").strip().lower()

    def _norm_cond_list(arr: Any) -> Any:
        if not isinstance(arr, list):
            return arr
        norm = []
        for cond in arr:
            if not isinstance(cond, dict):
                norm.append(cond)
                continue

            c = dict(cond)
            op_raw = str(c.get("op") or "=").strip().lower()

            op_map = {
                "eq": "=",
                "equals": "=",
                "neq": "!=",
                "ne": "!=",
                "gt": ">",
                "gte": ">=",
                "lt": "<",
                "lte": "<=",
                "contains": "ilike",
                "icontains": "ilike",
                "startswith": "ilike",
                "istartswith": "ilike",
                "endswith": "ilike",
                "iendswith": "ilike",
                "null": "is_null",
                "not_null": "is_not_null",
            }
            op = op_map.get(op_raw, op_raw)
            c["op"] = op

            t = str(c.get("table") or base).strip()
            col = str(c.get("column") or "").strip()
            if t:
                c["table"] = t
            if col:
                c["column"] = col

            # normalize wildcard syntax
            if "value" in c:
                c["value"] = _normalize_pattern_value(c.get("value"))

            if op == "between":
                v = c.get("value")
                if isinstance(v, dict) and ("from" in v or "to" in v):
                    c["value"] = [v.get("from"), v.get("to")]

            if op == "in":
                v = c.get("value")
                if isinstance(v, str) and "," in v:
                    parts = [p.strip() for p in v.split(",") if p.strip()]
                    if parts:
                        c["value"] = parts

            # startswith/endswith expansion
            if op_raw in ("startswith", "istartswith"):
                v = c.get("value")
                if isinstance(v, str):
                    v = _normalize_pattern_value(v)
                    if "%" not in v and "_" not in v:
                        c["value"] = f"{v}%"
            elif op_raw in ("endswith", "iendswith"):
                v = c.get("value")
                if isinstance(v, str):
                    v = _normalize_pattern_value(v)
                    if "%" not in v and "_" not in v:
                        c["value"] = f"%{v}"

            # If LLM chose ilike/like, only force contains if user implied search/contains or used contains op
            if op in ("ilike", "like"):
                force_contains = op_raw in ("contains", "icontains") or q_search
                c["value"] = _wrap_like_value(c.get("value"), force_contains=force_contains)

            # Upgrade '=' to ilike ONLY for search-like questions (contains/matching),
            # and only for non-ID strings.
            if op == "=" and q_search:
                v = c.get("value")
                if isinstance(v, str) and v.strip() and not _looks_like_exact_id(v):
                    # keep exact unless search: convert to ilike with contains
                    c["op"] = "ilike"
                    c["value"] = _wrap_like_value(v, force_contains=True)

            if str(c.get("op") or "").strip().lower() not in _ALLOWED_FILTER_OPS_GATEWAY:
                continue

            norm.append(c)
        return norm

    out["where"] = _norm_cond_list(out.get("where"))
    out["having"] = _norm_cond_list(out.get("having"))
    return out


# -------------------------
# ✅ Aggregate helpers (SUM/AVG/MIN/MAX + auto GROUP BY)
# -------------------------
_NUMERIC_TYPES = {
    "int",
    "integer",
    "bigint",
    "smallint",
    "numeric",
    "decimal",
    "real",
    "double precision",
    "float",
}
_DATE_TYPES = {
    "date",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
    "timestamptz",
}


def _schema_cols(schema: Dict[str, Any], table: str) -> List[str]:
    cols = schema.get(table) if isinstance(schema, dict) else None
    if not isinstance(cols, list):
        return []
    return [str(c) for c in cols]


def _pick_best_measure_column(
    table: str,
    *,
    schema: Dict[str, Any],
    types: Dict[str, Dict[str, str]],
    agg: str,
) -> Optional[str]:
    cols = _schema_cols(schema, table)
    if not cols:
        return None

    t_types = (types or {}).get(table) or {}

    def ctype(c: str) -> str:
        return str(t_types.get(c) or "").strip().lower()

    preferred_numeric_names = ("total", "amount", "value", "price", "rate", "qty", "quantity", "cost")
    preferred_date_names = ("created_at", "created_on", "created_date", "updated_at", "date", "po_date", "timestamp")

    if agg in ("sum", "avg"):
        for name in preferred_numeric_names:
            for c in cols:
                if name in c.lower() and ctype(c) in _NUMERIC_TYPES:
                    return c
        for c in cols:
            if ctype(c) in _NUMERIC_TYPES:
                return c
        return None

    if agg in ("min", "max"):
        for name in preferred_date_names:
            for c in cols:
                if name in c.lower() and ctype(c) in (_DATE_TYPES | _NUMERIC_TYPES):
                    return c
        for c in cols:
            if ctype(c) in _DATE_TYPES:
                return c
        for c in cols:
            if ctype(c) in _NUMERIC_TYPES:
                return c
        return cols[0] if cols else None

    if agg == "count":
        for c in cols:
            if c.lower() == "id":
                return c
        return cols[0] if cols else None

    return None


def _repair_aggregate_selects(
    spec: Dict[str, Any],
    *,
    schema: Dict[str, Any],
    types: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """
    Fix common LLM aggregate mistakes:
    - normalize agg casing
    - if agg exists but column missing, pick a safe column based on types
    """
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    out = dict(spec)
    base = str(out.get("from") or "").strip()
    sel = out.get("select")
    if not isinstance(sel, list):
        return out

    new_sel = []
    for it in sel:
        if not isinstance(it, dict):
            new_sel.append(it)
            continue

        ii = dict(it)
        agg = ii.get("agg")
        if isinstance(agg, str):
            a = agg.strip().lower()
            ii["agg"] = a if a else None

        a = ii.get("agg")
        if a and a in _ALLOWED_AGGS_GATEWAY and ii.get("expr") is None:
            col = ii.get("column")
            tbl = str(ii.get("table") or base).strip() or base

            if not isinstance(col, str) or not col.strip():
                pick = _pick_best_measure_column(tbl, schema=schema, types=types, agg=a)
                if pick:
                    ii["table"] = tbl
                    ii["column"] = pick

        new_sel.append(ii)

    out["select"] = new_sel
    return out


def _auto_group_by_for_aggregates(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    If SELECT contains any aggregate, ensure group_by includes every non-aggregate select column
    (simple table+column selects, not expr).
    """
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    sel = spec.get("select")
    if not isinstance(sel, list) or not sel:
        return spec

    has_agg = any(isinstance(s, dict) and s.get("agg") for s in sel)
    if not has_agg:
        return spec

    base = str(spec.get("from") or "").strip()
    gb = spec.get("group_by")
    gb_list: List[Any] = list(gb) if isinstance(gb, list) else []
    existing = set()

    def _gb_key(table: str, col: str) -> str:
        return f"{(table or '').strip().lower()}.{(col or '').strip().lower()}"

    for g in gb_list:
        if isinstance(g, dict):
            t = str(g.get("table") or base).strip()
            c = str(g.get("column") or "").strip()
            if t and c:
                existing.add(_gb_key(t, c))
        elif isinstance(g, str) and g.strip():
            q = _parse_qualified_col(g.strip())
            if q:
                existing.add(_gb_key(q[0], q[1]))
            else:
                existing.add(_gb_key(base, g.strip()))

    for s in sel:
        if not isinstance(s, dict):
            continue
        if s.get("agg"):
            continue
        if s.get("expr") is not None:
            expr = str(s.get("expr") or "").strip()
            q = _parse_qualified_col(expr)
            if q:
                key = _gb_key(q[0], q[1])
                if key not in existing:
                    gb_list.append({"table": q[0], "column": q[1]})
                    existing.add(key)
            continue

        t = str(s.get("table") or base).strip() or base
        c = str(s.get("column") or "").strip()
        if not t or not c:
            continue
        key = _gb_key(t, c)
        if key in existing:
            continue
        gb_list.append({"table": t, "column": c})
        existing.add(key)

    out = dict(spec)
    out["group_by"] = gb_list
    return out


def _normalize_aliases_in_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make SELECT aliases + ORDER BY alias consistent (snake_case),
    so chatbot_sql ORDER BY alias validation won't fail.
    """
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    out = dict(spec)

    sel = out.get("select")
    if isinstance(sel, list):
        ns = []
        for it in sel:
            if not isinstance(it, dict):
                ns.append(it)
                continue
            ii = dict(it)
            if "alias" in ii:
                na = _norm_alias(ii.get("alias"))
                ii["alias"] = na
            ns.append(ii)
        out["select"] = ns

    ob = out.get("order_by")
    if isinstance(ob, list):
        nob = []
        for it in ob:
            if not isinstance(it, dict):
                continue
            ii = dict(it)
            if "alias" in ii:
                ii["alias"] = _norm_alias(ii.get("alias"))
            nob.append(ii)
        out["order_by"] = nob

    return out


def _normalize_queryspec_output(spec: Any) -> Any:
    if not isinstance(spec, dict):
        return spec

    out = dict(spec)

    order_by = out.get("order_by")
    if isinstance(order_by, list):
        norm_ob = []
        for o in order_by:
            if not isinstance(o, dict):
                continue
            oo = dict(o)
            if "dir" not in oo and "direction" in oo:
                oo["dir"] = oo.get("direction")
            if "dir" in oo and isinstance(oo["dir"], str):
                d = oo["dir"].strip().lower()
                if d not in ("asc", "desc"):
                    d = "desc" if "desc" in d else "asc"
                oo["dir"] = d
            norm_ob.append(oo)
        out["order_by"] = norm_ob

    joins = out.get("joins")
    if isinstance(joins, list):
        norm_joins = []
        for j in joins:
            if not isinstance(j, dict):
                continue
            jj = dict(j)

            jt = jj.get("type")
            if not isinstance(jt, str) or not jt.strip():
                jj["type"] = "left"
            else:
                t = jt.strip().lower()
                if "left" in t:
                    jj["type"] = "left"
                elif "inner" in t:
                    jj["type"] = "inner"
                elif t in ("left", "inner"):
                    jj["type"] = t
                else:
                    jj["type"] = t

            on = jj.get("on")
            if isinstance(on, dict) and ("left" in on or "right" in on):
                lq = _parse_qualified_col(str(on.get("left") or ""))
                rq = _parse_qualified_col(str(on.get("right") or ""))
                if lq and rq:
                    jj["on"] = [
                        {
                            "left_table": lq[0],
                            "left_column": lq[1],
                            "op": "=",
                            "right_table": rq[0],
                            "right_column": rq[1],
                        }
                    ]
            elif isinstance(on, list):
                norm_on = []
                for cond in on:
                    if not isinstance(cond, dict):
                        continue
                    if "left" in cond or "right" in cond:
                        lq = _parse_qualified_col(str(cond.get("left") or ""))
                        rq = _parse_qualified_col(str(cond.get("right") or ""))
                        if lq and rq:
                            norm_on.append(
                                {
                                    "left_table": lq[0],
                                    "left_column": lq[1],
                                    "op": "=",
                                    "right_table": rq[0],
                                    "right_column": rq[1],
                                }
                            )
                        else:
                            norm_on.append(cond)
                    else:
                        cc = dict(cond)
                        if "op" not in cc:
                            cc["op"] = "="
                        norm_on.append(cc)
                jj["on"] = norm_on

            norm_joins.append(jj)
        out["joins"] = norm_joins

    out = _normalize_relative_time_filters_in_spec(out)
    return out


def _has_col(schema: Dict[str, Any], table: str, col: str) -> bool:
    c = (col or "").strip().lower()
    if not c:
        return False
    return any(str(x).lower() == c for x in _schema_cols(schema, table))


def _real_col(schema: Dict[str, Any], table: str, col: str) -> Optional[str]:
    c = (col or "").strip().lower()
    for x in _schema_cols(schema, table):
        if str(x).lower() == c:
            return str(x)
    return None


def _select_has_column_or_alias(spec: Dict[str, Any], col_or_alias: str) -> bool:
    target = (col_or_alias or "").strip().lower()
    for s in (spec.get("select") or []):
        if not isinstance(s, dict):
            continue
        col = str(s.get("column") or "").strip().lower()
        alias = str(s.get("alias") or "").strip().lower()
        if col == target or alias == target:
            return True
    return False


def _wants_customer_name(question: str) -> bool:
    q = (question or "").lower()
    if "customer name" in q or "customer_name" in q:
        return True
    if "customer" in q and "name" in q:
        return True
    return False


def _pick_best_table_with_column(
    schema: Dict[str, Any], col_name: str, *, prefer_name_contains: Optional[str] = None
) -> Optional[str]:
    target = (col_name or "").strip().lower()
    if not target:
        return None

    best = None
    best_score = -1
    for t, cols in (schema or {}).items():
        if not isinstance(cols, list):
            continue
        low_cols = [str(c).lower() for c in cols]
        if target not in low_cols:
            continue
        score = 0
        tl = str(t).lower()
        if prefer_name_contains and prefer_name_contains.lower() in tl:
            score += 5
        if target in ("customer_name",) and "customer" in tl:
            score += 3
        if score > best_score:
            best_score = score
            best = t
    return best


def _postprocess_queryspec(spec: Any, *, schema: Dict[str, Any], fk_edges: List[Dict[str, Any]], question: str) -> Any:
    """
    Deterministic business fix-ups WITHOUT hardcoding joins.
    """
    if not isinstance(spec, dict):
        return spec
    if spec.get("clarify"):
        return spec

    if _wants_customer_name(question) and not _select_has_column_or_alias(spec, "customer_name"):
        base = str(spec.get("from") or "").strip()
        if base and _has_col(schema, base, "customer_name"):
            real = _real_col(schema, base, "customer_name") or "customer_name"
            spec = dict(spec)
            spec["select"] = list(spec.get("select") or []) + [
                {
                    "table": base,
                    "column": real,
                    "agg": None,
                    "alias": "customer_name",
                }
            ]
            return spec

        t_customer = _pick_best_table_with_column(schema, "customer_name", prefer_name_contains="customer")
        if t_customer:
            real = _real_col(schema, t_customer, "customer_name") or "customer_name"
            spec = dict(spec)
            spec["select"] = list(spec.get("select") or []) + [
                {
                    "table": t_customer,
                    "column": real,
                    "agg": None,
                    "alias": "customer_name",
                }
            ]
            return spec

    return spec


def _select_aliases(spec: Dict[str, Any]) -> set:
    out = set()
    for s in (spec.get("select") or []):
        if not isinstance(s, dict):
            continue
        a = s.get("alias")
        if isinstance(a, str) and a.strip():
            out.add(a.strip().lower())
    return out


def _group_keys(spec: Dict[str, Any], base: str) -> set:
    out = set()
    for g in (spec.get("group_by") or []):
        if isinstance(g, dict):
            t = str(g.get("table") or base).strip()
            c = str(g.get("column") or "").strip()
            if t and c:
                out.add(f"{t}.{c}".lower())
        elif isinstance(g, str) and g.strip():
            q = _parse_qualified_col(g.strip())
            if q:
                out.add(f"{q[0]}.{q[1]}".lower())
            else:
                out.add(f"{base}.{g.strip()}".lower())
    return out


def _sanitize_order_by(spec: Dict[str, Any], *, schema: Dict[str, Any], question: str) -> Dict[str, Any]:
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    base = str(spec.get("from") or "").strip()
    if not base:
        return spec

    order_by = spec.get("order_by") or []
    if not isinstance(order_by, list) or not order_by:
        return spec

    select_keys = set()
    for s in (spec.get("select") or []):
        if not isinstance(s, dict):
            continue
        t = str(s.get("table") or base).strip()
        c = str(s.get("column") or "").strip()
        if t and c:
            select_keys.add(f"{t}.{c}".lower())

    aliases = _select_aliases(spec)
    group_keys = _group_keys(spec, base)

    has_group = bool(group_keys)
    is_distinct = bool(spec.get("distinct"))

    filtered = []
    for o in order_by:
        if not isinstance(o, dict):
            continue

        alias = o.get("alias")
        if isinstance(alias, str) and alias.strip():
            if alias.strip().lower() in aliases:
                filtered.append(o)
            continue

        if o.get("expr") is not None or o.get("agg"):
            filtered.append(o)
            continue

        t = str(o.get("table") or base).strip()
        c = str(o.get("column") or "").strip()
        if not c:
            continue
        key = f"{t}.{c}".lower()

        if has_group and key not in group_keys:
            continue
        if is_distinct and key not in select_keys:
            continue

        filtered.append(o)

    spec2 = dict(spec)
    if filtered:
        spec2["order_by"] = filtered
        return spec2

    if has_group:
        g0 = next(iter(group_keys))
        tt, cc = g0.split(".", 1)
        spec2["order_by"] = [{"table": tt, "column": cc, "dir": "asc"}]
    elif is_distinct and spec.get("select"):
        s0 = spec["select"][0]
        spec2["order_by"] = [
            {
                "table": str(s0.get("table") or base).strip(),
                "column": str(s0.get("column") or "").strip(),
                "dir": "asc",
            }
        ]
    else:
        spec2["order_by"] = []

    return spec2


def _ensure_default_order_by_if_latest(spec: Dict[str, Any], *, schema: Dict[str, Any], question: str) -> Dict[str, Any]:
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    if spec.get("order_by"):
        return spec

    q = (question or "").lower()
    if not re.search(r"\b(latest|recent|newest|most recent)\b", q):
        return spec

    base = str(spec.get("from") or "").strip()
    if not base:
        return spec

    candidates = [
        "created_at",
        "created_on",
        "created_date",
        "updated_at",
        "updated_on",
        "po_date",
        "date",
        "timestamp",
    ]
    for c in candidates:
        if _has_col(schema, base, c):
            real = _real_col(schema, base, c) or c
            out = dict(spec)
            out["order_by"] = [{"table": base, "column": real, "dir": "desc"}]
            return out

    return spec


def _fallback_po_queryspec(schema: Dict[str, Any], question: str) -> Optional[Dict[str, Any]]:
    required = ["po_no", "po_date", "status", "quantity", "price"]
    best_score = -1
    best_table = None
    best_cols: List[str] = []

    for t, cols in (schema or {}).items():
        if not isinstance(cols, list):
            continue
        low_cols = [str(c).lower() for c in cols]
        score = sum(1 for r in required if r in low_cols)
        if "purchase" in t.lower() or "po" in t.lower():
            score += 1
        if score > best_score:
            best_score = score
            best_table = t
            best_cols = low_cols

    if not best_table or best_score <= 0:
        return None

    limit = _infer_limit(question, default=25)
    select = []
    for col in required:
        if col in best_cols:
            select.append({"table": best_table, "column": col, "agg": None, "alias": None})

    if not select:
        for col in (schema.get(best_table) or [])[:6]:
            select.append({"table": best_table, "column": col, "agg": None, "alias": None})

    spec: Dict[str, Any] = {
        "from": best_table,
        "select": select,
        "joins": [],
        "where": [],
        "group_by": [],
        "having": [],
        "order_by": [],
        "distinct": False,
        "limit": limit,
    }

    if "po_date" in best_cols:
        spec["order_by"] = [{"table": best_table, "column": "po_date", "dir": "desc"}]

    spec = _postprocess_queryspec(spec, schema=schema, fk_edges=[], question=question)

    spec = _normalize_filter_ops_and_ilike(spec, schema=schema, types={}, question=question)
    spec = _repair_aggregate_selects(spec, schema=schema, types={})
    spec = _auto_group_by_for_aggregates(spec)
    spec = _normalize_aliases_in_spec(spec)

    spec = _ensure_default_order_by_if_latest(spec, schema=schema, question=question)
    spec = _sanitize_order_by(spec, schema=schema, question=question)

    _log_queryspec("FALLBACK_PO", spec, attempt=None)
    return spec


# -------------------------
# ✅ Mismatch detection + repair + optional last-resort patching
# -------------------------
_AGG_INTENT_RE = re.compile(
    r"\b(total|sum|count|how\s+many|number\s+of|avg|average|minimum|min\b|maximum|max\b)\b", re.I
)


def _infer_agg_intent(question: str) -> Optional[str]:
    q = (question or "").lower()
    if re.search(r"\b(how\s+many|count|number\s+of)\b", q):
        return "count"
    if re.search(r"\b(total|sum)\b", q):
        return "sum"
    if re.search(r"\b(avg|average|mean)\b", q):
        return "avg"
    if re.search(r"\b(minimum|min\b|earliest)\b", q):
        return "min"
    if re.search(r"\b(maximum|max\b)\b", q):
        return "max"
    return None


def _question_is_aggregate_only(question: str) -> bool:
    q = (question or "").lower()
    if not _infer_agg_intent(q):
        return False
    # if user asks "total ..." without grouping hints
    if re.search(r"\b(by|per|group)\b", q):
        return False
    return True


def _spec_has_any_agg(spec: Dict[str, Any]) -> bool:
    for s in (spec.get("select") or []):
        if isinstance(s, dict) and s.get("agg"):
            return True
    return False


def _needs_agg_repair(question: str, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    agg = _infer_agg_intent(question)
    if not agg:
        return None
    if spec.get("clarify"):
        return None
    if _spec_has_any_agg(spec):
        return None
    # only trigger when user likely intended an aggregate output
    return {"type": "missing_aggregate", "desired_agg": agg}


def _iter_conditions(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in ("where", "having"):
        arr = spec.get(key)
        if isinstance(arr, list):
            for c in arr:
                if isinstance(c, dict):
                    out.append(c)
    return out


def _needs_name_case_insensitive_repair(
    spec: Dict[str, Any],
    *,
    types: Dict[str, Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """
    Detect the common bug you hit: '=' on name-like text fields causes case-sensitive mismatch.
    We prefer to have the LLM repair it into ILIKE (exact, no %).
    """
    if spec.get("clarify"):
        return None
    base = str(spec.get("from") or "").strip()
    for c in _iter_conditions(spec):
        op = str(c.get("op") or "").strip().lower()
        if op != "=":
            continue
        t = str(c.get("table") or base).strip() or base
        col = str(c.get("column") or "").strip()
        if not col or not t:
            continue
        if not _col_is_name_like(col):
            continue
        ctype = str((types or {}).get(t, {}).get(col) or "").strip().lower()
        if ctype and ctype not in _TEXT_TYPES:
            continue
        v = c.get("value")
        if not _value_looks_like_human_name(v):
            continue
        return {"type": "case_sensitive_name_match", "table": t, "column": col}
    return None


def _apply_case_insensitive_name_fallback(spec: Dict[str, Any], *, types: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """
    Deterministic fallback: change '=' -> 'ilike' for name-like text columns, keep exact (no wildcards).
    """
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec
    out = dict(spec)
    base = str(out.get("from") or "").strip()

    def patch(arr: Any) -> Any:
        if not isinstance(arr, list):
            return arr
        new = []
        for c in arr:
            if not isinstance(c, dict):
                new.append(c)
                continue
            cc = dict(c)
            op = str(cc.get("op") or "").strip().lower()
            if op == "=":
                t = str(cc.get("table") or base).strip() or base
                col = str(cc.get("column") or "").strip()
                ctype = str((types or {}).get(t, {}).get(col) or "").strip().lower()
                if t and col and _col_is_name_like(col) and (not ctype or ctype in _TEXT_TYPES) and _value_looks_like_human_name(cc.get("value")):
                    cc["op"] = "ilike"
                    # keep exact; normalization will not add % unless search/contains
                    if isinstance(cc.get("value"), str):
                        cc["value"] = cc["value"].strip()
            new.append(cc)
        return new

    out["where"] = patch(out.get("where"))
    out["having"] = patch(out.get("having"))
    return out


def _apply_agg_fallback(
    spec: Dict[str, Any],
    *,
    schema: Dict[str, Any],
    types: Dict[str, Dict[str, str]],
    question: str,
) -> Dict[str, Any]:
    """
    Conservative deterministic fallback for missing aggregates:
    - Only if select has exactly 1 non-agg column.
    - Convert it to desired agg, set alias, and (if aggregate-only question) drop order_by + set limit=1.
    """
    if not isinstance(spec, dict) or spec.get("clarify"):
        return spec

    desired = _infer_agg_intent(question)
    if not desired:
        return spec
    if _spec_has_any_agg(spec):
        return spec

    sel = spec.get("select")
    if not isinstance(sel, list) or len(sel) != 1:
        return spec
    s0 = sel[0]
    if not isinstance(s0, dict):
        return spec
    if s0.get("agg"):
        return spec

    base = str(spec.get("from") or "").strip()
    t = str(s0.get("table") or base).strip() or base
    col = str(s0.get("column") or "").strip()

    # If column missing, pick a safe measure
    if not col:
        pick = _pick_best_measure_column(t, schema=schema, types=types, agg=desired)
        if not pick:
            return spec
        col = pick

    out = dict(spec)
    out_sel = [dict(s0)]
    out_sel[0]["table"] = t
    out_sel[0]["column"] = col
    out_sel[0]["agg"] = desired
    out_sel[0]["alias"] = _norm_alias(f"{desired}_{col}") or f"{desired}_{col}"
    out["select"] = out_sel

    if _question_is_aggregate_only(question):
        out["limit"] = 1
        out["order_by"] = []
        out["group_by"] = []
        out["distinct"] = False

    return out


def _llm_repair_queryspec(
    base_payload: Dict[str, Any],
    *,
    previous_spec: Dict[str, Any],
    issue: Dict[str, Any],
) -> Dict[str, Any]:
    payload = dict(base_payload)
    payload["previous_spec"] = previous_spec
    payload["issue"] = issue
    return llm_call_json(QUERY_SPEC_REPAIR_SYSTEM, payload, temperature=0.0)


# -------------------------
# Public functions
# -------------------------
def llm_extract_fields(user_question: str, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    payload = {"question": user_question, "history": _trim_history(history, question=user_question)}
    out = llm_call_json(FIELD_EXTRACT_SYSTEM, payload, temperature=0.0)

    if "requested_fields" not in out or not isinstance(out.get("requested_fields"), list):
        out["requested_fields"] = []
    if "constraints" not in out or not isinstance(out.get("constraints"), dict):
        out["constraints"] = {}
    if "filters_text" not in out or not isinstance(out.get("filters_text"), list):
        out["filters_text"] = []
    if "is_followup" not in out or not isinstance(out.get("is_followup"), bool):
        out["is_followup"] = False
    if "needs_clarification" not in out or not isinstance(out.get("needs_clarification"), bool):
        out["needs_clarification"] = False
    if "clarify" not in out or not isinstance(out.get("clarify"), str):
        out["clarify"] = ""

    return out


def llm_generate_queryspec(
    user_question: str,
    history: Optional[List[Dict[str, Any]]],
    schema_slice: Dict[str, Any],
    intent_hint: Optional[Dict[str, Any]] = None,
    forced_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    schema = _extract_schema(schema_slice)
    fk_edges = _extract_fk_edges(schema_slice)
    types = _extract_types(schema_slice)
    table_meanings = _table_meanings_for_schema(schema)

    trimmed_hist = _trim_history(history, question=user_question)
    intent_hint = _sanitize_intent_hint(intent_hint, user_question)

    # PO clarification memory
    is_po_clarify, prior_q = _last_po_detail_clarify_and_prior_question(trimmed_hist)
    if is_po_clarify:
        ans = _user_answered_po_detail(user_question)
        if ans is None:
            base_for_default = prior_q or user_question
            po_assumption = _po_default_assumption(base_for_default)
        else:
            po_assumption = ans
    else:
        po_assumption = _po_default_assumption(user_question)

    # ID hints (schema-driven)
    ids = _extract_id_tokens(user_question)
    id_hints: List[Dict[str, Any]] = []
    for tok in ids:
        id_hints.append(
            _find_id_candidates_in_schema(
                tok,
                schema=schema,
                types=types,
                table_meanings=table_meanings,
                forced_tables=forced_tables,
            )
        )

    record_types = _record_type_options(table_meanings)

    question_aug = user_question
    if po_assumption == "line_items":
        question_aug += "\n\nBusiness default: PO LINE ITEMS (detailed rows)."
    elif po_assumption == "po_headers":
        question_aug += "\n\nBusiness default: one row per PO (summary)."

    m_days = _LAST_N_DAYS_RE.search(user_question or "")
    if m_days:
        try:
            n = int(m_days.group(1))
            question_aug += f'\n\nTime rule: last {n} days => where.value="RELATIVE_DAYS:{n}".'
        except Exception:
            pass

    m_hours = _LAST_N_HOURS_RE.search(user_question or "")
    if m_hours:
        try:
            n = int(m_hours.group(1))
            question_aug += f'\n\nTime rule: last {n} hours => where.value="RELATIVE_HOURS:{n}".'
        except Exception:
            pass

    base_payload = {
        "question": question_aug,
        "history": trimmed_hist,
        "schema": schema,
        "types": types,
        "fk_edges": fk_edges,
        "intent_hint": intent_hint,
        "forced_tables": forced_tables or [],
        "table_meanings": table_meanings,
        "id_hints": id_hints,
        "record_types": record_types,
    }

    last_spec: Optional[Dict[str, Any]] = None
    clarify_repeat_count = 0

    for attempt in range(QUERY_SPEC_MAX_RETRIES + 1):
        raw_spec = llm_call_json(QUERY_SPEC_SYSTEM, base_payload, temperature=0.05)
        last_spec = raw_spec if isinstance(raw_spec, dict) else None

        _log_queryspec("RAW", raw_spec, attempt=attempt)

        spec = _normalize_queryspec_output(raw_spec)
        if spec is not raw_spec:
            _log_queryspec("NORM", spec, attempt=attempt)

        spec2 = _postprocess_queryspec(spec, schema=schema, fk_edges=fk_edges, question=user_question)
        if spec2 is not spec:
            spec = spec2
            _log_queryspec("POST", spec, attempt=attempt)

        # ---- normalize filters + aggregate + group_by + aliases ----
        if isinstance(spec, dict) and not spec.get("clarify"):
            spec = _normalize_filter_ops_and_ilike(spec, schema=schema, types=types, question=user_question)
            spec = _repair_aggregate_selects(spec, schema=schema, types=types)
            spec = _auto_group_by_for_aggregates(spec)
            spec = _normalize_aliases_in_spec(spec)
            _log_queryspec("AGG_NORM", spec, attempt=attempt)

            spec = _ensure_default_order_by_if_latest(spec, schema=schema, question=user_question)
            _log_queryspec("DEFAULT_ORDER_BY", spec, attempt=attempt)

            spec = _sanitize_order_by(spec, schema=schema, question=user_question)
            _log_queryspec("ORDER_BY_SANITIZED", spec, attempt=attempt)

        # ---- LLM-driven mismatch repairs (small, targeted) ----
        if (
            LLM_REPAIR_ON_MISMATCH
            and isinstance(spec, dict)
            and not spec.get("clarify")
            and isinstance(last_spec, dict)
        ):
            repaired_any = False
            for _ in range(max(0, LLM_REPAIR_MAX_TRIES)):
                issue = _needs_agg_repair(user_question, spec)
                if not issue:
                    issue = _needs_name_case_insensitive_repair(spec, types=types)

                if not issue:
                    break

                rep_raw = _llm_repair_queryspec(base_payload, previous_spec=spec, issue=issue)
                _log_queryspec("REPAIR_RAW", rep_raw, attempt=attempt)

                rep = _normalize_queryspec_output(rep_raw)
                rep = _postprocess_queryspec(rep, schema=schema, fk_edges=fk_edges, question=user_question)

                if isinstance(rep, dict) and not rep.get("clarify"):
                    rep = _normalize_filter_ops_and_ilike(rep, schema=schema, types=types, question=user_question)
                    rep = _repair_aggregate_selects(rep, schema=schema, types=types)
                    rep = _auto_group_by_for_aggregates(rep)
                    rep = _normalize_aliases_in_spec(rep)

                    rep = _ensure_default_order_by_if_latest(rep, schema=schema, question=user_question)
                    rep = _sanitize_order_by(rep, schema=schema, question=user_question)

                spec = rep
                repaired_any = True
                _log_queryspec("REPAIR_NORM", spec, attempt=attempt)

            # last-resort deterministic safety patches (optional)
            if repaired_any and isinstance(spec, dict) and not spec.get("clarify"):
                if EXEC_FALLBACK_CASE_INSENSITIVE and _needs_name_case_insensitive_repair(spec, types=types):
                    spec = _apply_case_insensitive_name_fallback(spec, types=types)
                    spec = _normalize_filter_ops_and_ilike(spec, schema=schema, types=types, question=user_question)
                    _log_queryspec("FALLBACK_NAME_CASE", spec, attempt=attempt)

                if EXEC_FALLBACK_AGG and _needs_agg_repair(user_question, spec):
                    spec = _apply_agg_fallback(spec, schema=schema, types=types, question=user_question)
                    spec = _repair_aggregate_selects(spec, schema=schema, types=types)
                    spec = _auto_group_by_for_aggregates(spec)
                    spec = _normalize_aliases_in_spec(spec)
                    spec = _sanitize_order_by(spec, schema=schema, question=user_question)
                    _log_queryspec("FALLBACK_AGG", spec, attempt=attempt)

        # ---- Clarify handling (business-only, loop-safe) ----
        if isinstance(spec, dict) and isinstance(spec.get("clarify"), str) and spec.get("clarify").strip():
            clarify_text = spec["clarify"].strip()

            if _is_technical_text(clarify_text):
                business = _fallback_business_clarify(user_question)

                tok = _pick_best_id_to_clarify(id_hints)
                if tok:
                    h = next((x for x in id_hints if x.get("id") == tok), {})
                    opts = (h.get("record_type_options_from_candidates") or []) or record_types
                    business = _fallback_id_clarify(tok, opts)

                out = {"clarify": business or "Could you clarify what you need?"}
                _log_queryspec("CLARIFY", out, attempt=attempt)
                return out

            if _clarify_seen_before(clarify_text, trimmed_hist):
                clarify_repeat_count += 1
                if clarify_repeat_count > CLARIFY_LOOP_MAX:
                    fb = _fallback_po_queryspec(schema, user_question)
                    if fb:
                        return fb

                    tok = _pick_best_id_to_clarify(id_hints)
                    if tok:
                        h = next((x for x in id_hints if x.get("id") == tok), {})
                        opts = (h.get("record_type_options_from_candidates") or []) or record_types
                        out = {"clarify": _fallback_id_clarify(tok, opts)}
                        _log_queryspec("CLARIFY", out, attempt=attempt)
                        return out

                    out = {"clarify": _fallback_business_clarify(user_question) or "Could you clarify what you need?"}
                    _log_queryspec("CLARIFY", out, attempt=attempt)
                    return out

            if po_assumption in ("line_items", "po_headers"):
                base_payload["question"] = question_aug + "\n\nIMPORTANT: Do NOT clarify. Use the business default and return QuerySpec now."
                continue

            out = {"clarify": clarify_text}
            _log_queryspec("CLARIFY", out, attempt=attempt)
            return out

        # ---- Non-clarify: validate spec ----
        if isinstance(spec, dict):
            ok, reason = _is_valid_queryspec(spec)
            if ok:
                _log_queryspec("FINAL", spec, attempt=attempt)
                return spec

            if reason == "missing from":
                for _ in range(QUERY_SPEC_MAX_REPAIR_TRIES):
                    base_payload["question"] = question_aug + "\n\nREQUIREMENT: Return a full QuerySpec JSON with non-empty 'from' and non-empty 'select'."
                    spec2_raw = llm_call_json(QUERY_SPEC_SYSTEM, base_payload, temperature=0.0)
                    _log_queryspec("REPAIR_RAW", spec2_raw, attempt=attempt)

                    spec2 = _normalize_queryspec_output(spec2_raw)
                    spec2 = _postprocess_queryspec(spec2, schema=schema, fk_edges=fk_edges, question=user_question)

                    if isinstance(spec2, dict) and not spec2.get("clarify"):
                        spec2 = _normalize_filter_ops_and_ilike(spec2, schema=schema, types=types, question=user_question)
                        spec2 = _repair_aggregate_selects(spec2, schema=schema, types=types)
                        spec2 = _auto_group_by_for_aggregates(spec2)
                        spec2 = _normalize_aliases_in_spec(spec2)

                        spec2 = _ensure_default_order_by_if_latest(spec2, schema=schema, question=user_question)
                        spec2 = _sanitize_order_by(spec2, schema=schema, question=user_question)

                    if isinstance(spec2, dict):
                        ok2, _ = _is_valid_queryspec(spec2)
                        if ok2 and not spec2.get("clarify"):
                            _log_queryspec("FINAL", spec2, attempt=attempt)
                            return spec2

            fb = _fallback_po_queryspec(schema, user_question)
            if fb:
                return fb

        base_payload["question"] = question_aug + "\n\nReturn ONLY the QuerySpec JSON object. Do not include explanations. Do not include extra keys."

    # Out of attempts
    if last_spec and isinstance(last_spec, dict) and last_spec.get("clarify"):
        out = {"clarify": str(last_spec.get("clarify"))}
        if _is_technical_text(out["clarify"]):
            tok = _pick_best_id_to_clarify(id_hints)
            if tok:
                h = next((x for x in id_hints if x.get("id") == tok), {})
                opts = (h.get("record_type_options_from_candidates") or []) or record_types
                out["clarify"] = _fallback_id_clarify(tok, opts)
            else:
                out["clarify"] = _fallback_business_clarify(user_question) or "Could you clarify what you need?"
        _log_queryspec("CLARIFY", out, attempt=None)
        return out

    fb = _fallback_po_queryspec(schema, user_question)
    if fb:
        return fb

    tok = _pick_best_id_to_clarify(id_hints)
    if tok:
        h = next((x for x in id_hints if x.get("id") == tok), {})
        opts = (h.get("record_type_options_from_candidates") or []) or record_types
        out = {"clarify": _fallback_id_clarify(tok, opts)}
        _log_queryspec("CLARIFY", out, attempt=None)
        return out

    out = {"clarify": _fallback_business_clarify(user_question) or "Could you clarify what you need?"}
    _log_queryspec("CLARIFY", out, attempt=None)
    return out


# -------------------------
# Safe deterministic answer formatting
# -------------------------
def _pretty_col(col: str) -> str:
    c = (col or "").strip()
    if not c:
        return ""
    low = c.lower()
    if low in ("po_no", "pono", "po number", "po_num"):
        return "PO Number"
    if low in ("po_date", "podate"):
        return "PO Date"
    if low in ("customer_name", "customer"):
        return "Customer Name"
    if low in ("item_description", "item_name", "item"):
        return "Item Name"
    if low in ("qty", "quantity"):
        return "Quantity"
    if low in ("price", "rate", "amount", "total"):
        return "Price"
    if low == "status":
        return "Status"
    c = c.replace("_", " ").strip()
    return c[:1].upper() + c[1:]


def _safe_cell(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    s = s.replace("|", "\\|")
    return s


def _format_result_deterministic(question: str, result: Dict[str, Any]) -> str:
    cols = result.get("columns") or []
    rows = result.get("rows") or []

    if not rows:
        return "No matching records were found."
    if not cols:
        return "No columns returned."

    max_rows = max(1, min(50, int(ANSWER_MAX_ROWS or 25)))

    out_rows: List[Tuple[str, ...]] = []
    seen = set()

    for r in rows:
        sr = tuple(_safe_cell(r[i]) if i < len(r) else "" for i in range(len(cols)))
        if ANSWER_DEDUP_ROWS:
            if sr in seen:
                continue
            seen.add(sr)
        out_rows.append(sr)
        if len(out_rows) >= max_rows:
            break

    if len(out_rows) == 1:
        row = out_rows[0]
        lines = []
        for i, col in enumerate(cols):
            label = _pretty_col(str(col))
            val = row[i] if i < len(row) else ""
            lines.append(f"- {label}: {val}")
        return "Here’s the latest record I found:\n\n" + "\n".join(lines)

    header = "| " + " | ".join(_pretty_col(str(c)) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"

    body_lines = []
    for r in out_rows:
        body_lines.append("| " + " | ".join(r[i] if i < len(r) else "" for i in range(len(cols))) + " |")

    note = ""
    if len(rows) > len(out_rows):
        note = f"\n\nShowing {len(out_rows)} of {len(rows)} rows."
    return header + "\n" + sep + "\n" + "\n".join(body_lines) + note


def llm_format_answer(user_question: str, result: Dict[str, Any], history: Optional[List[Dict[str, Any]]] = None) -> str:
    if not ANSWER_WITH_LLM:
        return _format_result_deterministic(user_question, result)

    payload = {
        "question": user_question,
        "result": result,
        "history": _trim_history(history, question=user_question),
    }
    content = _llm_call(
        [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ],
        temperature=0.0,
        expect_json=False,
    )

    content = (content or "").strip()
    return content or _format_result_deterministic(user_question, result)
