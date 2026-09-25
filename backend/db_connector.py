"""
Handles live MySQL connections, schema reflection, and pulling tables into
pandas for the pipeline to process.

Uses SQLAlchemy's inspector to read REAL schema metadata straight from the
database engine -- table names, column names, types, primary keys, AND
foreign keys. No guessing/heuristics needed here, unlike CSV ingestion,
because the database already knows its own structure with certainty.

Also reads SELECT VERSION() -- MySQL syntax varies by version (e.g. window
functions and CTEs need 8.0+), so storing this alongside the warehouse lets
any later query-generation layer (NL2SQL) pick syntax the source DB
actually supports.
"""

from urllib.parse import quote_plus
import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError


def _build_url(creds: dict) -> str:
    host = creds["host"]
    port = creds.get("port") or 3306
    # URL-encode user/password: special characters like '@', ':', '/' in a
    # raw password break the user:password@host parsing, since those are
    # the same characters the URL format itself uses as separators.
    user = quote_plus(creds["username"])
    password = quote_plus(creds.get("password", ""))
    database = creds["database"]
    # pymysql driver -- pure python, no system-level MySQL client needed
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"


def _get_engine(creds: dict):
    url = _build_url(creds)
    return create_engine(url, connect_args={"connect_timeout": 5})


def test_connection(creds: dict):
    """Returns (True, None) on success, or (False, error_message) on failure."""
    try:
        engine = _get_engine(creds)
        with engine.connect():
            pass
        return True, None
    except OperationalError:
        return False, "Could not connect. Check host/port/username/password and that MySQL is running."
    except SQLAlchemyError as e:
        return False, f"Connection error: {str(e)}"


def get_version(creds: dict) -> str:
    """Runs SELECT VERSION() against the connected MySQL server."""
    engine = _get_engine(creds)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT VERSION()")).scalar()
    return str(result)


def get_schema(creds: dict):
    """Returns (tables, relationships).

    tables: list of {table, columns: [{name, type, primary_key}]}
    relationships: list of {from_table, from_column, to_table, to_column}
        -- the real foreign-key graph, walked via SQLAlchemy's inspector,
        not inferred/guessed. This is what shows how tables are actually
        interlinked, for both the metadata table and the visualization.
    """
    engine = _get_engine(creds)
    inspector = inspect(engine)

    tables = []
    relationships = []

    for table_name in inspector.get_table_names():
        pk_cols = set(inspector.get_pk_constraint(table_name).get("constrained_columns", []))
        columns = []
        for col in inspector.get_columns(table_name):
            columns.append({
                "name": col["name"],
                "type": str(col["type"]),
                "primary_key": col["name"] in pk_cols,
            })
        tables.append({"table": table_name, "columns": columns})

        # walk foreign keys for this table -> build the relationship edges
        for fk in inspector.get_foreign_keys(table_name):
            referred_table = fk.get("referred_table")
            constrained_cols = fk.get("constrained_columns", [])
            referred_cols = fk.get("referred_columns", [])
            for from_col, to_col in zip(constrained_cols, referred_cols):
                relationships.append({
                    "from_table": table_name,
                    "from_column": from_col,
                    "to_table": referred_table,
                    "to_column": to_col,
                })

    return tables, relationships


def extract_mysql_tables(creds: dict) -> dict:
    """Pulls every table in the connected database into a DataFrame, keyed
    by table name. This is the DB-source equivalent of extract.py's
    CSV/Excel extraction -- same downstream clean/transform/load pipeline
    consumes it regardless of where the raw data came from."""
    engine = _get_engine(creds)
    inspector = inspect(engine)

    tables = {}
    with engine.connect() as conn:
        for table_name in inspector.get_table_names():
            df = pd.read_sql(text(f"SELECT * FROM `{table_name}`"), conn)
            tables[table_name] = df

    return tables
