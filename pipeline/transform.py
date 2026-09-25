"""
transform.py
Phase 3: Enterprise Data Warehouse — star schema construction.

Builds:
  dim_customers   - customer attributes
  dim_products    - product attributes (+ English category name)
  dim_sellers     - seller attributes
  dim_date        - standard date dimension (for time-based slicing)
  fact_order_items - grain: one row per (order_id, order_item_id)
                     measures: price, freight_value, payment_value (aggregated),
                     review_score (aggregated), delivery_days (derived KPI)

This is a classic star schema (not snowflake) — chosen because:
  - your fact table has a small, stable set of dimensions (4)
  - query performance matters more than storage normalization here
  - it maps directly to what DuckDB/OLAP engines optimize for
Snowflaking dim_products further (e.g. separate category table) would
add join complexity with no real benefit at this data volume (~100k rows).
"""

import pandas as pd
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("transform")


def build_dim_customers(customers: pd.DataFrame) -> pd.DataFrame:
    dim = customers[[
        "customer_id", "customer_unique_id", "customer_zip_code_prefix",
        "customer_city", "customer_state"
    ]].drop_duplicates(subset=["customer_id"]).reset_index(drop=True)
    logger.info(f"dim_customers: {len(dim):,} rows")
    return dim


def build_dim_products(products: pd.DataFrame, category_translation: pd.DataFrame) -> pd.DataFrame:
    dim = products.merge(category_translation, on="product_category_name", how="left")
    dim["product_category_name_english"] = dim["product_category_name_english"].fillna(
        dim["product_category_name"]
    ).fillna("unknown")
    dim = dim[[
        "product_id", "product_category_name_english",
        "product_weight_g", "product_length_cm", "product_height_cm",
        "product_width_cm", "product_photos_qty"
    ]].drop_duplicates(subset=["product_id"]).reset_index(drop=True)
    logger.info(f"dim_products: {len(dim):,} rows")
    return dim


def build_dim_sellers(sellers: pd.DataFrame) -> pd.DataFrame:
    dim = sellers[[
        "seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"
    ]].drop_duplicates(subset=["seller_id"]).reset_index(drop=True)
    logger.info(f"dim_sellers: {len(dim):,} rows")
    return dim


def build_dim_date(orders: pd.DataFrame) -> pd.DataFrame:
    """Generates one row per calendar date spanning the observed order range."""
    min_date = orders["order_purchase_timestamp"].min().normalize()
    max_date = orders["order_purchase_timestamp"].max().normalize()
    dates = pd.date_range(min_date, max_date, freq="D")
    dim = pd.DataFrame({"date": dates})
    dim["date_id"] = dim["date"].dt.strftime("%Y%m%d").astype(int)
    dim["year"] = dim["date"].dt.year
    dim["month"] = dim["date"].dt.month
    dim["day"] = dim["date"].dt.day
    dim["quarter"] = dim["date"].dt.quarter
    dim["weekday_name"] = dim["date"].dt.day_name()
    dim["is_weekend"] = dim["date"].dt.dayofweek >= 5
    logger.info(f"dim_date: {len(dim):,} rows ({min_date.date()} to {max_date.date()})")
    return dim[["date_id", "date", "year", "month", "day", "quarter", "weekday_name", "is_weekend"]]


def build_fact_order_items(
    order_items: pd.DataFrame,
    orders: pd.DataFrame,
    payments: pd.DataFrame,
    reviews: pd.DataFrame,
) -> pd.DataFrame:
    """
    Grain: one row per order line item.
    Payments and reviews are 1-to-many with orders, so they're aggregated
    to order level before joining, to avoid fan-out duplication of order_items.
    """
    # Aggregate payments to order level (an order can have multiple payment rows,
    # e.g. split across installments/methods)
    payments_agg = payments.groupby("order_id").agg(
        total_payment_value=("payment_value", "sum"),
        payment_installments_max=("payment_installments", "max"),
        primary_payment_type=("payment_type", lambda x: x.mode().iloc[0] if not x.mode().empty else "unknown"),
    ).reset_index()

    # Aggregate reviews to order level (rare duplicate reviews per order)
    reviews_agg = reviews.groupby("order_id").agg(
        review_score=("review_score", "mean"),
    ).reset_index()

    fact = order_items.merge(
        orders[["order_id", "customer_id", "order_status", "order_purchase_timestamp",
                "order_delivered_customer_date", "order_estimated_delivery_date"]],
        on="order_id", how="left",
    )
    fact = fact.merge(payments_agg, on="order_id", how="left")
    fact = fact.merge(reviews_agg, on="order_id", how="left")

    # --- derived KPIs (Phase 4: business KPI generation) ---
    # NaT-safe: undelivered/canceled orders legitimately have no delivered_customer_date.
    fact["order_delivered_customer_date"] = pd.to_datetime(fact["order_delivered_customer_date"], errors="coerce")
    fact["order_purchase_timestamp"] = pd.to_datetime(fact["order_purchase_timestamp"], errors="coerce")
    fact["order_estimated_delivery_date"] = pd.to_datetime(fact["order_estimated_delivery_date"], errors="coerce")

    fact["delivery_days"] = (
        fact["order_delivered_customer_date"] - fact["order_purchase_timestamp"]
    ).dt.days  # NaN where undelivered — correct, not an error
    fact["delivered_late"] = (
        fact["order_delivered_customer_date"] > fact["order_estimated_delivery_date"]
    ).fillna(False)  # can't be "late" if never delivered
    fact["date_id"] = fact["order_purchase_timestamp"].dt.strftime("%Y%m%d").astype("Int64")
    fact["order_total_value"] = fact["price"] + fact["freight_value"]

    fact = fact.rename(columns={"order_item_id": "order_item_seq"})
    fact["order_item_sk"] = range(1, len(fact) + 1)  # surrogate key

    cols = [
        "order_item_sk", "order_id", "order_item_seq", "customer_id", "product_id",
        "seller_id", "date_id", "price", "freight_value", "order_total_value",
        "total_payment_value", "primary_payment_type", "payment_installments_max",
        "review_score", "order_status", "delivery_days", "delivered_late",
    ]
    fact = fact[[c for c in cols if c in fact.columns]]
    logger.info(f"fact_order_items: {len(fact):,} rows")
    return fact


def build_star_schema(cleaned_tables: dict) -> dict:
    """Orchestrates dimension + fact construction. Returns dict of warehouse tables."""
    dim_customers = build_dim_customers(cleaned_tables["customers"])
    dim_products = build_dim_products(cleaned_tables["products"], cleaned_tables["category_translation"])
    dim_sellers = build_dim_sellers(cleaned_tables["sellers"])
    dim_date = build_dim_date(cleaned_tables["orders"])
    fact_order_items = build_fact_order_items(
        cleaned_tables["order_items"],
        cleaned_tables["orders"],
        cleaned_tables["payments"],
        cleaned_tables["reviews"],
    )

    return {
        "dim_customers": dim_customers,
        "dim_products": dim_products,
        "dim_sellers": dim_sellers,
        "dim_date": dim_date,
        "fact_order_items": fact_order_items,
    }


if __name__ == "__main__":
    from extract import extract_all
    from clean import clean_all
    raw = extract_all()
    cleaned, _ = clean_all(raw)
    warehouse_tables = build_star_schema(cleaned)
    for name, df in warehouse_tables.items():
        print(f"{name}: {df.shape}")
