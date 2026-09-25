"""
load.py
Loads the star schema into a DuckDB warehouse file.

WHY DUCKDB (recap of earlier decision, encoded here so it's not lost):
  - Embedded, zero infra/ops — no server to deploy for a college project demo
  - Columnar + vectorized execution -> real OLAP performance on star schema
  - Native pandas/parquet interop -> trivial to load from this pipeline
  - Single .duckdb file -> easy to hand in / version / share with your team
"""

import duckdb
import logging
from pathlib import Path
from config import WAREHOUSE_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("load")


def load_to_warehouse(warehouse_tables: dict, db_path: Path = WAREHOUSE_PATH):
    con = duckdb.connect(str(db_path))

    for table_name, df in warehouse_tables.items():
        con.execute(f"DROP TABLE IF EXISTS {table_name}")
        con.register("tmp_df", df)
        con.execute(f"CREATE TABLE {table_name} AS SELECT * FROM tmp_df")
        con.unregister("tmp_df")
        logger.info(f"Loaded {table_name} -> {len(df):,} rows into {db_path.name}")

    # --- indexes on fact table foreign keys (DuckDB benefits from these on joins/filters) ---
    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_fact_customer ON fact_order_items(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_product ON fact_order_items(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_seller ON fact_order_items(seller_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_date ON fact_order_items(date_id)",
        "CREATE INDEX IF NOT EXISTS idx_dim_customer_pk ON dim_customers(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_dim_product_pk ON dim_products(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_dim_seller_pk ON dim_sellers(seller_id)",
        "CREATE INDEX IF NOT EXISTS idx_dim_date_pk ON dim_date(date_id)",
    ]
    for stmt in index_statements:
        con.execute(stmt)
    logger.info("Indexes created on all fact FK columns and dimension PKs")

    # --- sanity check: row counts ---
    for table_name in warehouse_tables:
        count = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        logger.info(f"Verified {table_name}: {count:,} rows in warehouse")

    con.close()
    logger.info(f"Warehouse build complete: {db_path}")


if __name__ == "__main__":
    from extract import extract_all
    from clean import clean_all
    from transform import build_star_schema

    raw = extract_all()
    cleaned, _ = clean_all(raw)
    warehouse_tables = build_star_schema(cleaned)
    load_to_warehouse(warehouse_tables)
