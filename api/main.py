"""FastAPI application entry point for the ITSM Migration web UI."""
import dotenv

dotenv.load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from api.routers import (
    agents,
    compat,
    connections,
    mappings,
    migrations,
    schema,
    sources,
    transforms,
    workflow,
)
from api import sources as source_registry

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
app.include_router(connections.router, prefix="/api")
app.include_router(compat.router, prefix="/api")
app.include_router(schema.router, prefix="/api")
app.include_router(mappings.router, prefix="/api")
app.include_router(transforms.router, prefix="/api")
app.include_router(workflow.router, prefix="/api")
app.include_router(agents.router, prefix="/api")

# Plugin-contributed routers (e.g. Salesforce destination + custom-field endpoints).
# Each registered source/destination plugin may expose its own FastAPI routers.
for _provider in source_registry.get_all():
    for _router in _provider.get_routers():
        app.include_router(_router, prefix="/api")
for _destination in source_registry.get_all_destinations():
    for _router in _destination.get_routers():
        app.include_router(_router, prefix="/api")


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
        # The shell names a content-hashed bundle, so a cached copy pins the tab
        # to a build whose asset a rebuild has already deleted. The assets stay
        # cacheable -- their names change -- but the page that points at them
        # has to be re-read every time.
        return FileResponse(
            str(index),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )
