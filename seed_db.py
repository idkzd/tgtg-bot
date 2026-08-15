"""Bootstrap the store cache from the captured Vienna scan (vienna_items_all.json).

Run once after a fresh clone so chain search works immediately:
    .venv/bin/python seed_db.py [path/to/vienna_items_all.json]
"""
import json
import sys
import time

from db import DB
from tgtg_client import TgtgClient


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "../vienna_items_all.json"
    with open(path) as f:
        raw = json.load(f)
    entries = []
    for e in raw:
        n = TgtgClient._normalize(e)
        if n["item_id"]:
            entries.append(n)
    db = DB()
    db.upsert_stores(entries)
    db.set_meta("last_scan_ts", str(time.time()))
    db.set_meta("last_scan_count", str(len(entries)))
    print(f"seeded {len(entries)} stores into tgtg_bot.db")


if __name__ == "__main__":
    main()
