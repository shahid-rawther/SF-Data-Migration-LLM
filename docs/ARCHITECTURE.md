# ITSM Migration Platform — System Architecture

**Audience:** Technical director  
**Purpose:** Full architecture for staged ServiceNow/Jira → Salesforce migration with Bedrock agents  
**Data strategy:** Fetch **all** selected source data and persist it **on our side** first; transform and load run against our staged store — **not** on-the-fly streaming from source to Salesforce.

---

## 1. Goals

1. Human experts own **field mapping** (which source fields → which Salesforce fields).  
2. Agents own **relationship edge cases** and **load-error repair** (detect, notify, propose/apply solutions).  
3. Migration is **staged**: Extract → Our storage → Transform → Load.  
4. Runtime remaps (sys_id → Salesforce Id) stay **deterministic**; agents configure and guard them.

---

## 2. High-level system context

```mermaid
flowchart LR
  subgraph sources [Source systems]
    SN[ServiceNow]
    JSM[Jira Service Management]
  end

  subgraph platform [Migration platform - our side]
    API[FastAPI API]
    UI[Web Wizard UI]
    Agents[Bedrock Agents]
    Stage[(Staged data store)]
    Maps[(mapping_configs + id_mappings)]
    Jobs[(jobs / batches)]
  end

  subgraph dest [Destination]
    SF[Salesforce]
  end

  subgraph aws [AWS]
    BR[Amazon Bedrock]
  end

  UI --> API
  SN -->|Extract all selected tables| API
  JSM -->|Extract all selected tables| API
  API --> Stage
  API --> Maps
  API --> Jobs
  Agents --> BR
  API --> Agents
  API -->|Transform from stage + Load| SF
```

---

## 3. Staged data pipeline (core design)

**Principle:** Never migrate “on the fly” (row from ServiceNow straight into Salesforce). Always land a full extract locally first.

```mermaid
flowchart TD
  A[1. Configure connections + select tables] --> B[2. Human field mapping]
  B --> C[3. Relationship Agent analyze]
  C --> D[4. Expert review notices + accept solutions]
  D --> E[5. EXTRACT - pull all data from source]
  E --> F[(Persist raw/staged files + job metadata)]
  F --> G[6. TRANSFORM - from staged data only]
  G --> H[7. LOAD to Salesforce parent-first]
  H --> I{SF errors?}
  I -->|Yes| J[Repair Agent - patch transform]
  J --> G
  I -->|No / resolved| K[Complete + id_mappings retained]
```

### Why staged (not on-the-fly)

| Concern | Staged approach |
|---------|-----------------|
| Replay / retry | Re-transform + reload without re-hitting source |
| Audit | Exact payload that was migrated is retained |
| Agent repair | Repair Agent rewrites transforms against local stage |
| Source load | One extract window; less long-lived source API pressure |
| Consistency | Snapshot of related tables extracted together |

### Staging layout (logical)

```text
data/
  downloads/          # raw extracts per job / table
  transformations/    # transformed payloads ready for SF
  database/           # SQLite: jobs, batches, mapping_configs, id_mappings
```

Extract writes into `downloads/`; transform reads only from staged extracts; load reads transformed output. Agents never call Salesforce insert APIs directly — only through the existing loader.

---

## 4. Component architecture

```mermaid
flowchart TB
  subgraph presentation [Presentation]
    Wizard[New Migration Wizard]
    Progress[Job progress + repair log]
    Notices[Relationship Notices panel]
  end

  subgraph api_layer [API layer - FastAPI]
    MigRouter[migrations router]
    MapRouter[mappings / schema / compat]
    AgentRouter[mapping-agent + repair-agent routers]
  end

  subgraph agents [Agent layer]
    RelAgent[Relationship Agent]
    RepairAgent[Repair Agent]
    BedrockClient[Bedrock Converse client]
  end

  subgraph domain [Deterministic domain]
    Compat[compat_engine]
    Suggest[auto_suggest_mapping]
    Deps[resolve_dependency_order]
    MM[migration_manager]
    Extract[extraction_controller]
    Transform[transformation_service]
    Load[loading_controller]
  end

  subgraph data [Data plane]
    StageFiles[(Staged extract / transform files)]
    DB[(SQLite - jobs, mappings, id_mappings)]
  end

  Wizard --> MapRouter
  Wizard --> Notices
  Wizard --> MigRouter
  Progress --> MigRouter
  AgentRouter --> RelAgent
  AgentRouter --> RepairAgent
  RelAgent --> BedrockClient
  RepairAgent --> BedrockClient
  RelAgent --> Compat
  RelAgent --> Deps
  MigRouter --> MM
  MM --> Extract
  MM --> Transform
  MM --> Load
  Extract --> StageFiles
  Transform --> StageFiles
  Load --> DB
  Transform --> DB
```

---

## 5. Agent architecture

### 5.1 What humans vs agents own

```mermaid
flowchart LR
  Human[Human expert] -->|selects scalar field maps| Config[Mapping configs]
  RelAgent[Relationship Agent] -->|detects edge cases + solutions| Config
  RelAgent -->|notifies| Human
  Config --> Preflight[Preflight gates]
  Preflight --> ETL[Staged ETL]
  ETL -->|SF errors| RepairAgent[Repair Agent]
  RepairAgent -->|patch transforms + retry| ETL
  RepairAgent -->|notify| Human
```

| Owner | Responsibility |
|-------|----------------|
| Human | Field-to-field mapping choices |
| Relationship Agent | Scan object graph; identify edge cases; notify; propose concrete fixes (lookup, parents, order, block raw Id) |
| Deterministic ETL | Extract to stage, transform, load, store `id_mappings` |
| Repair Agent | Interpret SF errors; allow-listed transform fixes; retry from stage |

### 5.2 Relationship Agent — edge-case catalog

The agent does **not** assume only “parent then child.” It classifies and solves:

1. Parent → child Id remap (new SF Ids)  
2. Multi-level chains (Company → Dept → User → Incident)  
3. Multiple FKs on one child  
4. Missing parent in migration set  
5. Junction / M2M (two parents before junction)  
6. Self-reference / cycles  
7. Constant FK vs migrated parent  
8. Attachments linked to parent  
9. Upsert / pre-existing destination records  
10. Raw sys_id on SF reference (block)  
11. Wrong `id_mappings` lookup table name  

**Output:** Relationship Notices + proposed mapping patches + dependency order.

### 5.3 Repair Agent — runtime loop

```mermaid
sequenceDiagram
  participant Load as Loader
  participant SF as Salesforce
  participant Repair as RepairAgent
  participant BR as Bedrock
  participant Stage as Staged data
  participant T as Transform

  Load->>SF: Insert batch from transformed files
  SF-->>Load: failedResults summary
  Load->>Repair: error digest field + code + sample
  Repair->>BR: Classify + propose allow-listed fix
  BR-->>Repair: transform patch
  Repair->>T: Update mapping + re-transform from Stage
  T->>Load: Retry failed object only
  Note over Repair: Max N retries then escalate to human
```

---

## 6. End-to-end runtime sequence (staged)

```mermaid
sequenceDiagram
  participant U as Expert
  participant UI as Wizard
  participant API as API
  participant RA as RelationshipAgent
  participant Stage as Stage store
  participant Src as Source system
  participant MM as MigrationManager
  subgraph etl [ETL]
    Ex as Extract
    Tr as Transform
    Ld as Load
  end
  participant SF as Salesforce

  U->>UI: Map fields
  UI->>API: analyze-relationships
  API->>RA: configs + schemas
  RA-->>UI: notices + solutions
  U->>UI: Accept solutions / launch
  UI->>API: create migration job
  API->>MM: start job
  MM->>Ex: extract all selected tables
  Ex->>Src: query tables
  Ex->>Stage: persist full extracts
  loop Parent-first groups
    MM->>Tr: transform from Stage
    Tr->>Stage: write transformed files
    MM->>Ld: load to SF
    Ld->>SF: insert
    Ld->>API: write id_mappings
  end
```

---

## 7. Data model (our side)

```mermaid
erDiagram
  JOBS ||--o{ JOB_TABLES : contains
  JOB_TABLES ||--o{ JOB_TABLE_BATCHES : contains
  MAPPING_CONFIGS ||--o{ JOB_TABLES : used_by
  ID_MAPPINGS }o--|| MAPPING_CONFIGS : "keyed by table name"

  JOBS {
    int id
    string status
    json selected_objects
  }
  JOB_TABLES {
    int id
    int job_id
    string mapping_name
    string status
  }
  MAPPING_CONFIGS {
    string name
    string source_type
    json config
    int priority
  }
  ID_MAPPINGS {
    string table_name
    string source_sys_id
    string salesforce_id
  }
```

**Id remap at load time (deterministic):**

```text
Load ObjectSFA → insert rows → for each success:
  id_mappings[source_sys_id] = new_salesforce_id

Transform ObjectSFB FK field:
  generic_lookup(source_sys_id) → salesforce_id from id_mappings
```

---

## 8. Deployment / trust boundaries

```mermaid
flowchart TB
  subgraph browser [Browser]
    SPA[React SPA]
  end

  subgraph app_host [App host - our VPC or server]
    API[FastAPI]
    Files[Staged files]
    DB[(SQLite or managed DB)]
  end

  subgraph aws [AWS account]
    BR[Bedrock Runtime]
  end

  subgraph external [Customer systems]
    SN[ServiceNow / Jira]
    SF[Salesforce]
  end

  SPA -->|HTTPS API only| API
  API -->|Extract| SN
  API -->|Load| SF
  API -->|Converse tools - metadata only| BR
  API --> Files
  API --> DB
```

**Security rules**

- Credentials never sent to Bedrock  
- Bedrock receives schema metadata, mapping drafts, error digests — not full extract dumps  
- Agents have no direct SF write tools  
- Staged data stays on our side under job isolation  

---

## 9. Technology map

| Layer | Technology |
|-------|------------|
| UI | React (wizard, notices, progress) |
| API | FastAPI |
| Agents | Amazon Bedrock Converse + tool use (Claude) |
| Extract / Transform / Load | Existing controllers + `migration_manager` |
| Stage | Local/files + DB (`data/downloads`, `data/transformations`) |
| Mapping / Id maps | SQLite `mapping_configs`, `id_mappings` |
| Compatibility / order | `compat_engine` |

---

## 10. API surface (agent + migration)

| Endpoint / hook | Role |
|-----------------|------|
| `POST /api/mapping-agent/analyze-relationships` | Notices + proposed solutions |
| `POST /api/mapping-agent/plan` | Optional fuller plan |
| Preflight in `migration_manager` | Block raw references / missing parents |
| `POST /api/migrations/` | Start staged job |
| Extract phase | Persist all tables to stage |
| Post-load failure hook | Repair Agent |
| `POST /api/mapping-agent/repair` | Apply patch + retry object from stage |

---

## 11. Phased delivery

| Phase | Deliverable |
|-------|-------------|
| 0 | Bedrock access + IAM |
| 1 | Relationship Agent: detect + notify + propose solutions |
| 2 | Wire solutions into mappings + preflight; confirm staged extract path |
| 3 | Repair Agent: error → patch → re-transform from stage → retry |
| 4 | Pilot on real schema + metrics |

---

## 12. Director summary (one slide)

```text
Expert maps fields
        ↓
Relationship Agent finds relationship edge cases + proposes fixes
        ↓
EXTRACT everything → OUR STAGE
        ↓
TRANSFORM (lookups use id_mappings) → LOAD to Salesforce (parents first)
        ↓
On SF datatype errors → Repair Agent fixes transform → retry from stage
```

**Ask:** Bedrock Converse access for Relationship + Repair agents.  
**Non-ask:** Agents do not replace human field mapping; agents do not write to Salesforce outside existing loaders.

---

## 13. How to use this document

- Open in Cursor / VS Code / GitHub to render Mermaid diagrams  
- Export to PDF via Markdown preview print, or paste diagrams into PowerPoint  
- For a slide deck: sections 2, 3, 5, 6, 8, 12 are the core visual story
