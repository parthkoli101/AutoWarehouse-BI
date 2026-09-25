"""
warehouse_manager.py

Supports multiple, independently named warehouses instead of one fixed
warehouse file -- each is a separate .duckdb file under
data/warehouse/. Also enforces REPLACE semantics on build: building from
Files or Database clears the target warehouse's existing tables first,
rather than the old behavior of only dropping the specific table names
about to be written (which silently left a previous build's tables from a
different source sitting alongside the new ones -- the actual bug being
fixed here).
"""

import re
import duckdb
from pathlib import Path

WAREHOUSE_DIR = Path(__file__).resolve().parent.parent / "data" / "warehouse"

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,50}$")


class WarehouseError(Exception):
    pass


def _sanitize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise WarehouseError("Warehouse name cannot be empty.")
    if not NAME_PATTERN.match(name):
        raise WarehouseError("Warehouse name can only contain letters, numbers, underscores, and hyphens (max 50 chars).")
    return name


def sanitize_name(name: str) -> str:
    """Public wrapper -- other modules (e.g. app.py, for staging folder
    names) should use this rather than reaching into the private helper."""
    return _sanitize_name(name)


def resolve_path(name: str) -> Path:
    """Turns a warehouse name into its file path. Sanitizes first -- this
    is the one function every other route trusts to prevent path
    traversal (e.g. name='../../etc/passwd') since the name arrives from
    an HTTP request."""
    safe_name = _sanitize_name(name)
    return WAREHOUSE_DIR / f"{safe_name}.duckdb"


def _config_path_for(db_path: Path) -> Path:
    """Matches dashboard_engine's per-warehouse config file naming."""
    return db_path.parent / f"{db_path.stem}_dashboard_config.json"


def list_warehouses() -> list:
    """Returns every warehouse currently on disk with basic stats."""
    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for f in sorted(WAREHOUSE_DIR.glob("*.duckdb")):
        name = f.stem
        size_bytes = f.stat().st_size
        table_count = 0
        try:
            con = duckdb.connect(str(f), read_only=True)
            tables = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()]
            table_count = len([t for t in tables if not t.startswith("_warehouse_")])
            con.close()
        except Exception:
            pass  # corrupted/locked file -- still list it, just with 0 tables shown
        results.append({
            "name": name,
            "size_bytes": size_bytes,
            "table_count": table_count,
            "modified_at": f.stat().st_mtime,
            "has_dashboard": _config_path_for(f).exists(),
        })
    return results


def create_warehouse(name: str) -> Path:
    path = resolve_path(name)
    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise WarehouseError(f"A warehouse named '{name}' already exists.")
    con = duckdb.connect(str(path))  # creates an empty file
    con.close()
    return path


def delete_warehouse(name: str) -> None:
    path = resolve_path(name)
    if not path.exists():
        raise WarehouseError(f"No warehouse named '{name}' exists.")
    path.unlink()
    config_file = _config_path_for(path)
    if config_file.exists():
        config_file.unlink()


def clear_user_tables(db_path: Path) -> int:
    """Drops every table in the given warehouse (including the
    _warehouse_info/_warehouse_relationships metadata tables) so a
    'Create Warehouse' action is a genuine clean-slate rebuild, not an
    additive merge with whatever a previous build (possibly from a
    different source) left behind. Returns how many tables were dropped."""
    if not db_path.exists():
        return 0
    con = duckdb.connect(str(db_path))
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()]
    for t in tables:
        con.execute(f'DROP TABLE IF EXISTS "{t}"')
    con.close()
    return len(tables)
