"""Crash-safe immutable SQLite event snapshots and cold recovery.

The Parquet codec runs in an explicitly configured, already installed Python
runtime. SQLite remains the hot truth source; successful archival never deletes
hot rows. This module does not import a codec or touch any broker.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

COLUMNS = ("id", "event_id", "event_type", "timestamp", "aggregate_id", "payload_json", "created_at")


def _digest(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(".pending")
    with temp.open("w", encoding="utf-8") as out:
        json.dump(value, out, indent=2, ensure_ascii=False)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ParquetEventArchive:
    def __init__(self, directory: str | Path, *, python_executable: str):
        self.directory = Path(directory).resolve()
        self.python = str(Path(python_executable).absolute())
        if not Path(self.python).is_file():
            raise ValueError("Configured archive Python does not exist")
        self.codec = Path(__file__).with_name("parquet_codec.py")
        self.manifest_path = self.directory / "manifest.json"

    def _codec(self, request: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            [self.python, str(self.codec)], input=json.dumps(request, ensure_ascii=False),
            text=True, capture_output=True, timeout=120, check=False,
        )
        if completed.returncode:
            raise RuntimeError("Parquet codec failed: " + completed.stderr[-2000:])
        return json.loads(completed.stdout)

    def _manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"schema_version": 1, "source_identity": None, "last_sequence": 0, "segments": []}
        result = json.loads(self.manifest_path.read_text())
        if result.get("schema_version") != 1:
            raise ValueError("Unsupported archive manifest schema")
        return result

    def snapshot(self, store: Any, *, batch_size: int = 4096) -> dict[str, Any]:
        if store.read_only:
            raise PermissionError("Read-only EventStore cannot publish archives")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".archive.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest = self._manifest()
            with store._get_connection() as connection:
                connection.row_factory = sqlite3.Row
                first = connection.execute("SELECT * FROM events ORDER BY id LIMIT 1").fetchone()
                if first is None:
                    return {"status": "EMPTY", "new_rows": 0, "last_sequence": manifest["last_sequence"]}
                identity = _digest([dict(first)])
                if manifest["source_identity"] not in (None, identity):
                    raise ValueError("Archive belongs to a different event history")
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                    (manifest["last_sequence"], batch_size),
                ).fetchall()]
            if not rows:
                return {"status": "CURRENT", "new_rows": 0, "last_sequence": manifest["last_sequence"]}
            filename = f"events-{rows[0]['id']:012d}-{rows[-1]['id']:012d}.parquet"
            target = self.directory / filename
            result = self._codec({"action": "write", "path": str(target), "rows": rows})
            if result["rows_sha256"] != _digest(rows) or result["count"] != len(rows):
                raise ValueError("Archive round-trip differed from SQLite snapshot")
            segment = {"file": filename, "first_sequence": rows[0]["id"],
                       "last_sequence": rows[-1]["id"], "count": len(rows),
                       "rows_sha256": result["rows_sha256"], "file_sha256": result["file_sha256"]}
            manifest["source_identity"] = identity
            manifest["last_sequence"] = rows[-1]["id"]
            manifest["segments"].append(segment)
            _atomic_json(self.manifest_path, manifest)
            return {"status": "ARCHIVED", "new_rows": len(rows), "last_sequence": rows[-1]["id"],
                    "segment": segment, "codec_version": result["codec_version"]}

    def read_rows(self) -> list[dict[str, Any]]:
        manifest = self._manifest()
        rows: list[dict[str, Any]] = []
        previous = 0
        identities: set[str] = set()
        for segment in manifest["segments"]:
            filename = segment["file"]
            if Path(filename).name != filename or not filename.endswith(".parquet"):
                raise ValueError("Unsafe archive segment path")
            path = self.directory / filename
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != segment["file_sha256"]:
                raise ValueError("Archive segment checksum mismatch")
            result = self._codec({"action": "read", "path": str(path)})
            batch = result["rows"]
            if (len(batch) != segment["count"] or not batch or
                    _digest(batch) != segment["rows_sha256"] or
                    batch[0]["id"] != segment["first_sequence"] or
                    batch[-1]["id"] != segment["last_sequence"]):
                raise ValueError("Archive segment content mismatch")
            for row in batch:
                if row["id"] <= previous or row["event_id"] in identities:
                    raise ValueError("Archive sequence or identity conflict")
                previous = row["id"]
                identities.add(row["event_id"])
                rows.append(row)
        if previous != manifest["last_sequence"]:
            raise ValueError("Archive watermark mismatch")
        if rows and _digest(rows[:1]) != manifest["source_identity"]:
            raise ValueError("Archive source identity mismatch")
        return rows

    def restore(self, store: Any) -> dict[str, Any]:
        if store.read_only:
            raise PermissionError("Read-only EventStore cannot restore archives")
        rows = self.read_rows()  # Validate all segments before any transaction.
        inserted = 0
        with store._get_connection() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    previous = connection.execute(
                        "SELECT * FROM events WHERE event_id = ? OR id = ?", (row["event_id"], row["id"])
                    ).fetchall()
                    if previous:
                        if len(previous) != 1 or dict(previous[0]) != row:
                            raise ValueError("Cold restore conflicts with hot event history")
                        continue
                    connection.execute(
                        "INSERT INTO events (" + ",".join(COLUMNS) + ") VALUES (" + ",".join("?" for _ in COLUMNS) + ")",
                        tuple(row[column] for column in COLUMNS),
                    )
                    inserted += 1
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"status": "RESTORED", "inserted": inserted, "archive_rows": len(rows)}
