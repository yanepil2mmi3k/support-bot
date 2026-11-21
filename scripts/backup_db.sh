#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${DATABASE_PATH:-$ROOT/data/support.db}"
if [[ "$DB_PATH" != /* ]]; then
  DB_PATH="$ROOT/$DB_PATH"
fi
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
mkdir -p "$BACKUP_DIR"
DESTINATION="$BACKUP_DIR/support-$(date +%Y%m%d-%H%M%S).db"

"$PYTHON_BIN" - "$DB_PATH" "$DESTINATION" <<'PY'
import sqlite3
import sys
from pathlib import Path

source_path, destination_path = map(Path, sys.argv[1:3])
if not source_path.is_file():
    raise SystemExit(f"База данных не найдена: {source_path}")
source = sqlite3.connect(source_path)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
print(destination_path)
PY
