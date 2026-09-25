"""
main.py
Orchestrates the full pipeline: Extract -> Clean -> Transform -> Load -> Quality Report.

USAGE:
    1. Download the Olist dataset from Kaggle:
       https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
    2. Unzip all CSVs into ./data/raw/  (must match filenames in config.py)
    3. Run:  python main.py

    Optional flags:
       python main.py --skip-quality     # skip the quality report step
       python main.py --db-path custom.duckdb
"""

import argparse
import logging
import time
from pathlib import Path

from extract import extract_all
from clean import clean_all
from transform import build_star_schema
from load import load_to_warehouse
from quality import generate_report
from config import WAREHOUSE_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("pipeline")


def run_pipeline(db_path: Path = WAREHOUSE_PATH, skip_quality: bool = False):
    start = time.time()
    logger.info("=" * 70)
    logger.info("STARTING ETL PIPELINE")
    logger.info("=" * 70)

    logger.info("STAGE 1/5: Extraction")
    raw_tables = extract_all()
    if not raw_tables:
        logger.error("No tables extracted. Check ./data/raw/ for the Olist CSVs. Aborting.")
        return

    logger.info("STAGE 2/5: Cleaning")
    cleaned_tables, quality_reports = clean_all(raw_tables)

    logger.info("STAGE 3/5: Transform (star schema)")
    warehouse_tables = build_star_schema(cleaned_tables)

    logger.info("STAGE 4/5: Load into warehouse")
    load_to_warehouse(warehouse_tables, db_path=db_path)

    if not skip_quality:
        logger.info("STAGE 5/5: Quality report")
        generate_report(quality_reports)

    elapsed = time.time() - start
    logger.info("=" * 70)
    logger.info(f"PIPELINE COMPLETE in {elapsed:.1f}s -> {db_path}")
    logger.info("=" * 70)
    logger.info("Query it with: import duckdb; duckdb.connect('%s').sql('SELECT * FROM fact_order_items LIMIT 5').show()" % db_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI-assisted ETL pipeline -> Enterprise Data Warehouse")
    parser.add_argument("--skip-quality", action="store_true", help="skip quality report generation")
    parser.add_argument("--db-path", type=str, default=str(WAREHOUSE_PATH), help="output DuckDB file path")
    args = parser.parse_args()

    run_pipeline(db_path=Path(args.db_path), skip_quality=args.skip_quality)
