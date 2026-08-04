#!/usr/bin/env python3
"""Start the ITSM Migration API server.

Usage:
    python server.py               # production (serves built frontend from web/dist)
    python server.py --dev         # development (expects 'npm run dev' in web/ separately)
    python server.py --host 0.0.0.0 --port 8080
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Watching the whole project makes the reloader thrash, because OneDrive sync
# touches .venv and the runtime writes under data/ and logs/.
RELOAD_DIRS = [
    "agents",
    "api",
    "common",
    "config",
    "controllers",
    "helpers",
    "integrations",
    "services",
    "tools",
]


def main():
    parser = argparse.ArgumentParser(description="ITSM Migration API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dev", action="store_true", help="Enable hot-reload (uvicorn --reload)")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    import uvicorn

    reload_dirs = [str(ROOT / d) for d in RELOAD_DIRS if (ROOT / d).is_dir()]

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
        reload_dirs=reload_dirs if args.dev else None,
        workers=1 if args.dev else args.workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
