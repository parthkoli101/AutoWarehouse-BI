"""
generic_pipeline.py

Handles ingestion for arbitrary/unknown-shape sources -- specifically a
live MySQL database whose tables don't match the Olist-specific star
schema in config.py/transform.py.

Honesty note: this does NOT build a star schema, because we don't know the
business domain of an arbitrary connected database (could be an expense
tracker, a CRM, anything). Building fact/dimension tables requires knowing
what's a fact vs. a dimension -- that's a real classification problem
(flagged in earlier scoping as the AI-ETL "fact/dimension classification"
sub-problem, not yet built). Until that exists, tables are cleaned and
loaded as-is, with real metadata (version + FK graph) attached so the
warehouse still tells you how things are connected.
"""

import json
import re
import logging
from pathlib import Path

import duckdb
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("generic_pipeline")

TABULAR_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def _sanitize_table_name(stem: str) -> str:
    """Turns a filename stem into a safe SQL table name: lowercase,
    non-alphanumeric runs collapsed to a single underscore."""
    name = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return name or "table"


def read_folder_as_tables(folder: Path) -> dict:
    """Reads every CSV/Excel file in a folder into a DataFrame, keyed by a
    sanitized version of its filename. This is the generic (non-Olist)
    ingestion path: works on any dataset, not just files matching
    config.py's hardcoded Olist filenames."""
    folder = Path(folder)
    tables = {}
    if not folder.exists():
        return tables
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in TABULAR_EXTENSIONS:
            continue
        try:
            df = pd.read_excel(f) if f.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(f, low_memory=False)
        except Exception as e:
            logger.warning(f"Could not read {f.name}: {e}")
            continue
        name = _sanitize_table_name(f.stem)
        # avoid silently overwriting if two filenames sanitize to the same name
        original_name = name
        n = 2
        while name in tables:
            name = f"{original_name}_{n}"
            n += 1
        tables[name] = df
        logger.info(f"[{name}] read {len(df):,} rows, {len(df.columns)} columns from {f.name}")
    return tables


def infer_relationships(tables: dict) -> list:
    """Heuristic FK inference for arbitrary CSV/Excel tables -- no formal
    FK metadata exists for flat files the way SQLAlchemy's inspector gives
    us for a real database, so this is pattern-based, not ML: for every
    column that looks like an identifier (ends in '_id', or is exactly
    'id'), check every OTHER table for a column with the same name whose
    values are highly unique there (i.e. that table's primary key) -- if
    found, propose a relationship pointing at it. Same category of
    technique as the PK/FK heuristics scoped earlier for the AI-ETL
    concept (uniqueness ratios + name matching), not a learned model.

    A real foreign key points from the 'many' side to the 'one' side, so
    a candidate is only accepted if the column is LESS unique in the
    source table than in the target table -- this rejects the ambiguous
    case where a column happens to be unique in both tables (e.g. a small
    sample where every value coincidentally appears once on both sides),
    which would otherwise produce a nonsensical relationship in both
    directions at once."""
    relationships = []
    UNIQUENESS_THRESHOLD = 0.95

    def uniqueness(df, col):
        non_null = df[col].dropna()
        return (non_null.nunique() / len(non_null)) if len(non_null) else 0.0

    for from_table, from_df in tables.items():
        for col in from_df.columns:
            col_lower = col.lower()
            if not (col_lower.endswith("_id") or col_lower == "id"):
                continue
            from_uniqueness = uniqueness(from_df, col)

            best_match = None
            for to_table, to_df in tables.items():
                if to_table == from_table or col not in to_df.columns:
                    continue
                to_uniqueness = uniqueness(to_df, col)
                if to_uniqueness >= UNIQUENESS_THRESHOLD and to_uniqueness > from_uniqueness:
                    best_match = to_table
                    break

            if best_match:
                relationships.append({
                    "from_table": from_table, "from_column": col,
                    "to_table": best_match, "to_column": col,
                })
    return relationships


def clean_generic(tables: dict) -> dict:
    """Basic, domain-agnostic cleaning: drop exact duplicate rows. Nothing
    fancier is safe to do without knowing what each column means (e.g. we
    can't median-fill a column if we don't know if it's numeric-meaningful
    or an ID)."""
    cleaned = {}
    for name, df in tables.items():
        before = len(df)
        df = df.drop_duplicates()
        after = len(df)
        if before != after:
            logger.info(f"[{name}] removed {before - after} exact duplicate row(s)")
        cleaned[name] = df
    return cleaned


def load_generic(tables: dict, db_path: Path) -> dict:
    """Loads each table as-is into DuckDB. Returns {table_name: row_count}."""
    con = duckdb.connect(str(db_path))
    counts = {}
    for name, df in tables.items():
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
        con.register("tmp_df", df)
        con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM tmp_df')
        con.unregister("tmp_df")
        count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        counts[name] = count
        logger.info(f'Loaded "{name}" -> {count:,} rows into {db_path.name}')
    con.close()
    return counts


def write_metadata(db_path: Path, version: str, relationships: list, source_type: str = "mysql"):
    """Writes source metadata into the warehouse itself, so it travels with
    the .duckdb file: MySQL version (for version-aware query generation
    later) and the real FK relationship graph (for visualization + any
    downstream tool that needs to know how tables join)."""
    con = duckdb.connect(str(db_path))

    con.execute("DROP TABLE IF EXISTS _warehouse_info")
    con.execute("CREATE TABLE _warehouse_info (key VARCHAR, value VARCHAR)")
    con.execute(
        "INSERT INTO _warehouse_info VALUES (?, ?), (?, ?)",
        ["source_type", source_type, "source_version", version or "unknown"],
    )

    con.execute("DROP TABLE IF EXISTS _warehouse_relationships")
    con.execute(
        "CREATE TABLE _warehouse_relationships "
        "(from_table VARCHAR, from_column VARCHAR, to_table VARCHAR, to_column VARCHAR)"
    )
    if relationships:
        con.executemany(
            "INSERT INTO _warehouse_relationships VALUES (?, ?, ?, ?)",
            [[r["from_table"], r["from_column"], r["to_table"], r["to_column"]] for r in relationships],
        )

    con.close()
    logger.info(f"Wrote warehouse metadata: version={version}, {len(relationships)} relationship(s)")


def get_warehouse_summary(db_path: Path) -> dict:
    """Reads back everything needed for the schema visualization + summary
    view: table list with row/column counts, relationships, and source
    metadata -- whatever is present in the given warehouse file."""
    if not Path(db_path).exists():
        return {"exists": False}

    con = duckdb.connect(str(db_path), read_only=True)

    all_tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()]

    user_tables = [t for t in all_tables if not t.startswith("_warehouse_")]

    tables_info = []
    for t in user_tables:
        row_count = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        col_count = len(con.execute(f'SELECT * FROM "{t}" LIMIT 0').description)
        tables_info.append({"table": t, "rows": row_count, "columns": col_count})

    relationships = []
    if "_warehouse_relationships" in all_tables:
        rows = con.execute("SELECT from_table, from_column, to_table, to_column FROM _warehouse_relationships").fetchall()
        relationships = [
            {"from_table": r[0], "from_column": r[1], "to_table": r[2], "to_column": r[3]}
            for r in rows
        ]

    info = {}
    if "_warehouse_info" in all_tables:
        rows = con.execute("SELECT key, value FROM _warehouse_info").fetchall()
        info = {k: v for k, v in rows}

    con.close()
    return {
        "exists": True,
        "tables": tables_info,
        "relationships": relationships,
        "info": info,
    }
