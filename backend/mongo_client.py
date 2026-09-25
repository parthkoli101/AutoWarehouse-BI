"""
mongo_client.py

Connects to a locally running MongoDB instance, the same way the app
connects to MySQL: it does NOT launch the database server itself (Python
launching a DB server process reliably across OSes/install paths is not
realistic) -- you start `mongod` once, this connects to it on app startup.

Uploaded raw files (CSV/Excel) are stored in MongoDB via GridFS, tagged
with the warehouse they belong to. This is also what fixes the
"files mixing between warehouses" bug: each warehouse's files are stored
and retrieved separately, instead of all uploads landing in one shared
local folder regardless of which warehouse they were meant for.

If MongoDB isn't reachable, the app does NOT hard-fail -- uploads and
builds fall back to a warehouse-scoped local folder instead (still fixes
the mixing bug even without Mongo running), and a clear status is exposed
via is_connected() so the frontend can show the real state honestly.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("mongo_client")

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "warehouse_console"
BUCKET_NAME = "raw_files"
CONNECT_TIMEOUT_MS = 3000  # fail fast, don't hang the app waiting for a DB that isn't running

_client = None
_db = None
_bucket = None
_connection_error = None


def _try_connect():
    """Attempts the connection once, caching the result. Called lazily so
    importing this module never blocks or crashes even if Mongo is down."""
    global _client, _db, _bucket, _connection_error
    if _client is not None or _connection_error is not None:
        return
    try:
        import pymongo
        import gridfs
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=CONNECT_TIMEOUT_MS)
        client.admin.command("ping")  # forces an actual round-trip, not just object construction
        _client = client
        _db = client[DB_NAME]
        _bucket = gridfs.GridFSBucket(_db, bucket_name=BUCKET_NAME)
        logger.info("Connected to MongoDB at %s", MONGO_URI)
    except Exception as e:
        _connection_error = str(e)
        logger.warning("MongoDB not reachable (%s) -- falling back to local disk storage.", e)


def is_connected() -> bool:
    _try_connect()
    return _client is not None


def connection_status() -> dict:
    _try_connect()
    return {"connected": _client is not None, "error": _connection_error if _client is None else None}


def save_file(warehouse: str, filename: str, file_bytes: bytes) -> bool:
    """Stores a file's bytes in GridFS tagged with its warehouse. Replaces
    any existing file with the same warehouse+filename (re-uploading the
    same file overwrites it, rather than accumulating duplicates)."""
    if not is_connected():
        return False
    # delete any prior version of this exact file for this warehouse
    for doc in _bucket.find({"metadata.warehouse": warehouse, "metadata.filename": filename}):
        _bucket.delete(doc._id)
    _bucket.upload_from_stream(
        filename,
        file_bytes,
        metadata={"warehouse": warehouse, "filename": filename, "uploaded_at": datetime.now(timezone.utc).isoformat()},
    )
    return True


def list_files(warehouse: str) -> list:
    if not is_connected():
        return []
    docs = _bucket.find({"metadata.warehouse": warehouse})
    return [{"filename": d.metadata["filename"], "size": d.length, "uploaded_at": d.metadata.get("uploaded_at")} for d in docs]


def get_file_bytes(warehouse: str, filename: str) -> bytes:
    if not is_connected():
        return None
    doc = _db[f"{BUCKET_NAME}.files"].find_one({"metadata.warehouse": warehouse, "metadata.filename": filename})
    if not doc:
        return None
    stream = _bucket.open_download_stream(doc["_id"])
    return stream.read()


def delete_files_for_warehouse(warehouse: str) -> int:
    """Removes every file stored for a warehouse -- used when the
    warehouse itself is deleted, so orphaned files don't accumulate."""
    if not is_connected():
        return 0
    count = 0
    for doc in _bucket.find({"metadata.warehouse": warehouse}):
        _bucket.delete(doc._id)
        count += 1
    return count
