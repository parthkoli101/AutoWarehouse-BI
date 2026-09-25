"""
config.py
Central configuration for the AI-assisted ETL pipeline.

To add a new source dataset (e.g. Favorita, H&M), add a new entry to
SOURCE_TABLES following the same pattern, then register it in
transform.py's build_star_schema().
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
# Resolved relative to the project root (one level up from this pipeline/
# folder), NOT relative to the current working directory -- this way paths
# are correct whether the pipeline is run via `python main.py` from the
# project root, or in-process from backend/app.py (which lives in a
# different folder and may be launched with a different cwd).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"                       # dump raw CSVs/Excel here
STAGING_DIR = PROJECT_ROOT / "data" / "staging"                    # cleaned intermediate parquet files
WAREHOUSE_PATH = PROJECT_ROOT / "data" / "warehouse" / "default.duckdb"
QUALITY_REPORT_DIR = PROJECT_ROOT / "data" / "quality_reports"

for d in [RAW_DATA_DIR, STAGING_DIR, WAREHOUSE_PATH.parent, QUALITY_REPORT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# SOURCE TABLE REGISTRY (Olist Brazilian E-Commerce)
# ---------------------------------------------------------------------------
# filename            : expected CSV file name in RAW_DATA_DIR
# primary_key         : column(s) uniquely identifying a row
# foreign_keys        : dict of {column: referenced_table}
# dtype_hints         : column -> pandas dtype override (else inferred)
# date_columns        : columns to parse as datetime
# required_columns    : columns that must not be null (used for validation)
SOURCE_TABLES = {
    "customers": {
        "filename": "olist_customers_dataset.csv",
        "primary_key": ["customer_id"],
        "foreign_keys": {},
        "dtype_hints": {"customer_zip_code_prefix": "str"},
        "date_columns": [],
        "required_columns": ["customer_id"],
    },
    "orders": {
        "filename": "olist_orders_dataset.csv",
        "primary_key": ["order_id"],
        "foreign_keys": {"customer_id": "customers"},
        "dtype_hints": {},
        "date_columns": [
            "order_purchase_timestamp", "order_approved_at",
            "order_delivered_carrier_date", "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ],
        "required_columns": ["order_id", "customer_id", "order_status"],
    },
    "order_items": {
        "filename": "olist_order_items_dataset.csv",
        "primary_key": ["order_id", "order_item_id"],
        "foreign_keys": {"order_id": "orders", "product_id": "products", "seller_id": "sellers"},
        "dtype_hints": {},
        "date_columns": ["shipping_limit_date"],
        "required_columns": ["order_id", "product_id", "seller_id", "price"],
    },
    "payments": {
        "filename": "olist_order_payments_dataset.csv",
        "primary_key": ["order_id", "payment_sequential"],
        "foreign_keys": {"order_id": "orders"},
        "dtype_hints": {},
        "date_columns": [],
        "required_columns": ["order_id", "payment_value"],
    },
    "reviews": {
        "filename": "olist_order_reviews_dataset.csv",
        "primary_key": ["review_id"],
        "foreign_keys": {"order_id": "orders"},
        "dtype_hints": {},
        "date_columns": ["review_creation_date", "review_answer_timestamp"],
        "required_columns": ["order_id", "review_score"],
    },
    "products": {
        "filename": "olist_products_dataset.csv",
        "primary_key": ["product_id"],
        "foreign_keys": {},
        "dtype_hints": {},
        "date_columns": [],
        "required_columns": ["product_id"],
    },
    "sellers": {
        "filename": "olist_sellers_dataset.csv",
        "primary_key": ["seller_id"],
        "foreign_keys": {},
        "dtype_hints": {"seller_zip_code_prefix": "str"},
        "date_columns": [],
        "required_columns": ["seller_id"],
    },
    "category_translation": {
        "filename": "product_category_name_translation.csv",
        "primary_key": ["product_category_name"],
        "foreign_keys": {},
        "dtype_hints": {},
        "date_columns": [],
        "required_columns": [],
    },
    "geolocation": {
        "filename": "olist_geolocation_dataset.csv",
        "primary_key": [],  # many-to-many by zip prefix, no clean PK
        "foreign_keys": {},
        "dtype_hints": {"geolocation_zip_code_prefix": "str"},
        "date_columns": [],
        "required_columns": [],
    },
}

# Outlier detection: numeric columns per table worth checking (IQR method)
OUTLIER_COLUMNS = {
    "order_items": ["price", "freight_value"],
    "payments": ["payment_value"],
    "products": ["product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"],
}

# Missing value strategy per column: "drop_row" | "median" | "mode" | "flag_unknown"
MISSING_VALUE_STRATEGY = {
    "products.product_category_name": "flag_unknown",
    "products.product_weight_g": "median",
    "reviews.review_comment_message": "flag_unknown",
    "reviews.review_comment_title": "flag_unknown",
    "orders.order_delivered_customer_date": "flag_unknown",  # legitimately null for undelivered orders
}
