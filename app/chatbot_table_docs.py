from __future__ import annotations
from pathlib import Path
from typing import Dict, Optional


_CACHE: Optional[Dict[str, str]] = None


def load_table_descriptions(path: Optional[str] = None) -> Dict[str, str]:
    """
    Loads table descriptions from a text file:
      app_table: description
    Cached in-memory.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    p = Path(path) if path else (Path(__file__).resolve().parent / "table_descriptions.txt")
    if not p.exists():
        _CACHE = {}
        return _CACHE

    out: Dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v

    _CACHE = out
    return _CACHE


def describe_table(db_table: str) -> str:
    return load_table_descriptions().get(db_table, "").strip()


def docs_for_tables(tables: list[str]) -> Dict[str, str]:
    d = load_table_descriptions()
    return {t: d[t] for t in tables if t in d}
