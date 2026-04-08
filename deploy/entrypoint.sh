#!/bin/bash
# Entrypoint: sync data from S3 on startup, sync back on graceful shutdown.
set -euo pipefail

S3_BUCKET="${S3_DATA_BUCKET:-}"
S3_PREFIX="${S3_DATA_PREFIX:-data}"

# ── Startup: restore persisted data from S3 ──────────────────────────────────
if [[ -n "$S3_BUCKET" ]]; then
    echo "[entrypoint] Restoring data from s3://${S3_BUCKET}/${S3_PREFIX}/ ..."
    aws s3 sync "s3://${S3_BUCKET}/${S3_PREFIX}/" /app/data/ \
        --exclude "mappings/*" \
        --no-progress \
        || echo "[entrypoint] WARNING: S3 restore failed (first run?), continuing with empty data."
else
    echo "[entrypoint] S3_DATA_BUCKET not set — skipping data restore."
fi

# ── Shutdown: push data back to S3 on SIGTERM/SIGINT ─────────────────────────
_shutdown() {
    echo "[entrypoint] Caught shutdown signal — syncing data to S3 ..."
    if [[ -n "$S3_BUCKET" ]]; then
        aws s3 sync /app/data/ "s3://${S3_BUCKET}/${S3_PREFIX}/" \
            --exclude "mappings/*" \
            --no-progress \
            || echo "[entrypoint] WARNING: S3 sync on shutdown failed."
    fi
    echo "[entrypoint] Shutdown complete."
    exit 0
}
trap _shutdown SIGTERM SIGINT

# ── Start the application ─────────────────────────────────────────────────────
echo "[entrypoint] Starting ITSM Migration API on port 8000 ..."
exec python server.py --host 0.0.0.0 --port 8000 &

# Wait for python process and forward signals
PYTHON_PID=$!
wait "$PYTHON_PID"
