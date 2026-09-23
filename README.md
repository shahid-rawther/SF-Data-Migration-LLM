# ITSM Data Migration — ServiceNow / Jira Service Management → Salesforce

Migrate ITSM records into **Salesforce** via a guided web UI or a CLI. The pipeline is staged —
every selected table is extracted to local storage first, then transformed and loaded — and a
selectively agentic layer on top of it resolves relationship edge cases and repairs load errors.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full architecture.

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy [`.env.example`](.env.example) to `.env` and fill it in. It documents every setting with the
reasoning behind the default. The three groups that matter:

```ini
# Source and destination credentials (also configurable per-connection in the UI)
SERVICE_NOW_BASE_URL=https://myinstance.service-now.com
SERVICE_NOW_USERNAME=admin
SERVICE_NOW_PASSWORD=secret
SALESFORCE_BASE_URL=https://myorg.salesforce.com
SALESFORCE_CLIENT_ID=...
SALESFORCE_CLIENT_SECRET=...
SALESFORCE_API_VERSION=v60.0

# Agent layer (Amazon Bedrock). Omit entirely to run the pipeline deterministically.
BEDROCK_PROVIDER=boto3
BEDROCK_REGION=us-east-1
AGENT_AUTONOMY=apply          # or `preview` to analyse without writing mapping configs

# Resumable sessions. Omit to run without checkpoints.
MIGRATION_S3_BUCKET=
MIGRATION_S3_REGION=us-east-1
```

> `python-dotenv` reads plain `KEY=value` lines. A `set ` or `export ` prefix makes the line
> unparseable and the variable is silently dropped.

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
| **Dashboard** | All migration sessions with live status, plus reminders for deferred references that have become fillable |
| **New Migration Wizard** | 6 steps: source → destination → objects → map fields → compatibility → review and run |
| **Object Selection** | Source tables and Salesforce objects, with a tree of everything the chosen objects depend on and a drawer describing any object's fields |
| **Field Mapping** | Per-table mapping editor with model-assisted field matching, transforms, joins and filters |
| **Compatibility + Preflight** | Type and coverage report, execution order, inline custom-field creation, and the agent launch gate |
| **Progress View** | Real-time progress bars per phase (preparation / extraction / transformation / loading / repair) over SSE |
| **Agent Notices** | What the agent wired, with per-item revert, and the concerns it raised with a suggested next step |
| **Backfill Panel** | References that loaded empty, with per-item approval and a second pass to fill them |
| **Session Objects** | Add objects to an existing session and run only the outstanding work |
| **Start / Pause / Resume / Stop / Retry** | Full lifecycle control, plus resume from an S3 checkpoint |
| **History** | Completed / failed / stopped sessions with a per-object status report |

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
│   ├── migration_manager.py     # Run phases, progress events, agent + preflight hooks
│   ├── compat_engine.py         # Compatibility report and deterministic dependency order
│   ├── cancellation_token.py    # Thread-safe cancel/pause signal
│   ├── session_db.py            # migration_sessions helpers, incremental selection
│   ├── routers/                 # migrations, mappings, schema, compat, transforms,
│   │                            # agents, connections, sources
│   └── sources/                 # SourceProvider base + per-source implementations
│
├── agents/                      # Selectively agentic layer — never touches record data
│   ├── bedrock_client.py        # Two transports, tool loop, token budgets
│   ├── config.py                # Env-driven settings, autonomy levels
│   ├── context.py               # Cached schema + draft mapping context
│   ├── detector.py              # Deterministic finding vocabulary
│   ├── relationship_agent.py    # Resolve edge cases, wire them, assign load order
│   ├── repair_agent.py          # Patch a field transform from load errors
│   ├── resolutions.py           # Validate model submissions before wiring
│   ├── wiring.py                # The only path that mutates a mapping config
│   ├── preflight.py             # Deterministic launch gate
│   ├── audit.py                 # Change journal and per-item revert
│   ├── summary.py               # Human-readable "what the agent just did"
│   ├── field_matcher.py         # Semantic field pairing — proposals only
│   └── order_advisor.py         # Advisory migration order for the hierarchy view
│
├── services/
│   ├── internal/                # backfill, checkpoint, hierarchy, staging,
│   │                            # session_status, transformation, workspace
│   └── external/                # db_client, s3_client
│
├── web/                         # React + TypeScript frontend (Vite)
│   └── src/
│       ├── api/client.ts        # Typed API client
│       ├── components/          # FieldMapper, AgentNoticesPanel, BackfillPanel,
│       │                        # SessionObjectsPanel, DestinationHierarchy, …
│       ├── pages/               # Dashboard, NewMigrationWizard, MappingEditor,
│       │                        # MigrationDetail, History
│       └── hooks/               # useMigrationProgress (SSE), useSourceObjects
│
├── data/
│   ├── downloads/               # Staged source extract — the staging boundary
│   ├── staging/                 # Extract manifests
│   ├── transformations/         # Transform output, incl. deferred.csv per batch
│   ├── results/                 # Per-batch load results
│   └── database/sql/schema/     # 001–009 DDL
│
├── controllers/                 # ETL controllers
├── integrations/                # ServiceNow, JSM, Salesforce adapters + plugin routers
├── config/settings.py           # Configuration constants
├── docs/                        # ARCHITECTURE.md + narrative HTML/PDF/DOCX
├── app.py                       # CLI entry point
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

Interactive docs available at `http://localhost:8000/docs` when the server is running. The full
surface is catalogued in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#13-api-surface); the endpoints
you are most likely to reach for:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sources/` | List source providers |
| GET | `/api/sources/{id}/objects` | List migratable objects |
| POST | `/api/sources/{id}/validate-credentials` | Test connection |
| POST | `/api/migrations/` | Create migration session |
| GET | `/api/migrations/` | List sessions |
| GET | `/api/migrations/{id}` | Get session details |
| POST | `/api/migrations/{id}/start` | Start migration |
| POST | `/api/migrations/{id}/pause` \| `/resume` \| `/stop` \| `/retry` | Lifecycle control |
| GET | `/api/migrations/{id}/progress` | SSE stream of progress events |
| GET | `/api/migrations/{id}/status` | Per-object status report |
| POST | `/api/migrations/{id}/objects` | Add objects to an existing session |
| GET | `/api/migrations/{id}/run-plan` | What a run would execute vs skip |
| POST | `/api/migrations/{id}/run-remaining` | Run only outstanding objects |
| POST | `/api/migrations/{id}/resume-from-checkpoint` | Restore from S3 and continue |
| GET | `/api/migrations/backfill` | Deferred references awaiting a second pass |
| POST | `/api/migrations/backfill/{id}/decision` \| `/run` | Approve or skip; execute |
| POST | `/api/mapping-agent/analyze-relationships` | Resolve and wire relationship edge cases |
| POST | `/api/mapping-agent/preflight` | Run the launch gate without starting anything |
| GET | `/api/mapping-agent/changes` | What the agent applied and what it flagged |
| POST | `/api/mapping-agent/changes/{id}/revert` | Revert one agent change |
