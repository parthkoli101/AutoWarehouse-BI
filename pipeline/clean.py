"""
clean.py
Phase 2: AI-assisted ETL — cleaning layer.

Implements, per table:
  - duplicate detection & removal (primary-key based)
  - missing value handling (median / mode / flag_unknown / drop_row)
  - outlier detection (IQR method — chosen over Isolation Forest here
    because it's interpretable, cheap, and sufficient for single-column
    numeric checks; Isolation Forest is reserved for the ML layer where
    multivariate anomaly detection actually adds value — see notes below)
  - per-table data quality score (0-100)

WHY IQR HERE AND NOT ISOLATION FOREST:
Outlier detection has two different jobs in this platform:
  1. ETL-time sanity checking (this file) — "is this single value
     plausible for this column" — IQR is the right tool: fast,
     deterministic, explainable to a non-technical reviewer.
  2. ML-layer anomaly detection (separate module, Phase 5) — "is this
     multi-dimensional transaction pattern suspicious" — that's where
     Isolation Forest belongs, because it needs multiple correlated
     features to be useful. Using Isolation Forest here would be
     overkill and slower for no accuracy gain on single columns.
"""

import pandas as pd
import numpy as np
import logging
from config import SOURCE_TABLES, OUTLIER_COLUMNS, MISSING_VALUE_STRATEGY

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("clean")


def _dedupe(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
    pk = SOURCE_TABLES[table_name]["primary_key"]
    if not pk:
        return df
    before = len(df)
    df = df.drop_duplicates(subset=pk, keep="first")
    removed = before - len(df)
    if removed:
        logger.info(f"[{table_name}] removed {removed} duplicate rows on {pk}")
    return df


def _handle_missing(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
    for col in df.columns:
        key = f"{table_name}.{col}"
        strategy = MISSING_VALUE_STRATEGY.get(key)
        null_count = df[col].isna().sum()
        if null_count == 0:
            continue

        if strategy == "median" and pd.api.types.is_numeric_dtype(df[col]):
            fill_value = df[col].median()
            df[col] = df[col].fillna(fill_value)
            logger.info(f"[{table_name}.{col}] filled {null_count} nulls with median={fill_value:.2f}")

        elif strategy == "mode" and not df[col].mode().empty:
            fill_value = df[col].mode()[0]
            df[col] = df[col].fillna(fill_value)
            logger.info(f"[{table_name}.{col}] filled {null_count} nulls with mode='{fill_value}'")

        elif strategy == "flag_unknown":
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                # Leave datetime nulls as NaT — a sentinel like -1 corrupts the dtype
                # and breaks downstream date arithmetic. NaT IS the "unknown" flag here.
                logger.info(f"[{table_name}.{col}] left {null_count} datetime nulls as NaT (legitimately missing, e.g. undelivered orders)")
            elif pd.api.types.is_numeric_dtype(df[col]):
                df[col] = df[col].fillna(-1)
                logger.info(f"[{table_name}.{col}] flagged {null_count} nulls as -1")
            else:
                df[col] = df[col].fillna("unknown")
                logger.info(f"[{table_name}.{col}] flagged {null_count} nulls as 'unknown'")

        elif strategy == "drop_row":
            df = df.dropna(subset=[col])
            logger.info(f"[{table_name}.{col}] dropped {null_count} rows with nulls")

        # else: no strategy defined -> leave as-is, will be reflected in quality score

    return df


def _detect_outliers_iqr(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
    """Flags outliers via IQR; does NOT remove them (business decisions on
    outliers — e.g. legitimately high-value orders — should not be silently
    dropped). Adds a boolean `<col>_is_outlier` column instead."""
    cols = OUTLIER_COLUMNS.get(table_name, [])
    for col in cols:
        if col not in df.columns or not pd.api.types.is_numeric_dtype(df[col]):
            continue
        q1, q3 = df[col].quantile(0.25), df[col].quantile(0.75)
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        flag_col = f"{col}_is_outlier"
        df[flag_col] = ~df[col].between(lower, upper)
        n_outliers = df[flag_col].sum()
        if n_outliers:
            logger.info(f"[{table_name}.{col}] flagged {n_outliers} outliers (bounds: {lower:.2f} - {upper:.2f})")
    return df


def compute_quality_score(df: pd.DataFrame, table_name: str) -> dict:
    """Simple, explainable data quality score: 100 - weighted penalty for
    nulls, duplicates, and outliers. Not ML-based by design — this needs
    to be auditable in a report, not a black box."""
    pk = SOURCE_TABLES[table_name]["primary_key"]
    total_cells = df.shape[0] * df.shape[1] if df.shape[0] else 1

    null_pct = df.isna().sum().sum() / total_cells * 100
    dupe_pct = (df.duplicated(subset=pk).sum() / len(df) * 100) if pk and len(df) else 0
    outlier_cols = [c for c in df.columns if c.endswith("_is_outlier")]
    outlier_pct = (df[outlier_cols].sum().sum() / len(df) * 100) if outlier_cols and len(df) else 0

    score = max(0, 100 - (null_pct * 0.5) - (dupe_pct * 1.0) - (outlier_pct * 0.2))

    return {
        "table": table_name,
        "rows": len(df),
        "null_pct": round(null_pct, 2),
        "duplicate_pct": round(dupe_pct, 2),
        "outlier_pct": round(outlier_pct, 2),
        "quality_score": round(score, 1),
    }


def clean_table(df: pd.DataFrame, table_name: str) -> tuple:
    """Returns (cleaned_df, quality_report_dict)."""
    df = _dedupe(df, table_name)
    df = _handle_missing(df, table_name)
    df = _detect_outliers_iqr(df, table_name)
    report = compute_quality_score(df, table_name)
    logger.info(f"[{table_name}] quality score: {report['quality_score']}/100")
    return df, report


def clean_all(tables: dict) -> tuple:
    """tables: {name: raw_df} -> ({name: cleaned_df}, [quality_reports])"""
    cleaned = {}
    reports = []
    for name, df in tables.items():
        cleaned_df, report = clean_table(df, name)
        cleaned[name] = cleaned_df
        reports.append(report)
    return cleaned, reports


if __name__ == "__main__":
    from extract import extract_all
    raw = extract_all()
    cleaned, reports = clean_all(raw)
    for r in reports:
        print(r)
