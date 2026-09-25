"""
blueprint_engine.py

Generates a "company-level" blueprint of a warehouse: an overview
paragraph plus a role (fact / dimension / reference / lookup) and one-line
purpose for every table, grounded in the warehouse's real schema and real
relationships (including heuristically-inferred ones for generic/non-Olist
warehouses). This is what turns the schema visualization from "here are
some boxes and lines" into an explained blueprint.

Same safety posture as dashboard_engine: the schema context sent to
Ollama is built from real DuckDB metadata, the response is required to be
JSON, and it's validated (every table Ollama describes must actually
exist in the warehouse) before being trusted or saved.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone

import duckdb

from ollama_client import generate_json, OllamaError

logger = logging.getLogger("blueprint_engine")

VALID_ROLES = {"fact", "dimension", "reference", "lookup"}

# Static, not AI-generated -- these describe the actual system architecture,
# not the data, so they're the same for every warehouse and should never
# be hallucinated or vary between generations.
PIPELINE_STEPS = [
    {"name": "Ingest", "detail": "CSV/Excel upload or live MySQL connection, staged per-warehouse (MongoDB + local)"},
    {"name": "Extract", "detail": "Schema validation, dtype inference, date parsing"},
    {"name": "Clean", "detail": "Deduplication, missing-value handling, outlier flagging"},
    {"name": "Transform", "detail": "Star schema (Olist-shaped) or as-is tables with inferred relationships (any other dataset)"},
    {"name": "Load", "detail": "Indexed load into DuckDB, replacing any prior build of this warehouse"},
    {"name": "AI Layer", "detail": "Ollama-generated blueprint (roles, insights, KPIs) and dashboard (KPIs, charts, grounded explanations)"},
]

TECH_STACK = {
    "Backend": "Flask (Python)",
    "Warehouse": "DuckDB",
    "Sources": "MongoDB (GridFS) for file storage, MySQL for live database connections",
    "AI / LLM": "Ollama, local inference (qwen2.5:7b)",
    "Visualization": "Chart.js (dashboard), vis.js (schema graph)",
}


def _blueprint_path(db_path: Path) -> Path:
    db_path = Path(db_path)
    return db_path.parent / f"{db_path.stem}_blueprint.json"


def _get_user_tables(con) -> list:
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()]
    return [t for t in tables if not t.startswith("_warehouse_")]


def compute_dataset_profile(db_path: Path) -> dict:
    """Real, deterministic per-table/per-column statistics computed
    directly via DuckDB -- row counts, null percentages, distinct-value
    counts, min/max for numeric/date columns. Zero LLM involvement, so
    this is the accurate, hardcoded backbone of the blueprint: it cannot
    hallucinate, and it's what should be trusted over any AI-generated
    text for the actual facts about the data."""
    con = duckdb.connect(str(db_path), read_only=True)
    table_names = _get_user_tables(con)

    tables_profile = []
    total_rows = 0
    for t in table_names:
        row_count = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        total_rows += row_count
        cols = con.execute(f'DESCRIBE "{t}"').fetchall()

        columns_profile = []
        for col_name, col_type, *_ in cols:
            safe_col = col_name.replace('"', '""')
            try:
                null_pct, distinct_count = con.execute(
                    f'SELECT 100.0 * SUM(CASE WHEN "{safe_col}" IS NULL THEN 1 ELSE 0 END) / GREATEST(COUNT(*), 1), '
                    f'COUNT(DISTINCT "{safe_col}") FROM "{t}"'
                ).fetchone()
            except Exception:
                null_pct, distinct_count = None, None

            entry = {
                "name": col_name,
                "type": col_type,
                "null_pct": round(null_pct, 1) if null_pct is not None else None,
                "distinct_count": distinct_count,
            }

            type_upper = str(col_type).upper()
            is_numeric = any(k in type_upper for k in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "BIGINT", "NUMERIC"))
            is_date = any(k in type_upper for k in ("DATE", "TIMESTAMP"))
            if is_numeric or is_date:
                try:
                    min_v, max_v = con.execute(f'SELECT MIN("{safe_col}"), MAX("{safe_col}") FROM "{t}"').fetchone()
                    entry["min"] = str(min_v) if min_v is not None else None
                    entry["max"] = str(max_v) if max_v is not None else None
                except Exception:
                    pass

            columns_profile.append(entry)

        tables_profile.append({
            "name": t,
            "row_count": row_count,
            "column_count": len(cols),
            "columns": columns_profile,
        })

    con.close()
    warehouse_size_bytes = Path(db_path).stat().st_size if Path(db_path).exists() else 0

    return {
        "tables": tables_profile,
        "total_rows": total_rows,
        "total_tables": len(table_names),
        "warehouse_size_bytes": warehouse_size_bytes,
    }


def _build_context(db_path: Path) -> tuple:
    """Returns (context_text, table_names, relationships) -- reads real
    schema + real (or heuristically inferred) relationships directly from
    the warehouse, same as dashboard_engine's schema context builder."""
    con = duckdb.connect(str(db_path), read_only=True)
    tables = _get_user_tables(con)
    if not tables:
        con.close()
        return "", [], []

    blocks = []
    for t in tables:
        cols = con.execute(f'DESCRIBE "{t}"').fetchall()
        col_desc = ", ".join(f"{c[0]} ({c[1]})" for c in cols)
        row_count = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        blocks.append(f"TABLE {t} ({row_count:,} rows, {len(cols)} columns)\nCOLUMNS: {col_desc}")

    relationships = []
    all_tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()]
    if "_warehouse_relationships" in all_tables:
        rows = con.execute("SELECT from_table, from_column, to_table, to_column FROM _warehouse_relationships").fetchall()
        relationships = [{"from_table": r[0], "from_column": r[1], "to_table": r[2], "to_column": r[3]} for r in rows]

    con.close()

    rel_text = "\n".join(f"- {r['from_table']}.{r['from_column']} -> {r['to_table']}.{r['to_column']}" for r in relationships)
    context = "\n\n".join(blocks) + ("\n\nRELATIONSHIPS:\n" + rel_text if rel_text else "\n\nRELATIONSHIPS: none detected")
    return context, tables, relationships


def _build_prompt(context: str) -> str:
    return f"""You are documenting a data warehouse for an engineering team, in the
style of a real company's internal data catalog / architecture blueprint.

Here is the REAL schema, with real row counts and real relationships:

{context}

Write a blueprint of this warehouse. For each table, classify its role:
- "fact": records events/transactions, usually the largest table, has
  foreign keys pointing to multiple dimension tables
- "dimension": describes an entity (customer, product, seller, date, etc),
  usually smaller, referenced BY fact tables
- "reference"/"lookup": small static translation/category tables

Also propose up to 6 key business questions this warehouse can answer, up
to 6 suggested KPIs a business would track from it, and up to 5 concrete
business use cases (who would use this data and for what decision).
Ground all of these in the ACTUAL tables/columns above -- do not propose
anything that isn't actually derivable from this schema.

Respond with ONLY valid JSON in exactly this shape, no other text:
{{
  "overview": "3-5 sentence plain-English description of what this warehouse represents overall and how the tables work together",
  "tables": [
    {{"name": "must match a table name exactly", "role": "fact|dimension|reference|lookup", "purpose": "one sentence on what this table holds and why it matters"}}
  ],
  "key_insights": ["business question this data can answer, e.g. 'Which regions drive the most revenue?'"],
  "suggested_kpis": ["KPI name a business would track from this data"],
  "business_use_cases": ["who would use this and for what decision, one sentence each"]
}}
"""


def _cap_string_list(items, max_items: int) -> list:
    """Validates an AI-returned field is actually a list of non-empty
    strings, and caps its length -- defense against a malformed or
    excessively long response."""
    if not isinstance(items, list):
        return []
    cleaned = [str(i).strip() for i in items if isinstance(i, (str, int, float)) and str(i).strip()]
    return cleaned[:max_items]


def generate_blueprint(db_path: Path, model: str = None) -> dict:
    """Full pass: hardcoded profile (deterministic, no LLM) + schema ->
    Ollama for the descriptive/analytical layer -> validate -> save.
    Best-effort in the sense that a malformed/partial AI response degrades
    gracefully (unknown tables dropped, invalid roles defaulted, bad list
    items dropped) rather than failing the whole blueprint -- and the
    hardcoded profile is always present even if Ollama fails entirely for
    everything else, since it doesn't depend on the LLM call at all."""
    context, table_names, relationships = _build_context(db_path)
    if not table_names:
        raise ValueError("Warehouse is empty -- build a warehouse before generating a blueprint.")

    profile = compute_dataset_profile(db_path)  # hardcoded, real, no LLM -- computed regardless of what follows

    prompt = _build_prompt(context)
    kwargs = {"model": model} if model else {}
    raw = generate_json(prompt, **kwargs)  # raises OllamaError on failure

    overview = raw.get("overview", "")
    tables_out = []
    seen = set()
    for t in raw.get("tables", []):
        name = t.get("name")
        if name not in table_names or name in seen:
            continue  # hallucinated or duplicate table name -- drop it, don't trust blindly
        seen.add(name)
        role = t.get("role", "").lower()
        if role not in VALID_ROLES:
            role = "dimension"  # safe default rather than an invalid/empty role
        tables_out.append({"name": name, "role": role, "purpose": t.get("purpose", "")})

    # any real table Ollama didn't describe still gets a minimal entry, so
    # the blueprint always covers every table, not just the ones the model happened to mention
    for name in table_names:
        if name not in seen:
            tables_out.append({"name": name, "role": "dimension", "purpose": ""})

    blueprint = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overview": overview,
        "tables": tables_out,
        "relationships": relationships,
        "key_insights": _cap_string_list(raw.get("key_insights"), 6),
        "suggested_kpis": _cap_string_list(raw.get("suggested_kpis"), 6),
        "business_use_cases": _cap_string_list(raw.get("business_use_cases"), 5),
        "profile": profile,  # hardcoded/deterministic section -- the "strong warehouse and dataset info" backbone
        "pipeline_steps": PIPELINE_STEPS,  # static, describes the actual system, not AI-generated
        "tech_stack": TECH_STACK,          # static, describes what's actually running, not AI-generated
    }
    _blueprint_path(db_path).write_text(json.dumps(blueprint, indent=2))
    return blueprint


def load_blueprint(db_path: Path) -> dict:
    """Returns the saved blueprint, or None if one hasn't been generated
    for this warehouse yet."""
    path = _blueprint_path(db_path)
    if not path.exists():
        return None
    return json.loads(path.read_text())
