#!/usr/bin/env python3
"""Start the ITSM Migration API server.

Usage:
    python server.py               # production (serves built frontend from web/dist)
    python server.py --dev         # development (expects 'npm run dev' in web/ separately)
    python server.py --host 0.0.0.0 --port 8080
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="ITSM Migration API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dev", action="store_true", help="Enable hot-reload (uvicorn --reload)")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
        workers=1 if args.dev else args.workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
