#!/usr/bin/env python3
"""
migrate_to_sqlite.py — one-time migration from the old JSON store to SQLite.

Reads the legacy files:
    zotero_database.json          (list of Paper dicts)
    state/processed_files.json    (filename → ProcessedFile bookkeeping)

and writes them into the new SQLite database at config.DB_PATH
(zotero_database.db) via the same Database class the app uses, so the on-disk
shape is exactly what the running app expects.

Safety:
  - Refuses to overwrite an existing local .db, and refuses to write to any
    destination (local OR Turso) that already contains papers — so re-running it
    can't clobber a populated database. Move the .db aside, or migrate into a
    fresh empty Turso DB.
  - Never deletes or modifies the old .json files — they stay as a backup.
  - After writing, reloads the database and asserts the paper/processed counts
    and the full stats() dict match what was read from JSON, so a silent data
    loss fails loudly instead.

Run from the project root:
    python migrate_to_sqlite.py

To migrate into Turso instead of a local file, set TURSO_DATABASE_URL and
TURSO_AUTH_TOKEN in the environment first (database._connect routes there
automatically); create the Turso DB empty beforehand.
"""

from __future__ import annotations

import json
import os
import sys

import config
from database import Database, ProcessedFile, normalize_url


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    db_path = str(config.DB_PATH)
    json_path = str(config.PROJECT_ROOT / "zotero_database.json")
    processed_path = str(config.PROCESSED_FILES_PATH)
    to_turso = bool(os.environ.get("TURSO_DATABASE_URL")
                    and os.environ.get("TURSO_AUTH_TOKEN"))

    # For a local file, an existing .db is a hard stop. (For Turso the local file
    # is irrelevant; the populated-destination check below guards it instead.)
    if not to_turso and os.path.exists(db_path):
        print(f"✗ Refusing to overwrite existing database: {db_path}")
        print("  Move it aside first if you really want to re-migrate.")
        return 1

    if not os.path.exists(json_path):
        print(f"✗ No legacy JSON database found at: {json_path}")
        return 1

    # Never write into a destination that already has data (covers Turso, where
    # the os.path check above doesn't apply).
    existing = Database.load(db_path, processed_path)
    if existing.all_papers():
        dest = "Turso database" if to_turso else db_path
        print(f"✗ Refusing to migrate: destination {dest} already has "
              f"{len(existing.all_papers())} paper(s).")
        return 1
    if to_turso:
        print("Targeting Turso (TURSO_DATABASE_URL is set).")

    papers = _read_json(json_path, default=[])
    processed = _read_json(processed_path, default={})
    print(f"Read {len(papers)} papers and {len(processed)} processed-file "
          f"records from JSON.")

    # Build the in-memory model exactly as Database.load would, then save() it
    # out through the new SQLite path.
    db = Database(db_path, processed_path)
    for p in papers:
        db._papers[normalize_url(p["url"])] = p
    db._processed = {fn: ProcessedFile(**rec) for fn, rec in processed.items()}
    src_stats = db.stats()
    db.save()
    print(f"Wrote SQLite database: {db_path}")

    # Reload from disk and verify nothing was lost in the round trip.
    reloaded = Database.load(db_path, processed_path)
    dst_stats = reloaded.stats()

    ok = True
    if len(reloaded.all_papers()) != len(db._papers):
        print(f"✗ Paper count mismatch: {len(db._papers)} → "
              f"{len(reloaded.all_papers())}")
        ok = False
    if src_stats != dst_stats:
        print("✗ stats() mismatch between source and reloaded database:")
        for k in sorted(set(src_stats) | set(dst_stats)):
            if src_stats.get(k) != dst_stats.get(k):
                print(f"    {k}: {src_stats.get(k)} → {dst_stats.get(k)}")
        ok = False

    if not ok:
        print("\nMigration produced a mismatch — the .db was written but does "
              "NOT match the JSON. Inspect before trusting it.")
        return 1

    print("\n✓ Verified: paper count and full stats() match the JSON source.")
    print("  The old .json files were left untouched as a backup.")
    print(f"  stats: {dst_stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
