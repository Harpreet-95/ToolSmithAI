"""
Safe, explicit migration of runtime-generated files (the sqlite metadata DB
and the audit log) into TOOLSMITH_DATA_DIR — Phase 3.2 / Task 1.

Never runs automatically. Copies (never moves/deletes) the source files, so
the original data is untouched until you've verified the app works against
the new location and remove it yourself.

Usage:
    Stop the app first — a running process can still be writing to
    toolsmith.db (WAL mode), and a copy taken mid-write is not guaranteed
    consistent even with the checkpoint step below.

    set TOOLSMITH_DATA_DIR=D:\\toolsmith-data   (or export on Linux/macOS)
    python -m data.migrate_data_dir

    Or specify the destination directly, without setting the env var first:
    python -m data.migrate_data_dir --dest D:\\toolsmith-data
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sqlite3
import sys

from data.db import DB_PATH
from data.audit import AUDIT_LOG_PATH


def _checkpoint_wal(db_path: pathlib.Path) -> None:
    """Force any pending WAL contents into the main db file and truncate the
    WAL, so a plain file copy of db_path alone is complete and consistent.
    Best-effort: if the DB is locked by a running process, this will raise —
    intentionally, so the migration stops rather than copying a possibly
    inconsistent file (see module docstring: stop the app first)."""
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _copy_one(src: pathlib.Path, dest_dir: pathlib.Path, *, force: bool) -> str:
    if not src.exists():
        return f"skip: {src} does not exist"
    dest = dest_dir / src.name
    if dest.exists() and not force:
        return f"skip: {dest} already exists (pass --force to overwrite)"
    shutil.copy2(src, dest)
    src_size, dest_size = src.stat().st_size, dest.stat().st_size
    if src_size != dest_size:
        raise RuntimeError(
            f"Copy verification failed for {src.name}: "
            f"source {src_size} bytes, destination {dest_size} bytes."
        )
    return f"copied: {src} -> {dest} ({dest_size:,} bytes, verified)"


def migrate(dest_dir: pathlib.Path, *, force: bool = False) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    results: list[str] = []

    if DB_PATH.exists():
        try:
            _checkpoint_wal(DB_PATH)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not checkpoint {DB_PATH} before copying — is the app "
                f"still running? Stop it and retry. ({exc})"
            ) from exc

    for src in (
        DB_PATH,
        DB_PATH.with_name(DB_PATH.name + "-wal"),
        DB_PATH.with_name(DB_PATH.name + "-shm"),
        AUDIT_LOG_PATH,
        *AUDIT_LOG_PATH.parent.glob(AUDIT_LOG_PATH.name + ".*.bak"),
    ):
        results.append(_copy_one(src, dest_dir, force=force))

    return results


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        help="Destination directory. Defaults to TOOLSMITH_DATA_DIR if not given.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite files already present at the destination.",
    )
    args = parser.parse_args(argv)

    if args.dest:
        dest_dir = pathlib.Path(args.dest)
    else:
        from core.config import TOOLSMITH_DATA_DIR
        if not TOOLSMITH_DATA_DIR:
            print(
                "No destination given: pass --dest <path> or set "
                "TOOLSMITH_DATA_DIR before running this script.",
                file=sys.stderr,
            )
            return 2
        dest_dir = pathlib.Path(TOOLSMITH_DATA_DIR)

    print(f"Source metadata DB: {DB_PATH}")
    print(f"Source audit log:   {AUDIT_LOG_PATH}")
    print(f"Destination:        {dest_dir}")
    print()

    try:
        results = migrate(dest_dir, force=args.force)
    except RuntimeError as exc:
        print(f"Migration aborted: {exc}", file=sys.stderr)
        return 1

    for line in results:
        print(line)

    print()
    print(
        "Originals were copied, not moved or deleted. Set TOOLSMITH_DATA_DIR="
        f"{dest_dir} in your environment/.env, restart the app, verify it "
        "reads correctly from the new location, and only then delete the "
        "original files listed above yourself."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
