# ITSM Data Migration — ServiceNow → Salesforce

Migrate ITSM records from **ServiceNow** to **Salesforce** via a guided web UI or a CLI.

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root:

```ini
# ServiceNow
SERVICE_NOW_BASE_URL=https://myinstance.service-now.com
SERVICE_NOW_USERNAME=admin
SERVICE_NOW_PASSWORD=secret

# Salesforce
SALESFORCE_BASE_URL=https://myorg.salesforce.com
SALESFORCE_CLIENT_ID=...
SALESFORCE_CLIENT_SECRET=...
SALESFORCE_API_VERSION=v60.0
```

### 3. Start the API server

```bash
python server.py
# → API available at http://localhost:8000
# → OpenAPI docs at http://localhost:8000/docs
```

### 4. Start the frontend (development)

```bash
cd web
npm install
npm run dev
# → UI available at http://localhost:5173
```

### 5. Build for production

```bash
cd web && npm run build
# Compiled assets land in web/dist/
# FastAPI serves the SPA from web/dist automatically
python server.py  # serves both API + UI on port 8000
```

---

## Web UI Features

| Feature | Description |
|---------|-------------|
| **Dashboard** | Overview of all migration sessions with live status |
| **New Migration Wizard** | 4-step wizard: source → destination → objects → options |
| **Object Selection** | Checkbox tree grouped by category; select all / deselect all per category |
| **Credential Validation** | "Test Connection" button validates credentials before creating a session |
| **Progress View** | Real-time progress bars (overall + per phase: extraction / transformation / loading) |
| **Start / Pause / Resume / Stop** | Full lifecycle control of a running migration |
| **Live Activity Log** | Streaming log messages via Server-Sent Events |
| **History** | List of all completed / failed / stopped sessions |

---

## CLI (existing)

```bash
python app.py --op extract-tables
python app.py --op extract-all-tables
python app.py --op extract-attachments
python app.py --op transform-load
python app.py --op transform-load-attachments
```

---

## Project Structure

```
itsm-data-migration-snow-sf/
├── api/                         # FastAPI application
│   ├── main.py                  # App bootstrap, CORS, static file serving
│   ├── migration_manager.py     # Session lifecycle (start/pause/resume/stop)
│   ├── cancellation_token.py    # Thread-safe cancel/pause signal
│   ├── session_db.py            # migration_sessions table helpers
│   ├── routers/
│   │   ├── sources.py           # GET /sources, POST /validate-credentials, …
│   │   └── migrations.py        # POST /migrations, SSE /progress, …
│   └── sources/
│       ├── base.py              # SourceProvider abstract class
│       └── servicenow.py        # ServiceNow implementation
│
├── web/                         # React + TypeScript frontend (Vite)
│   └── src/
│       ├── api/client.ts        # Typed API client
│       ├── components/          # ProgressBar, ObjectSelector, MigrationControls, …
│       ├── pages/               # Dashboard, NewMigrationWizard, MigrationDetail, History
│       └── hooks/               # useMigrationProgress (SSE), useSourceObjects
│
├── controllers/                 # Existing ETL controllers (unchanged)
├── services/                    # Existing service layer (unchanged)
├── config/settings.py           # Configuration constants
├── app.py                       # CLI entry point (unchanged)
└── server.py                    # uvicorn launcher for the web API
```

---

## Adding a New Migration Source

To add Jira, Remedy, or any other source:

1. Create `api/sources/jira.py` implementing `SourceProvider` (see `api/sources/base.py`)
2. Register it in `api/sources/__init__.py`:
   ```python
   from api.sources.jira import JiraProvider
   register(JiraProvider())
   ```
3. The UI **automatically** shows the new source in the wizard — no frontend changes needed.

---

## API Reference

Interactive docs available at `http://localhost:8000/docs` when the server is running.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sources/` | List source providers |
| GET | `/api/sources/{id}/objects` | List migratable objects |
| POST | `/api/sources/{id}/validate-credentials` | Test connection |
| POST | `/api/migrations/` | Create migration session |
| GET | `/api/migrations/` | List sessions |
| GET | `/api/migrations/{id}` | Get session details |
| POST | `/api/migrations/{id}/start` | Start migration |
| POST | `/api/migrations/{id}/pause` | Pause between batches |
| POST | `/api/migrations/{id}/resume` | Resume |
| POST | `/api/migrations/{id}/stop` | Cancel |
| GET | `/api/migrations/{id}/progress` | SSE stream of progress events |
