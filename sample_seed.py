"""Safely loads the nine sample documents into an empty development database.
Usage: set SEED_SAMPLE_DATA=YES and run sample_seed.py.

The samples show document SHAPES only. The grader uses a much larger hidden dataset with cases they do not show.
Passing on this sample does not mean your analytics are correct - re-read the business rules."""
import os
import pathlib
import sys

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(override=False)

if os.getenv("SEED_SAMPLE_DATA") != "YES":
    sys.exit("Refusing to seed without SEED_SAMPLE_DATA=YES.")

uri = os.getenv("MONGO_URI")
if not uri:
    sys.exit("MONGO_URI is required; the seeder never falls back to localhost.")

here = pathlib.Path(__file__).parent / "sample_data"
options = json_util.JSONOptions(tz_aware=True)
client = MongoClient(
    uri,
    tls=True,
    tz_aware=True,
    connectTimeoutMS=5_000,
    serverSelectionTimeoutMS=5_000,
)
try:
    client.admin.command("ping")
    database = client[os.getenv("MONGO_DB", "attendance_db")]
    collection_names = ("employees", "attendance_logs")
    counts = {name: database[name].count_documents({}) for name in collection_names}
    if any(counts.values()):
        sys.exit(
            "Refusing to seed: both employees and attendance_logs must be empty. "
            f"Current counts: {counts}"
        )
    for name in collection_names:
        documents = json_util.loads(
            (here / f"{name}.json").read_text(encoding="utf-8"),
            json_options=options,
        )
        database[name].insert_many(documents)
        print(f"loaded {len(documents):>2} documents into '{database.name}.{name}'")
finally:
    client.close()
