from typing import Any, Dict, List, Tuple, Set, Optional

import os
import re
import logging
from datetime import timedelta
from collections import deque

from django.db import connection, transaction
from django.utils import timezone

from .chatbot_schema import get_schema_and_fks, ForeignKey

logger = logging.getLogger(__name__)

ALLOWED_OPS = {"=", "!=", "<", ">", "<=", ">=", "ilike", "like", "in", "between", "is_null", "is_not_null"}
ALLOWED_JOIN_TYPES = {"inner", "left"}
ALLOWED_AGGS = {"count", "sum", "avg", "min", "max"}

MAX_LIMIT = 100
MAX_SELECT = 30
MAX_JOINS = 12  # hard safety cap

# DB safety timeouts (ms)
CHATBOT_STATEMENT_TIMEOUT_MS = int(os.getenv("CHATBOT_STATEMENT_TIMEOUT_MS", "4000"))
CHATBOT_LOCK_TIMEOUT_MS = int(os.getenv("CHATBOT_LOCK_TIMEOUT_MS", "1000"))

SQL_STATEMENT_TIMEOUT_MS = int(os.getenv("SQL_STATEMENT_TIMEOUT_MS", "4000"))

# -----------------------------
# Aggregation policy toggles
# -----------------------------
# If enabled, and if spec includes "_question"/"question"/"nl_query"/"user_question",
# we will enforce aggregation ONLY when the user asked for it (total/max/min/avg/count).
CHATBOT_AGG_POLICY_ENABLED = os.getenv("CHATBOT_AGG_POLICY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# If strict, strip accidental aggs when user did NOT ask for aggregation.
CHATBOT_STRICT_AGG_ONLY_WHEN_ASKED = os.getenv("CHATBOT_STRICT_AGG_ONLY_WHEN_ASKED", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}

# -----------------------------
# NL intent regex (agg vs list)
# -----------------------------
_AGG_SUM_RE = re.compile(r"\b(total|sum)\b", re.I)
_AGG_COUNT_RE = re.compile(r"\b(count|how\s+many|number\s+of)\b", re.I)
_AGG_MAX_RE = re.compile(r"\b(max|maximum|highest|largest)\b", re.I)
_AGG_MIN_RE = re.compile(r"\b(min|minimum|lowest|smallest)\b", re.I)
_AGG_AVG_RE = re.compile(r"\b(avg|average|mean)\b", re.I)

_GROUP_HINT_RE = re.compile(r"\b(per|grouped\s+by|group\s+by|by)\b", re.I)
_LIST_HINT_RE = re.compile(r"\b(list|show|latest|recent|all|records|entries|details)\b", re.I)
_WHICH_ENTITY_RE = re.compile(r"\b(which|who)\b", re.I)


def _infer_agg_intent(question: str) -> Optional[str]:
    """
    Returns one of: sum/count/avg/max/min OR None.
    - Avoids turning "Which jobcard has highest qty?" into MAX(qty) (user wants entity row).
    - Avoids aggregating when the question is clearly a list request.
    """
    q = (question or "").strip()
    if not q:
        return None

    has_any_agg_word = bool(
        _AGG_SUM_RE.search(q) or _AGG_COUNT_RE.search(q) or _AGG_MAX_RE.search(q) or _AGG_MIN_RE.search(q) or _AGG_AVG_RE.search(q)
    )

    # Clear list query and no agg keywords => do not aggregate
    if _LIST_HINT_RE.search(q) and not has_any_agg_word:
        return None

    # "Which/who ... highest/max" often expects the row (ORDER BY qty DESC LIMIT 1)
    if _WHICH_ENTITY_RE.search(q) and (_AGG_MAX_RE.search(q) or _AGG_MIN_RE.search(q)):
        return None

    if _AGG_SUM_RE.search(q):
        return "sum"
    if _AGG_COUNT_RE.search(q):
        return "count"
    if _AGG_AVG_RE.search(q):
        return "avg"
    if _AGG_MAX_RE.search(q):
        return "max"
    if _AGG_MIN_RE.search(q):
        return "min"
    return None


def _has_any_agg(spec: Dict[str, Any]) -> bool:
    for s in (spec.get("select") or []):
        if isinstance(s, dict) and s.get("agg"):
            return True
    return False


def _pick_best_measure_select(select_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Choose a likely numeric/measure column from existing selects.
    Priority: qty/quantity/amount/price/rate/total, else first column select.
    """
    if not select_items:
        return None

    priority = {"qty", "quantity", "amount", "price", "rate", "total"}
    for it in select_items:
        if not isinstance(it, dict):
            continue
        if it.get("expr") is not None:
            continue
        c = (it.get("column") or "").strip().lower()
        if c in priority:
            return it

    for it in select_items:
        if not isinstance(it, dict):
            continue
        if it.get("expr") is None and isinstance(it.get("column"), str) and it["column"].strip():
            return it

    return None


def _apply_agg_policy(spec: Dict[str, Any], *, question: Optional[str]) -> Dict[str, Any]:
    """
    Enforce:
    - If user asked for agg (sum/count/max/min/avg) but spec has none -> rewrite SELECT to include agg.
    - If user did NOT ask for agg and strict mode enabled -> strip aggs in SELECT.
    Notes:
    - This policy requires the natural language question. Pass as spec["_question"] in caller.
    """
    if not CHATBOT_AGG_POLICY_ENABLED:
        return spec

    q = (question or "").strip()
    if not q:
        return spec

    intent = _infer_agg_intent(q)

    # Strict: remove accidental aggs when user didn't ask
    if not intent and CHATBOT_STRICT_AGG_ONLY_WHEN_ASKED and _has_any_agg(spec):
        out = dict(spec)
        new_sel = []
        for s in (out.get("select") or []):
            if isinstance(s, dict) and s.get("agg"):
                ss = dict(s)
                ss["agg"] = None
                new_sel.append(ss)
            else:
                new_sel.append(s)
        out["select"] = new_sel
        return out

    # If no agg intent, do nothing
    if not intent:
        return spec

    # If already has agg, do nothing
    if _has_any_agg(spec):
        return spec

    out = dict(spec)
    select_items = [x for x in (out.get("select") or []) if isinstance(x, dict)]
    best = _pick_best_measure_select(select_items)

    if intent == "count":
        # Prefer COUNT(*)
        out["select"] = [{"table": out.get("from"), "column": "*", "agg": "count", "alias": "count"}]
    else:
        if not best:
            return spec  # can't safely infer target
        base_table = (best.get("table") or out.get("from") or "").strip()
        col = (best.get("column") or "").strip()
        alias = f"{intent}_{col}" if intent in {"max", "min", "avg"} else f"total_{col}"
        out["select"] = [{"table": base_table, "column": col, "agg": intent, "alias": alias}]

    # If user is not asking for grouped stats, aggregation should be single-row:
    if not (out.get("group_by") or _GROUP_HINT_RE.search(q)):
        out["order_by"] = []
        out["limit"] = 1

    return out


# -----------------------------
# Identifier safety
# -----------------------------
def _ensure_table(schema_tables: Dict[str, List[str]], table: str) -> None:
    if table not in schema_tables:
        raise ValueError(f"Unknown table: {table}")


def _ensure_column(schema_tables: Dict[str, List[str]], table: str, column: str) -> None:
    _ensure_table(schema_tables, table)
    if column not in schema_tables[table]:
        raise ValueError(f"Unknown column: {table}.{column}")


def _q_ident(name: str) -> str:
    if not name or not isinstance(name, str):
        raise ValueError("Invalid identifier")
    if any(ch in name for ch in ['"', ";", "--", "/*", "*/"]):
        raise ValueError("Invalid identifier")
    return '"' + name.replace('"', '""') + '"'


def _q_col(table: str, column: str) -> str:
    return f"{_q_ident(table)}.{_q_ident(column)}"


# -----------------------------
# Safe arithmetic expressions
# -----------------------------
ALLOWED_EXPR_OPS = {"+", "-", "*", "/"}


def _compile_expr(expr: Any, schema_tables: Dict[str, List[str]]) -> str:
    """
    Compile a SAFE arithmetic expression node into SQL.
    Supported shapes:
      - {"table": "t", "column": "c"}
      - {"op": "*", "left": <expr>, "right": <expr>}
      - {"op": "+", "left": <expr>, "right": <expr>} etc
    """
    if isinstance(expr, dict):
        # column ref
        if "table" in expr and "column" in expr:
            t = (expr.get("table") or "").strip()
            c = (expr.get("column") or "").strip()
            _ensure_column(schema_tables, t, c)
            return _q_col(t, c)

        # binary arithmetic
        op = (expr.get("op") or "").strip()
        if op in ALLOWED_EXPR_OPS and "left" in expr and "right" in expr:
            left_sql = _compile_expr(expr["left"], schema_tables)
            right_sql = _compile_expr(expr["right"], schema_tables)
            return f"({left_sql} {op} {right_sql})"

    raise ValueError("Invalid expr node in QuerySpec (only column refs and + - * / are allowed)")


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
# Relative time normalization
# -----------------------------
_REL_DAYS_RE = re.compile(r"^RELATIVE_DAYS:(\d{1,5})$", re.I)
_REL_HOURS_RE = re.compile(r"^RELATIVE_HOURS:(\d{1,6})$", re.I)
_NOW_INTERVAL_DAYS_RE = re.compile(r"^now\d*\(\)?\s*-\s*interval\s*'(\d{1,5})\s*days'\s*$", re.I)
_NOW_INTERVAL_HOURS_RE = re.compile(r"^now\d*\(\)?\s*-\s*interval\s*'(\d{1,6})\s*hours'\s*$", re.I)


def _normalize_value(val: Any) -> Any:
    """
    Converts special time tokens to real datetimes so Postgres doesn't have to parse
    weird strings like "now() - interval '30 days'".
    """
    if not isinstance(val, str):
        return val

    s = val.strip()

    m = _REL_DAYS_RE.match(s)
    if m:
        days = int(m.group(1))
        return timezone.now() - timedelta(days=days)

    m = _REL_HOURS_RE.match(s)
    if m:
        hours = int(m.group(1))
        return timezone.now() - timedelta(hours=hours)

    m = _NOW_INTERVAL_DAYS_RE.match(s)
    if m:
        days = int(m.group(1))
        return timezone.now() - timedelta(days=days)

    m = _NOW_INTERVAL_HOURS_RE.match(s)
    if m:
        hours = int(m.group(1))
        return timezone.now() - timedelta(hours=hours)

    return val


# -----------------------------
# Join minimization + validation
# -----------------------------
def _collect_tables_from_items(items: Any, default_table: str) -> Set[str]:
    out: Set[str] = set()
    if not items or not isinstance(items, list):
        return out

    for it in items:
        if not isinstance(it, dict):
            continue

        # table from direct ref (only if table/column present)
        if "column" in it or "table" in it:
            t = (it.get("table") or default_table or "").strip()
            if t:
                out.add(t)

        # table(s) from expr ref
        if "expr" in it:
            out |= _tables_in_expr(it.get("expr"))

    return out


def _tables_referenced_in_spec(spec: Dict[str, Any]) -> Set[str]:
    base = (spec.get("from") or "").strip()
    used: Set[str] = {base} if base else set()

    used |= _collect_tables_from_items(spec.get("select") or [], base)
    used |= _collect_tables_from_items(spec.get("where") or [], base)
    used |= _collect_tables_from_items(spec.get("group_by") or [], base)
    used |= _collect_tables_from_items(spec.get("order_by") or [], base)
    used |= _collect_tables_from_items(spec.get("having") or [], base)

    return {t for t in used if t}


def _tables_in_join_on(on: Any, default_left: str, default_right: str) -> Set[str]:
    out: Set[str] = set()
    if not on or not isinstance(on, list):
        return out
    for cond in on:
        if not isinstance(cond, dict):
            continue
        lt = (cond.get("left_table") or default_left or "").strip()
        rt = (cond.get("right_table") or default_right or "").strip()
        if lt:
            out.add(lt)
        if rt:
            out.add(rt)
    return out


def _split_qual(s: str) -> Tuple[Optional[str], str]:
    s = (s or "").strip()
    if "." in s:
        t, c = s.split(".", 1)
        return (t.strip() or None), (c.strip() or "")
    return None, s


# -----------------------------
# LLM output tolerance (select/order_by strings)
# -----------------------------
_SELECT_AS_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.I,
)

_ORDER_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*(asc|desc)?\s*$",
    re.I,
)


def _coerce_select_items(select_items: Any, from_table: str) -> List[Any]:
    if not isinstance(select_items, list):
        return []

    out: List[Any] = []
    for it in select_items:
        if isinstance(it, dict):
            out.append(it)
            continue
        if not isinstance(it, str):
            continue

        s = it.strip()
        if not s:
            continue

        m = _SELECT_AS_RE.match(s)
        if m:
            qual = m.group(1)
            alias = m.group(2)
            tt, cc = _split_qual(qual)
            if tt:
                out.append({"table": tt, "column": cc, "alias": alias})
            else:
                out.append({"table": from_table, "column": cc, "alias": alias})
            continue

        tt, cc = _split_qual(s)
        if tt:
            out.append({"table": tt, "column": cc})
        else:
            out.append({"table": from_table, "column": cc})

    return out


def _coerce_order_by_items(order_by: Any, from_table: str, select_aliases: Set[str]) -> List[Any]:
    if not isinstance(order_by, list):
        return []

    out: List[Any] = []
    for o in order_by:
        if isinstance(o, dict):
            out.append(o)
            continue
        if not isinstance(o, str):
            continue

        m = _ORDER_RE.match(o.strip())
        if not m:
            continue

        qual = m.group(1)
        d = (m.group(2) or "asc").lower()

        if qual in select_aliases:
            out.append({"alias": qual, "dir": d})
            continue

        tt, cc = _split_qual(qual)
        if tt:
            out.append({"table": tt, "column": cc, "dir": d})
        else:
            out.append({"table": from_table, "column": cc, "dir": d})

    return out


def _normalize_join_on(on: Any, from_table: str, join_table: str) -> List[Dict[str, Any]]:
    if isinstance(on, list):
        out = []
        for cond in on:
            if not isinstance(cond, dict):
                continue
            out.append(
                {
                    "left_table": (cond.get("left_table") or from_table).strip(),
                    "left_column": (cond.get("left_column") or "").strip(),
                    "op": (cond.get("op") or "=").strip(),
                    "right_table": (cond.get("right_table") or join_table).strip(),
                    "right_column": (cond.get("right_column") or "").strip(),
                }
            )
        return [c for c in out if c["left_column"] and c["right_column"]]

    if isinstance(on, dict):
        left = on.get("left") or ""
        right = on.get("right") or ""
        lt, lc = _split_qual(str(left))
        rt, rc = _split_qual(str(right))

        return [
            {
                "left_table": (lt or from_table).strip(),
                "left_column": (lc or "").strip(),
                "op": "=",
                "right_table": (rt or join_table).strip(),
                "right_column": (rc or "").strip(),
            }
        ]

    return []


def _prune_unnecessary_joins(spec: Dict[str, Any]) -> Dict[str, Any]:
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
            on_norm = _normalize_join_on(j.get("on"), base, jt)
            required |= _tables_in_join_on(on_norm, default_left=base, default_right=jt)

    out = dict(spec)
    out["joins"] = list(reversed(kept_rev))
    return out


def _validate_referenced_tables_joined(spec: Dict[str, Any], schema_tables: Dict[str, List[str]]) -> None:
    base = (spec.get("from") or "").strip()
    _ensure_table(schema_tables, base)

    join_tables: Set[str] = set()
    for j in (spec.get("joins") or []):
        if isinstance(j, dict) and j.get("table"):
            join_tables.add(str(j["table"]).strip())

    referenced = _tables_referenced_in_spec(spec)
    allowed = {base} | join_tables

    for t in referenced:
        _ensure_table(schema_tables, t)
        if t not in allowed:
            raise ValueError(
                f"Query references table '{t}' but it is not in FROM/JOIN and could not be auto-joined. "
                f"FROM='{base}', JOINs={sorted(join_tables)}"
            )


# -----------------------------
# FK-based auto join inference (NO hardcoding)
# -----------------------------
def _build_fk_adj(fks: List[ForeignKey]) -> Dict[str, List[Tuple[str, ForeignKey]]]:
    adj: Dict[str, List[Tuple[str, ForeignKey]]] = {}
    for fk in fks or []:
        adj.setdefault(fk.table, []).append((fk.ref_table, fk))
        adj.setdefault(fk.ref_table, []).append((fk.table, fk))
    return adj


def _find_path_to_any_target(
    adj: Dict[str, List[Tuple[str, ForeignKey]]],
    sources: Set[str],
    targets: Set[str],
) -> Tuple[Optional[str], Dict[str, Tuple[Optional[str], Optional[ForeignKey]]]]:
    q = deque()
    prev: Dict[str, Tuple[Optional[str], Optional[ForeignKey]]] = {}

    for s in sources:
        prev[s] = (None, None)
        q.append(s)

    while q:
        cur = q.popleft()
        if cur in targets:
            return cur, prev

        for nb, fk in adj.get(cur, []):
            if nb in prev:
                continue
            prev[nb] = (cur, fk)
            q.append(nb)

    return None, prev


def _reconstruct_edges(
    hit: str,
    prev: Dict[str, Tuple[Optional[str], Optional[ForeignKey]]],
) -> List[Tuple[str, str, ForeignKey]]:
    edges_rev: List[Tuple[str, str, ForeignKey]] = []
    cur = hit
    while True:
        p, fk = prev.get(cur, (None, None))
        if p is None or fk is None:
            break
        edges_rev.append((p, cur, fk))
        cur = p
    edges_rev.reverse()
    return edges_rev


def _auto_add_fk_joins(spec: Dict[str, Any], schema_tables: Dict[str, List[str]], fks: List[ForeignKey]) -> Dict[str, Any]:
    base = (spec.get("from") or "").strip()
    if not base:
        return spec

    joins = spec.get("joins") or []
    if not isinstance(joins, list):
        joins = []

    available: Set[str] = {base}
    for j in joins:
        if isinstance(j, dict) and j.get("table"):
            available.add(str(j["table"]).strip())

    referenced = _tables_referenced_in_spec(spec)
    missing = {t for t in referenced if t not in available}

    if not missing:
        return spec

    adj = _build_fk_adj(fks)

    out = dict(spec)
    out_joins = list(joins)

    while missing:
        if len(out_joins) >= MAX_JOINS:
            raise ValueError(f"Too many joins needed (max {MAX_JOINS}). Missing tables: {sorted(missing)}")

        hit, prev = _find_path_to_any_target(adj, available, missing)
        if not hit:
            raise ValueError(
                f"Could not infer FK join path from {sorted(available)} to {sorted(missing)}. "
                f"Check that FK constraints exist between these tables."
            )

        edges = _reconstruct_edges(hit, prev)
        for parent, child, fk in edges:
            if child in available:
                continue

            _ensure_table(schema_tables, fk.table)
            _ensure_table(schema_tables, fk.ref_table)
            _ensure_column(schema_tables, fk.table, fk.column)
            _ensure_column(schema_tables, fk.ref_table, fk.ref_column)

            join_obj = {
                "type": "left",
                "table": child,
                "on": [
                    {
                        "op": "=",
                        "left_table": fk.table,
                        "left_column": fk.column,
                        "right_table": fk.ref_table,
                        "right_column": fk.ref_column,
                    }
                ],
            }
            out_joins.append(join_obj)
            available.add(child)

            if len(out_joins) >= MAX_JOINS and missing - available:
                raise ValueError(f"Too many joins needed (max {MAX_JOINS}). Still missing: {sorted(missing - available)}")

        missing = {t for t in referenced if t not in available}

    out["joins"] = out_joins
    return out


# -----------------------------
# GROUP BY / ORDER BY guardrails
# -----------------------------
def _is_agg_select_item(it: Any) -> bool:
    if not isinstance(it, dict):
        return False
    agg = (it.get("agg") or "").strip().lower()
    return bool(agg)


def _select_alias_set(select_items: List[dict]) -> Set[str]:
    out: Set[str] = set()
    for it in select_items or []:
        if not isinstance(it, dict):
            continue
        a = it.get("alias")
        if isinstance(a, str) and a.strip():
            out.add(a.strip())
    return out


def _group_key_set(group_by: List[Any], from_table: str) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    for g in group_by or []:
        if isinstance(g, dict):
            t = (g.get("table") or from_table).strip()
            c = (g.get("column") or "").strip()
            if t and c:
                keys.add((t, c))
        elif isinstance(g, str) and g.strip():
            tt, cc = _split_qual(g.strip())
            t = (tt or from_table).strip()
            c = (cc or "").strip()
            if t and c:
                keys.add((t, c))
    return keys


def _ensure_group_by_for_aggregates(spec: Dict[str, Any], schema_tables: Dict[str, List[str]]) -> Dict[str, Any]:
    out = dict(spec)
    from_table = out["from"]
    select_items = out.get("select") or []
    group_by = list(out.get("group_by") or [])

    has_agg = any(_is_agg_select_item(it) for it in select_items)
    if not has_agg:
        return out

    for it in select_items:
        if not isinstance(it, dict):
            continue
        if it.get("expr") is not None and not _is_agg_select_item(it):
            raise ValueError(
                "Invalid grouped query: select.expr without agg is not supported unless you explicitly group by an equivalent column."
            )

    keys = _group_key_set(group_by, from_table)

    for it in select_items:
        if not isinstance(it, dict):
            continue

        if it.get("expr") is None and not _is_agg_select_item(it):
            t = (it.get("table") or from_table).strip()
            c = (it.get("column") or "").strip()
            if t and c:
                _ensure_column(schema_tables, t, c)
                if (t, c) not in keys:
                    group_by.append({"table": t, "column": c})
                    keys.add((t, c))

    out["group_by"] = group_by
    return out


def _sanitize_order_by_for_grouping(spec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(spec)
    from_table = out["from"]
    select_items = out.get("select") or []
    group_by = out.get("group_by") or []
    order_by = list(out.get("order_by") or [])

    has_agg = any(_is_agg_select_item(it) for it in select_items)
    has_group = bool(group_by)

    if not (has_agg or has_group):
        return out

    aliases = _select_alias_set(select_items)
    gkeys = _group_key_set(group_by, from_table)

    cleaned: List[dict] = []
    for o in order_by:
        if not isinstance(o, dict):
            continue

        alias = o.get("alias")
        if isinstance(alias, str) and alias.strip():
            if alias.strip() in aliases:
                cleaned.append(o)
            continue

        if o.get("expr") is not None:
            agg = (o.get("agg") or "").strip().lower()
            if agg in ALLOWED_AGGS:
                cleaned.append(o)
            continue

        t = (o.get("table") or from_table).strip()
        c = (o.get("column") or "").strip()
        if not t or not c:
            continue

        agg = (o.get("agg") or "").strip().lower()
        if agg in ALLOWED_AGGS:
            cleaned.append(o)
            continue

        if (t, c) in gkeys:
            cleaned.append(o)

    if not cleaned:
        for it in select_items:
            if not isinstance(it, dict):
                continue
            if _is_agg_select_item(it) and isinstance(it.get("alias"), str) and it["alias"].strip():
                cleaned = [{"alias": it["alias"].strip(), "dir": "desc"}]
                break

        if not cleaned:
            for it in select_items:
                if not isinstance(it, dict):
                    continue
                if _is_agg_select_item(it):
                    cleaned = [
                        {
                            "agg": (it.get("agg") or "").strip().lower(),
                            "dir": "desc",
                            **(
                                {"expr": it["expr"]}
                                if it.get("expr") is not None
                                else {
                                    "table": (it.get("table") or from_table).strip(),
                                    "column": (it.get("column") or "").strip(),
                                }
                            ),
                        }
                    ]
                    break

        if not cleaned and gkeys:
            t, c = next(iter(gkeys))
            cleaned = [{"table": t, "column": c, "dir": "asc"}]

    out["order_by"] = cleaned
    return out


# -----------------------------
# Spec normalization
# -----------------------------
def _normalize_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("QuerySpec must be an object.")

    if "clarify" in spec and spec["clarify"]:
        raise ValueError(f"clarify: {spec['clarify']}")

    if not spec.get("from") or not isinstance(spec.get("from"), str):
        raise ValueError("QuerySpec must include 'from' table.")

    spec.setdefault("select", [])
    spec.setdefault("joins", [])
    spec.setdefault("where", [])
    spec.setdefault("group_by", [])
    spec.setdefault("order_by", [])
    spec.setdefault("having", [])
    spec["distinct"] = bool(spec.get("distinct", False))

    if (
        not isinstance(spec["select"], list)
        or not isinstance(spec["joins"], list)
        or not isinstance(spec["where"], list)
        or not isinstance(spec["group_by"], list)
        or not isinstance(spec["order_by"], list)
        or not isinstance(spec["having"], list)
    ):
        raise ValueError("select/joins/where/group_by/order_by/having must be lists.")

    from_table = spec["from"]
    spec["select"] = _coerce_select_items(spec.get("select") or [], from_table)
    select_aliases = _select_alias_set([x for x in spec["select"] if isinstance(x, dict)])

    if spec.get("order_by") and any(isinstance(x, str) for x in (spec.get("order_by") or [])):
        spec["order_by"] = _coerce_order_by_items(spec.get("order_by") or [], from_table, select_aliases)

    if len(spec["joins"]) > MAX_JOINS:
        raise ValueError(f"Too many joins (max {MAX_JOINS}).")

    limit = int(spec.get("limit") or 50)
    spec["limit"] = max(1, min(limit, MAX_LIMIT))
    return spec


# -----------------------------
# Build SQL
# -----------------------------
def build_sql_from_queryspec(spec: dict) -> Tuple[str, List[Any]]:
    # Grab NL question if caller provided it in the spec
    question = None
    if isinstance(spec, dict):
        question = spec.get("_question") or spec.get("question") or spec.get("nl_query") or spec.get("user_question")

    spec = _normalize_spec(spec)

    schema_tables, fks = get_schema_and_fks()
    from_table = spec["from"]
    _ensure_table(schema_tables, from_table)

    # 1) prune any junk joins
    spec = _prune_unnecessary_joins(spec)

    # 2) auto-add missing joins using FK graph (no LLM guessing)
    spec = _auto_add_fk_joins(spec, schema_tables, fks)

    # 3) prune again (keeps it minimal even after inference)
    spec = _prune_unnecessary_joins(spec)

    # 3.5) Apply agg policy based on NL question (ONLY if user asked)
    spec = _apply_agg_policy(spec, question=question)

    # 4) GROUP BY + ORDER BY safety for aggregates
    spec = _ensure_group_by_for_aggregates(spec, schema_tables)
    spec = _sanitize_order_by_for_grouping(spec)

    params: List[Any] = []

    select_items = spec.get("select") or []
    if not select_items:
        raise ValueError("QuerySpec.select cannot be empty.")
    if len(select_items) > MAX_SELECT:
        raise ValueError(f"Too many select items (max {MAX_SELECT}).")

    # ensure all referenced tables are present (after auto-join)
    _validate_referenced_tables_joined(spec, schema_tables)

    # --- SELECT ---
    select_sql_parts: List[str] = []
    for it in select_items:
        if not isinstance(it, dict):
            raise ValueError("select items must be objects")

        agg = (it.get("agg") or "").lower().strip() if it.get("agg") else None
        alias = it.get("alias")

        # Support either column ref OR expr ref
        if "expr" in it and it["expr"] is not None:
            expr_sql = _compile_expr(it["expr"], schema_tables)
        else:
            t = (it.get("table") or from_table).strip()
            c = it.get("column")
            if not c or not isinstance(c, str):
                raise ValueError("select.column required (or provide select.expr)")

            # ✅ COUNT(*) support
            if c.strip() == "*" and agg == "count":
                expr_sql = "*"
            else:
                _ensure_column(schema_tables, t, c)
                expr_sql = _q_col(t, c)

        if agg:
            if agg not in ALLOWED_AGGS:
                raise ValueError(f"Invalid agg: {agg}")
            expr_sql = f"{agg.upper()}({expr_sql})"

        if alias and isinstance(alias, str):
            expr_sql = f"{expr_sql} AS {_q_ident(alias)}"

        select_sql_parts.append(expr_sql)

    distinct = "DISTINCT " if spec.get("distinct") else ""
    sql = f"SELECT {distinct}{', '.join(select_sql_parts)} FROM {_q_ident(from_table)}"

    # --- JOINS ---
    available_tables: Set[str] = {from_table}

    for j in spec.get("joins") or []:
        if not isinstance(j, dict):
            raise ValueError("join must be object")

        jtype = (j.get("type") or "left").lower().strip()
        jtable = (j.get("table") or "").strip()

        if jtype not in ALLOWED_JOIN_TYPES:
            raise ValueError(f"Invalid join type: {jtype}")
        if not jtable or not isinstance(jtable, str):
            raise ValueError("join.table required")
        _ensure_table(schema_tables, jtable)

        on_norm = _normalize_join_on(j.get("on"), from_table, jtable)
        if not on_norm:
            raise ValueError("join.on must be non-empty (list or {left,right} dict)")

        on_parts = []
        join_mentions_table = False

        for cond in on_norm:
            lt = (cond.get("left_table") or from_table).strip()
            lc = (cond.get("left_column") or "").strip()
            op = (cond.get("op") or "=").strip()
            rt = (cond.get("right_table") or jtable).strip()
            rc = (cond.get("right_column") or "").strip()

            if op != "=":
                raise ValueError("Only '=' allowed for join conditions")
            if not lc or not rc:
                raise ValueError("join.on requires left_column/right_column")

            _ensure_column(schema_tables, lt, lc)
            _ensure_column(schema_tables, rt, rc)

            if lt == jtable or rt == jtable:
                join_mentions_table = True

            if (lt not in available_tables and lt != jtable) and (rt not in available_tables and rt != jtable):
                raise ValueError(
                    f"Join '{jtable}' does not connect to any available table. "
                    f"Available={sorted(available_tables)}. ON references {lt} and {rt}."
                )

            on_parts.append(f"{_q_ident(lt)}.{_q_ident(lc)} = {_q_ident(rt)}.{_q_ident(rc)}")

        if not join_mentions_table:
            raise ValueError(f"Join ON for '{jtable}' must reference '{jtable}' in left_table/right_table.")

        sql += f" {jtype.upper()} JOIN {_q_ident(jtable)} ON " + " AND ".join(on_parts)
        available_tables.add(jtable)

    # --- WHERE ---
    where_parts: List[str] = []
    for w in spec.get("where") or []:
        if not isinstance(w, dict):
            raise ValueError("where item must be object")
        t = (w.get("table") or from_table).strip()
        c = w.get("column")
        op = (w.get("op") or "=").lower().strip()
        val = w.get("value", None)

        if not c or not isinstance(c, str):
            raise ValueError("where.column required")
        if op not in ALLOWED_OPS:
            raise ValueError(f"Invalid op: {op}")
        _ensure_column(schema_tables, t, c)

        left = _q_col(t, c)

        if op in {"is_null", "is_not_null"}:
            where_parts.append(f"{left} IS {'NOT ' if op == 'is_not_null' else ''}NULL")
            continue

        if op == "between":
            if not isinstance(val, list) or len(val) != 2:
                raise ValueError("between requires value=[low, high]")
            low = _normalize_value(val[0])
            high = _normalize_value(val[1])
            where_parts.append(f"{left} BETWEEN %s AND %s")
            params.extend([low, high])
            continue

        if op == "in":
            if not isinstance(val, list) or not val:
                raise ValueError("in requires non-empty list value")
            placeholders = ", ".join(["%s"] * len(val))
            where_parts.append(f"{left} IN ({placeholders})")
            params.extend([_normalize_value(x) for x in val])
            continue

        if val is None:
            raise ValueError(f"op '{op}' requires a value")

        val = _normalize_value(val)
        sql_op = "ILIKE" if op == "ilike" else ("LIKE" if op == "like" else op)
        where_parts.append(f"{left} {sql_op} %s")
        params.append(val)

    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    # --- GROUP BY ---
    group_parts: List[str] = []
    for g in spec.get("group_by") or []:
        if isinstance(g, dict):
            t = (g.get("table") or from_table).strip()
            c = (g.get("column") or "").strip()
            if t and c:
                _ensure_column(schema_tables, t, c)
                group_parts.append(_q_col(t, c))
        elif isinstance(g, str) and g.strip():
            tt, cc = _split_qual(g.strip())
            t = (tt or from_table).strip()
            c = (cc or "").strip()
            if t and c:
                _ensure_column(schema_tables, t, c)
                group_parts.append(_q_col(t, c))
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)

    # --- HAVING (✅ now supports agg + expr) ---
    having_parts: List[str] = []
    for h in spec.get("having") or []:
        if not isinstance(h, dict):
            continue

        op = (h.get("op") or "=").lower().strip()
        val = h.get("value", None)
        agg = (h.get("agg") or "").lower().strip() if h.get("agg") else None

        if op not in ALLOWED_OPS:
            continue
        if agg and agg not in ALLOWED_AGGS:
            raise ValueError(f"Invalid agg in having: {agg}")

        # left side: expr OR column
        if h.get("expr") is not None:
            left_expr = _compile_expr(h["expr"], schema_tables)
        else:
            t = (h.get("table") or from_table).strip()
            c = h.get("column")
            if not c or not isinstance(c, str):
                continue
            _ensure_column(schema_tables, t, c)
            left_expr = _q_col(t, c)

        if agg:
            left_expr = f"{agg.upper()}({left_expr})"

        if op in {"is_null", "is_not_null"}:
            having_parts.append(f"{left_expr} IS {'NOT ' if op == 'is_not_null' else ''}NULL")
            continue

        if op == "between":
            if not isinstance(val, list) or len(val) != 2:
                raise ValueError("having between requires value=[low, high]")
            low = _normalize_value(val[0])
            high = _normalize_value(val[1])
            having_parts.append(f"{left_expr} BETWEEN %s AND %s")
            params.extend([low, high])
            continue

        if op == "in":
            if not isinstance(val, list) or not val:
                raise ValueError("having in requires non-empty list value")
            placeholders = ", ".join(["%s"] * len(val))
            having_parts.append(f"{left_expr} IN ({placeholders})")
            params.extend([_normalize_value(x) for x in val])
            continue

        if val is None:
            raise ValueError(f"having op '{op}' requires a value")

        val = _normalize_value(val)
        sql_op = "ILIKE" if op == "ilike" else ("LIKE" if op == "like" else op)
        having_parts.append(f"{left_expr} {sql_op} %s")
        params.append(val)

    if having_parts:
        sql += " HAVING " + " AND ".join(having_parts)

    # --- ORDER BY (supports alias/expr/agg) ---
    select_aliases = _select_alias_set(select_items)

    order_parts: List[str] = []
    for o in spec.get("order_by") or []:
        if not isinstance(o, dict):
            continue

        d = (o.get("dir") or o.get("direction") or "asc").lower().strip()
        if d not in {"asc", "desc"}:
            d = "asc"

        alias = o.get("alias")
        if isinstance(alias, str) and alias.strip():
            a = alias.strip()
            if a not in select_aliases:
                raise ValueError(f"ORDER BY alias '{a}' is not present in SELECT aliases")
            order_parts.append(f"{_q_ident(a)} {d.upper()}")
            continue

        agg = (o.get("agg") or "").lower().strip() if o.get("agg") else None
        if agg and agg not in ALLOWED_AGGS:
            raise ValueError(f"Invalid agg in order_by: {agg}")

        if o.get("expr") is not None:
            expr_sql = _compile_expr(o["expr"], schema_tables)
        else:
            t = (o.get("table") or from_table).strip()
            c = (o.get("column") or "").strip()
            if not t or not c:
                continue
            _ensure_column(schema_tables, t, c)
            expr_sql = _q_col(t, c)

        if agg:
            expr_sql = f"{agg.upper()}({expr_sql})"

        order_parts.append(f"{expr_sql} {d.upper()}")

    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)

    sql += " LIMIT %s"
    params.append(int(spec["limit"]))

    return sql, params


# -----------------------------
# Execute SQL (readonly)
# -----------------------------
def execute_readonly_sql(sql: str, params: List[Any]) -> dict:
    """
    Executes a SELECT query safely with:
      - read-only transaction (Postgres)
      - statement timeout
    Uses transaction.atomic() so SET LOCAL works correctly.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL transaction_read_only = on;")
            cursor.execute("SET LOCAL statement_timeout = %s;", [SQL_STATEMENT_TIMEOUT_MS])

            cursor.execute(sql, params)
            cols = [c[0] for c in cursor.description] if cursor.description else []
            rows = cursor.fetchall()

    return {"columns": cols, "rows": rows}
