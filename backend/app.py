"""
Flask backend for the Warehouse Ingestion Console.

Running `python app.py` alone runs everything -- there is no separate
main.py step. The pipeline (extract/clean/transform/load) lives in
../pipeline/ and is imported and called in-process from here.

Multiple, independently named warehouses are supported (see
warehouse_manager.py) -- every build/read route takes a `warehouse` name
(defaulting to "default" for simplicity if the frontend doesn't specify
one). Building into a warehouse REPLACES its existing tables first (see
warehouse_manager.clear_user_tables) -- this is deliberate: building from
Files then from Database used to leave both sources' tables sitting side
by side in the same file, which looked like an unwanted merge. Each build
is now a clean-slate rebuild of whichever warehouse is targeted.

Routes:
  1. /api/upload                    -> stage CSV/Excel files (MongoDB + local, per warehouse)
  2. /api/create-warehouse-files    -> Olist star-schema pipeline OR generic (any dataset) pipeline,
                                        picked automatically based on what's actually staged
  3. /api/connect                   -> test a MySQL connection, show its schema
  4. /api/create-warehouse-database -> pull MySQL tables, version, FK graph -> warehouse
  5. /api/warehouse-schema          -> read back table list + relationships
  6. /api/warehouses (GET/POST)     -> list / create warehouses
  7. /api/warehouses/<name> (DELETE)-> delete a warehouse (+ its Mongo files, staging folder)
  8. /api/blueprint (GET/POST)      -> AI-generated warehouse overview (roles, purposes, narrative)
  9. /api/dashboard/design, /data   -> AI-designed dashboard (per warehouse)
 10. /api/status                    -> infrastructure status (MongoDB connected or not)
"""

import os
import sys
import logging
from pathlib import Path

from flask import Flask, render_template, request, jsonify

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_DIR = os.path.join(BASE_DIR, "pipeline")
sys.path.insert(0, PIPELINE_DIR)  # so `import config`, `import extract` etc. work as they do in main.py

from db_connector import test_connection, get_schema, get_version, extract_mysql_tables  # noqa: E402
import warehouse_manager as wm  # noqa: E402
import mongo_client as mongo  # noqa: E402

import config  # noqa: E402
from extract import extract_all, olist_files_present  # noqa: E402
from clean import clean_all  # noqa: E402
from transform import build_star_schema  # noqa: E402
from load import load_to_warehouse  # noqa: E402
from quality import generate_report  # noqa: E402
from generic_pipeline import (  # noqa: E402
    clean_generic, load_generic, write_metadata, get_warehouse_summary,
    read_folder_as_tables, infer_relationships,
)
from dashboard_engine import design_dashboards, refresh_dashboards  # noqa: E402
from ollama_client import OllamaError  # noqa: E402
from blueprint_engine import generate_blueprint, load_blueprint  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("app")

RAW_STAGING_ROOT = config.PROJECT_ROOT / "data" / "raw"  # per-warehouse subfolders live under here
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
DEFAULT_WAREHOUSE = "default"


def _staging_dir(warehouse_name: str) -> Path:
    """Each warehouse gets its own staging subfolder -- this is what
    prevents different warehouses' uploaded files from mixing, which was
    the actual bug: a single shared data/raw/ folder meant every warehouse
    read from the same pile of files regardless of which one they were
    meant for."""
    safe_name = wm.sanitize_name(warehouse_name or DEFAULT_WAREHOUSE)
    d = RAW_STAGING_ROOT / safe_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _materialize_from_mongo(warehouse_name: str, staging_dir: Path):
    """If MongoDB is reachable and has files stored for this warehouse,
    writes them into the local staging folder fresh (clearing stale local
    files first) so the pipeline always builds from MongoDB's copy when
    available -- MongoDB is the source of truth, local disk is just the
    materialized working copy the pipeline actually reads."""
    if not mongo.is_connected():
        return False
    files = mongo.list_files(warehouse_name)
    if not files:
        return False
    for f in staging_dir.glob("*"):
        if f.is_file():
            f.unlink()
    for f in files:
        data = mongo.get_file_bytes(warehouse_name, f["filename"])
        if data is not None:
            (staging_dir / f["filename"]).write_bytes(data)
    return True

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "frontend", "templates"),
    static_folder=os.path.join(BASE_DIR, "frontend", "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB ceiling for demo safety


def _resolve_warehouse(name: str) -> Path:
    """Every route uses this to turn a warehouse name into a validated path
    -- ensures a warehouse of that name exists as a file (creating an empty
    one on first use is fine; building into it is what actually populates
    it) and that the name itself was sanitized by warehouse_manager."""
    path = wm.resolve_path(name or DEFAULT_WAREHOUSE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# WAREHOUSE MANAGEMENT: list / create / delete named warehouses
# ---------------------------------------------------------------------------
@app.route("/api/warehouses", methods=["GET"])
def list_warehouses():
    return jsonify({"status": "ok", "warehouses": wm.list_warehouses()})


@app.route("/api/warehouses", methods=["POST"])
def create_warehouse():
    payload = request.get_json(force=True) or {}
    name = payload.get("name", "")
    try:
        wm.create_warehouse(name)
        return jsonify({"status": "ok", "message": f"Warehouse '{name}' created.", "warehouses": wm.list_warehouses()})
    except wm.WarehouseError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/warehouses/<name>", methods=["DELETE"])
def delete_warehouse(name):
    try:
        wm.delete_warehouse(name)
        mongo.delete_files_for_warehouse(name)
        staging_dir = RAW_STAGING_ROOT / wm.sanitize_name(name)
        if staging_dir.exists():
            for f in staging_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            staging_dir.rmdir()
        return jsonify({"status": "ok", "message": f"Warehouse '{name}' deleted.", "warehouses": wm.list_warehouses()})
    except wm.WarehouseError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ---------------------------------------------------------------------------
# INFRASTRUCTURE STATUS: does the frontend show Mongo as connected?
# ---------------------------------------------------------------------------
@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({"status": "ok", "mongo": mongo.connection_status()})


# ---------------------------------------------------------------------------
# PATH 1: file staging -- stored in MongoDB (per warehouse) when available,
# always also materialized to a warehouse-scoped local folder so the
# pipeline (which reads real files via pandas) has something to read.
# ---------------------------------------------------------------------------
@app.route("/api/upload", methods=["POST"])
def upload_files():
    """Accepts one or more CSV/Excel files (also used for 'folder' uploads,
    since browsers send a folder as a flat list of files with
    webkitRelativePath). Staged for a SPECIFIC warehouse -- this is what
    prevents different warehouses' files from mixing. Building the
    warehouse is still a separate explicit step via /api/create-warehouse-files."""
    files = request.files.getlist("files")
    warehouse_name = request.form.get("warehouse", DEFAULT_WAREHOUSE)

    if not files or files[0].filename == "":
        return jsonify({"status": "error", "message": "No files received."}), 400

    staging_dir = _staging_dir(warehouse_name)
    mongo_ok = mongo.is_connected()

    saved, skipped = [], []
    for f in files:
        filename = os.path.basename(f.filename)  # strip any folder path, flatten
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            skipped.append(filename)
            continue
        file_bytes = f.read()
        if mongo_ok:
            mongo.save_file(warehouse_name, filename, file_bytes)
        (staging_dir / filename).write_bytes(file_bytes)  # local working copy either way
        saved.append(filename)

    storage_note = "MongoDB" if mongo_ok else "local disk (MongoDB not connected)"
    return jsonify({
        "status": "ok",
        "saved": saved,
        "skipped": skipped,
        "mongo_connected": mongo_ok,
        "message": f"{len(saved)} file(s) staged for warehouse '{warehouse_name}' via {storage_note}."
                    + (f" {len(skipped)} unsupported file(s) skipped." if skipped else "")
    })


# ---------------------------------------------------------------------------
# BUTTON 1: "Create Warehouse from Files"
# ---------------------------------------------------------------------------
@app.route("/api/create-warehouse-files", methods=["POST"])
def create_warehouse_files():
    """Materializes this warehouse's staged files (from MongoDB if
    connected, else whatever's already in its local staging folder), then
    picks a pipeline based on what's actually there:

      - If all 9 registered Olist filenames are present -> the Olist-
        specific star-schema pipeline (extract/clean/transform/load).
      - Otherwise -> the generic pipeline: every CSV/Excel file becomes its
        own table as-is, with relationships heuristically inferred from
        column names + uniqueness (same honest, non-star-schema approach
        already used for the MySQL Database path, extended to cover any
        dataset instead of only Olist).

    Either way, the target warehouse's existing tables are cleared first
    -- a fresh build, not an additive merge with a previous build."""
    payload = request.get_json(silent=True) or {}
    warehouse_name = payload.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)
    staging_dir = _staging_dir(warehouse_name)

    _materialize_from_mongo(warehouse_name, staging_dir)

    if not any(staging_dir.glob("*")):
        return jsonify({
            "status": "error",
            "message": f"No files staged for warehouse '{warehouse_name}'. Upload files first."
        }), 400

    try:
        if olist_files_present(staging_dir):
            return _build_olist_warehouse(warehouse_name, db_path, staging_dir)
        else:
            return _build_generic_file_warehouse(warehouse_name, db_path, staging_dir)
    except Exception as e:
        logger.exception("Warehouse build (files) failed")
        return jsonify({"status": "error", "message": f"Pipeline failed: {str(e)}"}), 500


def _build_olist_warehouse(warehouse_name: str, db_path: Path, staging_dir: Path):
    logger.info(f"'{warehouse_name}': Olist filenames detected -- running star-schema pipeline")
    logger.info("STAGE 1/5: Extraction")
    raw_tables = extract_all(raw_dir=staging_dir)
    if not raw_tables:
        return jsonify({"status": "error", "message": "Extraction failed even though Olist filenames were detected."}), 400

    logger.info("STAGE 2/5: Cleaning")
    cleaned_tables, quality_reports = clean_all(raw_tables)

    logger.info("STAGE 3/5: Transform (star schema)")
    warehouse_tables = build_star_schema(cleaned_tables)

    dropped = wm.clear_user_tables(db_path)
    logger.info(f"STAGE 4/5: Load into warehouse '{warehouse_name}' (cleared {dropped} existing table(s) first)")
    load_to_warehouse(warehouse_tables, db_path=db_path)

    logger.info("STAGE 5/5: Quality report")
    generate_report(quality_reports)

    relationships = []
    for table_name, cfg in config.SOURCE_TABLES.items():
        for col, ref_table in cfg.get("foreign_keys", {}).items():
            relationships.append({"from_table": table_name, "from_column": col, "to_table": ref_table, "to_column": col})
    write_metadata(db_path, version="n/a (file source)", relationships=relationships, source_type="files-olist")

    tables_loaded = {name: len(df) for name, df in warehouse_tables.items()}
    return jsonify({
        "status": "ok",
        "message": f"Warehouse '{warehouse_name}' built (Olist star schema): {len(warehouse_tables)} table(s) loaded.",
        "tables": tables_loaded,
        "warehouse": warehouse_name,
        "mode": "olist",
    })


def _build_generic_file_warehouse(warehouse_name: str, db_path: Path, staging_dir: Path):
    logger.info(f"'{warehouse_name}': non-Olist dataset -- running generic (any-dataset) pipeline")
    raw_tables = read_folder_as_tables(staging_dir)
    if not raw_tables:
        return jsonify({"status": "error", "message": "No readable CSV/Excel files found in the staged files."}), 400

    cleaned_tables = clean_generic(raw_tables)
    relationships = infer_relationships(cleaned_tables)

    dropped = wm.clear_user_tables(db_path)
    logger.info(f"Loading into warehouse '{warehouse_name}' (cleared {dropped} existing table(s) first)")
    counts = load_generic(cleaned_tables, db_path)
    write_metadata(db_path, version="n/a (file source)", relationships=relationships, source_type="files-generic")

    return jsonify({
        "status": "ok",
        "message": f"Warehouse '{warehouse_name}' built (generic dataset): {len(counts)} table(s) loaded, "
                    f"{len(relationships)} relationship(s) inferred.",
        "tables": counts,
        "warehouse": warehouse_name,
        "mode": "generic",
    })


# ---------------------------------------------------------------------------
# PATH 2: MySQL connect (test only, doesn't build the warehouse by itself)
# ---------------------------------------------------------------------------
@app.route("/api/connect", methods=["POST"])
def connect_db():
    """Attempts a live MySQL connection, reads its version and full schema
    (tables + real FK relationships via inspector). Credentials are used
    only for this single request -- never written to disk or logged."""
    payload = request.get_json(force=True) or {}
    required = ["host", "database", "username"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return jsonify({"status": "error", "message": f"Missing field(s): {', '.join(missing)}"}), 400

    ok, error = test_connection(payload)
    if not ok:
        return jsonify({"status": "error", "message": error}), 400

    version = get_version(payload)
    tables, relationships = get_schema(payload)

    return jsonify({
        "status": "ok",
        "message": f"Connected to '{payload['database']}' (MySQL {version}). "
                    f"Found {len(tables)} table(s), {len(relationships)} relationship(s).",
        "version": version,
        "tables": tables,
        "relationships": relationships,
    })


# ---------------------------------------------------------------------------
# BUTTON 2: "Create Warehouse from Database"
# ---------------------------------------------------------------------------
@app.route("/api/create-warehouse-database", methods=["POST"])
def create_warehouse_database():
    """Pulls every table from the connected MySQL database, cleans
    generically, loads as-is into the named warehouse (default "default"),
    and writes version + FK relationships as metadata. Clears the target
    warehouse's existing tables first -- same replace semantics as the
    Files path, for the same reason."""
    payload = request.get_json(force=True) or {}
    required = ["host", "database", "username"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return jsonify({"status": "error", "message": f"Missing field(s): {', '.join(missing)}"}), 400

    warehouse_name = payload.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)

    ok, error = test_connection(payload)
    if not ok:
        return jsonify({"status": "error", "message": error}), 400

    try:
        version = get_version(payload)
        _, relationships = get_schema(payload)

        raw_tables = extract_mysql_tables(payload)
        if not raw_tables:
            return jsonify({"status": "error", "message": "Connected, but no tables found in this database."}), 400

        cleaned_tables = clean_generic(raw_tables)

        dropped = wm.clear_user_tables(db_path)
        logger.info(f"Loading into warehouse '{warehouse_name}' (cleared {dropped} existing table(s) first)")
        counts = load_generic(cleaned_tables, db_path)
        write_metadata(db_path, version=version, relationships=relationships, source_type="mysql")

        return jsonify({
            "status": "ok",
            "message": f"Warehouse '{warehouse_name}' built from '{payload['database']}' (MySQL {version}): "
                        f"{len(counts)} table(s) loaded, {len(relationships)} relationship(s) mapped.",
            "tables": counts,
            "warehouse": warehouse_name,
        })
    except Exception as e:
        logger.exception("Warehouse build (database) failed")
        return jsonify({"status": "error", "message": f"Pipeline failed: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# VISUALIZATION: read back the built warehouse's schema + relationships
# ---------------------------------------------------------------------------
@app.route("/api/warehouse-schema", methods=["GET"])
def warehouse_schema():
    warehouse_name = request.args.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)
    summary = get_warehouse_summary(db_path)
    return jsonify(summary)


# ---------------------------------------------------------------------------
# BLUEPRINT: AI-generated warehouse overview (table roles, purposes, narrative)
# ---------------------------------------------------------------------------
@app.route("/api/blueprint", methods=["GET"])
def blueprint_get():
    """Returns the saved blueprint for a warehouse, or a clear 'not found'
    signal if one hasn't been generated yet -- never auto-generates on a
    GET, since that would mean silently triggering an LLM call from what
    looks like a passive read."""
    warehouse_name = request.args.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)
    blueprint = load_blueprint(db_path)
    if blueprint is None:
        return jsonify({"status": "error", "message": "No blueprint generated yet."}), 404
    return jsonify({"status": "ok", **blueprint})


@app.route("/api/blueprint", methods=["POST"])
def blueprint_generate():
    payload = request.get_json(silent=True) or {}
    warehouse_name = payload.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)

    if not db_path.exists():
        return jsonify({"status": "error", "message": "No warehouse built yet."}), 400
    try:
        blueprint = generate_blueprint(db_path)
        return jsonify({"status": "ok", **blueprint})
    except OllamaError as e:
        return jsonify({"status": "error", "message": str(e)}), 502
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception("Blueprint generation failed")
        return jsonify({"status": "error", "message": f"Blueprint generation failed: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# DASHBOARDS: multi-dashboard suite, AI-designed (Ollama) once, then cheap
# re-query on manual refresh
# ---------------------------------------------------------------------------
@app.route("/api/dashboard/design", methods=["POST"])
def dashboard_design():
    """Triggers a full multi-dashboard design pass: schema -> propose
    themes -> design each theme -> combined insights -> centralized
    overview -> save, for the named warehouse (default "default"). Several
    Ollama calls in sequence (one per theme plus two more) -- can take
    several minutes total, not a single quick request."""
    payload = request.get_json(silent=True) or {}
    warehouse_name = payload.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)

    if not db_path.exists():
        return jsonify({"status": "error", "message": "No warehouse built yet. Build one from Files or Database first."}), 400
    try:
        result = design_dashboards(db_path)
        return jsonify({"status": "ok", **result})
    except OllamaError as e:
        return jsonify({"status": "error", "message": str(e)}), 502
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception("Dashboard design failed")
        return jsonify({"status": "error", "message": f"Dashboard design failed: {str(e)}"}), 500


@app.route("/api/dashboard/data", methods=["GET"])
def dashboard_data():
    """Re-runs every already-designed dashboard's queries -- no LLM call.
    This is the manual 'Refresh' button, for the named warehouse."""
    warehouse_name = request.args.get("warehouse", DEFAULT_WAREHOUSE)
    db_path = _resolve_warehouse(warehouse_name)

    if not db_path.exists():
        return jsonify({"status": "error", "message": "No warehouse built yet."}), 400
    try:
        result = refresh_dashboards(db_path)
        return jsonify({"status": "ok", **result})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception("Dashboard refresh failed")
        return jsonify({"status": "error", "message": f"Dashboard refresh failed: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
