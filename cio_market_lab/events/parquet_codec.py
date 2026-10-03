"""Isolated Parquet codec; stdin/stdout JSON only, no network or credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

COLUMNS = ("id", "event_id", "event_type", "timestamp", "aggregate_id", "payload_json", "created_at")
SCHEMA = pa.schema([pa.field("id", pa.int64(), nullable=False)] +
                   [pa.field(name, pa.string(), nullable=False) for name in COLUMNS[1:]])


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read(path):
    table = pq.read_table(path)
    if table.schema.remove_metadata() != SCHEMA:
        raise ValueError("Parquet event schema mismatch")
    return table.to_pylist()


def main():
    request = json.load(sys.stdin)
    path = Path(request["path"])
    action = request["action"]
    if action == "write":
        rows = request["rows"]
        if not rows or any(set(row) != set(COLUMNS) for row in rows):
            raise ValueError("Empty or invalid event snapshot")
        table = pa.Table.from_pylist(rows, schema=SCHEMA)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, pending_name = tempfile.mkstemp(prefix=".parquet-pending-", dir=path.parent)
        os.close(fd)
        pending = Path(pending_name)
        try:
            pq.write_table(table, pending, compression="zstd", version="2.6")
            with pending.open("rb") as stream:
                os.fsync(stream.fileno())
            actual = read(pending)
            if actual != rows:
                raise ValueError("Parquet write changed event rows")
            try:
                os.link(pending, path)  # Atomic no-clobber publication.
            except FileExistsError:
                if read(path) != rows:
                    raise ValueError("Immutable Parquet segment collision")
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            pending.unlink(missing_ok=True)
    elif action == "read":
        rows = read(path)
    else:
        raise ValueError("Unknown codec action")
    response = {"count": len(rows), "rows_sha256": digest(rows),
                "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "codec_version": pa.__version__}
    if action == "read":
        response["rows"] = rows
    json.dump(response, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
