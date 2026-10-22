"""Storage for the ATMS booking server.

Uses MongoDB Atlas when MONGODB_URI is set, otherwise the local bookings.json next to the server.
The whole state is one record:  {"bookings": [...], "pinHash": "..."}

    d = load()      # a dict you can edit
    save(d)         # write it back

With MongoDB, load() adds a hidden "_v" version number and save() refuses to overwrite if another server saved
in between (you get a StorageError, nothing is lost). With JSON files "_v" is ignored.
Call load() and save() inside the server's lock so the check-and-write stays one step.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


class StorageError(Exception):
    """A problem the user can retry (database busy, data too large, database unreachable)."""


def _running_tests():
    first = (sys.argv[0] if sys.argv else "").lower()
    return "unittest" in first or "pytest" in first or os.environ.get("ATMS_SKIP_DOTENV") == "1"


# Local convenience: read settings from a .env file next to this file (pip install python-dotenv).
# Real environment variables (Render's Environment tab) always win over the file.
if not _running_tests():
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass

MONGODB_URI = (os.environ.get("MONGODB_URI") or "").strip()
MONGODB_DB = (os.environ.get("MONGODB_DB") or "atms_booking").strip()
DATA_FILE = Path(os.environ.get("BOOKINGS_FILE") or HERE / "bookings.json")
WARN_BYTES, MAX_BYTES = 12 * 1024 * 1024, 15 * 1024 * 1024  # a MongoDB document holds at most 16 MB
DOC_ID = "state"


def _connect():
    import pymongo
    from pymongo.errors import PyMongoError
    kwargs = {"serverSelectionTimeoutMS": 8000, "appName": "atms-booking"}
    if MONGODB_URI.startswith("mongodb+srv://"):
        try:
            import certifi  # avoids "CERTIFICATE_VERIFY_FAILED" on some Windows PCs
            kwargs["tlsCAFile"] = certifi.where()
        except ImportError:
            pass
    try:
        client = pymongo.MongoClient(MONGODB_URI, **kwargs)
        client.admin.command("ping")
        col = client.get_database(MONGODB_DB)["booking_state"]
        col.update_one({"_id": DOC_ID}, {"$setOnInsert": {"bookings": [], "pinHash": None, "version": 0}}, upsert=True)
    except PyMongoError as exc:
        raise RuntimeError(
            "Cannot connect to MongoDB Atlas. Check MONGODB_URI (special characters in the password must be "
            "URL-encoded), that the database user exists, and that Atlas > Network Access allows this server. "
            f"Details: {type(exc).__name__}") from None
    print(f"Storage backend: MongoDB Atlas (database '{MONGODB_DB}')", file=sys.stderr, flush=True)
    return col


_col = _connect() if MONGODB_URI else None
if _col is None:
    print(f"Storage backend: JSON file {DATA_FILE} (set MONGODB_URI to use MongoDB Atlas)", file=sys.stderr, flush=True)


def backend():
    return "mongodb" if _col is not None else "json"


def load():
    if _col is None:
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            d = {}
        d.setdefault("bookings", [])
        d.setdefault("pinHash", None)
        return d
    from pymongo.errors import PyMongoError
    try:
        doc = _col.find_one({"_id": DOC_ID})
        if doc is None:
            _col.update_one({"_id": DOC_ID}, {"$setOnInsert": {"bookings": [], "pinHash": None, "version": 0}}, upsert=True)
            doc = {"bookings": [], "pinHash": None, "version": 0}
    except PyMongoError as exc:
        raise StorageError(f"Could not read bookings ({type(exc).__name__}). Please try again.") from None
    return {"bookings": doc.get("bookings", []), "pinHash": doc.get("pinHash"), "_v": doc.get("version", 0)}


def save(d):
    state = {"bookings": d["bookings"], "pinHash": d.get("pinHash")}
    if _col is None:
        fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)  # atomic: never leaves a half-written file
        return
    import bson
    from pymongo.errors import PyMongoError
    size = len(bson.encode({"_id": DOC_ID, **state, "version": 0}))
    if size > MAX_BYTES:
        raise StorageError("The booking history is too large to save. Archive old bookings.")
    if size > WARN_BYTES:
        print("WARNING: booking data is close to MongoDB's 16 MB document limit.", file=sys.stderr, flush=True)
    guarded = "_v" in d  # loaded earlier -> only save if nobody else saved since
    flt = {"_id": DOC_ID, "version": d["_v"]} if guarded else {"_id": DOC_ID}
    try:
        res = _col.update_one(flt, {"$set": state, "$inc": {"version": 1}}, upsert=not guarded)
    except PyMongoError as exc:
        raise StorageError(f"Could not save bookings ({type(exc).__name__}). Please try again.") from None
    if guarded and res.matched_count != 1:
        raise StorageError("Someone else saved at the same moment. Nothing was lost; please try again.")
