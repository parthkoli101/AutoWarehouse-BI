"""
extract.py
Phase 1: Intelligent Data Ingestion.

Loads raw CSVs, validates against expected schema, applies dtype hints,
parses date columns, and reports structural issues before anything
gets cleaned or transformed.
"""

import pandas as pd
import logging
from pathlib import Path
from config import RAW_DATA_DIR, SOURCE_TABLES

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("extract")


class ExtractionError(Exception):
    pass


def _read_tabular(filepath: Path) -> pd.DataFrame:
    """Reads a CSV or Excel file into a DataFrame, based on extension.
    Excel support added so real-world handoffs (.xlsx) work the same as .csv
    without any change to the rest of the pipeline downstream."""
    suffix = filepath.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(filepath)
    return pd.read_csv(filepath, low_memory=False)


def _resolve_filepath(cfg: dict, raw_dir: Path) -> Path:
    """Finds the source file for a table, accepting either .csv or .xlsx/.xls
    with the same base filename (config.py registers the .csv name; this
    also looks for an Excel counterpart if the CSV isn't present)."""
    base = raw_dir / cfg["filename"]
    if base.exists():
        return base
    stem = Path(cfg["filename"]).stem
    for ext in (".xlsx", ".xls"):
        candidate = raw_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return base  # fall through to the not-found error with the original expected name


def extract_table(table_name: str, raw_dir: Path = None) -> pd.DataFrame:
    """Load and validate a single source table by name (must exist in
    config.SOURCE_TABLES). raw_dir defaults to config.RAW_DATA_DIR (the
    standalone CLI's folder) but can be overridden -- used by app.py to
    point at a warehouse-scoped staging folder instead."""
    raw_dir = raw_dir or RAW_DATA_DIR
    if table_name not in SOURCE_TABLES:
        raise ExtractionError(f"'{table_name}' is not registered in config.SOURCE_TABLES")

    cfg = SOURCE_TABLES[table_name]
    filepath = _resolve_filepath(cfg, raw_dir)

    if not filepath.exists():
        raise ExtractionError(
            f"Expected file not found: {filepath}\n"
            f"Place the raw CSV or Excel file in {raw_dir}/ before running the pipeline."
        )

    df = _read_tabular(filepath)
    logger.info(f"[{table_name}] loaded {len(df):,} rows, {len(df.columns)} columns from {filepath.name}")

    # --- Structural validation ---
    missing_required = [c for c in cfg["required_columns"] if c not in df.columns]
    if missing_required:
        raise ExtractionError(
            f"[{table_name}] missing required columns: {missing_required}. "
            f"Schema mismatch — check the source file version."
        )

    # --- dtype hints ---
    for col, dtype in cfg["dtype_hints"].items():
        if col in df.columns:
            try:
                df[col] = df[col].astype(dtype)
            except Exception as e:
                logger.warning(f"[{table_name}] could not cast {col} to {dtype}: {e}")

    # --- date parsing ---
    for col in cfg["date_columns"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # --- primary key uniqueness check ---
    pk = cfg["primary_key"]
    if pk:
        dupes = df.duplicated(subset=pk).sum()
        if dupes > 0:
            logger.warning(f"[{table_name}] {dupes} duplicate rows on primary key {pk} — will be deduped in clean stage")

    return df


def extract_all(raw_dir: Path = None) -> dict:
    """Extract every registered table. Returns {table_name: DataFrame}.
    raw_dir defaults to config.RAW_DATA_DIR; app.py overrides it with a
    warehouse-scoped folder so different warehouses' files never mix."""
    tables = {}
    failures = []
    for name in SOURCE_TABLES:
        try:
            tables[name] = extract_table(name, raw_dir=raw_dir)
        except ExtractionError as e:
            logger.error(str(e))
            failures.append(name)

    if failures:
        logger.warning(f"Extraction incomplete. Missing/failed tables: {failures}")
    else:
        logger.info(f"Extraction complete. {len(tables)}/{len(SOURCE_TABLES)} tables loaded.")

    return tables


def olist_files_present(raw_dir: Path) -> bool:
    """Checks whether ALL 9 registered Olist source files are present in
    the given folder (in either .csv or .xlsx/.xls form) -- used to decide
    whether to run the Olist-specific star-schema pipeline or fall back
    to the generic (any-dataset) path."""
    raw_dir = Path(raw_dir)
    for cfg in SOURCE_TABLES.values():
        filepath = _resolve_filepath(cfg, raw_dir)
        if not filepath.exists():
            return False
    return True


if __name__ == "__main__":
    data = extract_all()
    for name, df in data.items():
        print(f"{name}: {df.shape}")
