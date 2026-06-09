"""FastAPI application entry point for the ITSM Migration web UI."""
import dotenv

dotenv.load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from api.routers import sources, migrations, destinations, connections, compat, custom_fields

app = FastAPI(
    title="ITSM Data Migration API",
    description="REST API for migrating ITSM data from ServiceNow to Salesforce",
    version="1.0.0",
)

# Allow the Vite dev server (port 5173) and production builds
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sources.router, prefix="/api")
app.include_router(migrations.router, prefix="/api")
app.include_router(destinations.router, prefix="/api")
app.include_router(connections.router, prefix="/api")
app.include_router(compat.router, prefix="/api")
app.include_router(custom_fields.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the compiled React frontend from web/dist if it exists
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        index = _DIST / "index.html"
        return FileResponse(str(index))
