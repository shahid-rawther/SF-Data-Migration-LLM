// Typed API client for the ITSM Migration backend

const BASE = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    // The API reports the reason in `detail`. Shown raw, that reason arrives
    // wrapped in JSON behind a status line, so the sentence written for the
    // operator ends up as the least readable part of the message.
    let reason = text;
    try {
      const body = JSON.parse(text);
      if (typeof body?.detail === 'string' && body.detail) reason = body.detail;
    } catch {
      // Not JSON — a proxy or a crash, so the raw body is all there is to show.
    }
    throw new Error(reason || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

// ── Types ─────────────────────────────────────────────────────────────────────

export interface Source {
  id: string;
  name: string;
  description: string;
  credentials_schema: CredentialsSchema;
}

export interface CredentialsSchema {
  type: string;
  required: string[];
  properties: Record<string, FieldSchema>;
}

export interface FieldSchema {
  type: string;
  title: string;
  description?: string;
  format?: string;
}

export interface MigratableObject {
  name: string;
  label: string;
  category: string;
  destination_object: string;
  source_tables: string[];
  /** @deprecated use destination_object */
  salesforce_object: string;
  /** @deprecated use source_tables */
  servicenow_tables: string[];
  dependencies: string[];
  priority: number;
}

export interface MigrationSession {
  id: string;
  source_type: string;
  selected_objects: string[];
  config: SessionConfig;
  status: 'pending' | 'running' | 'paused' | 'stopped' | 'completed' | 'failed';
  phase?: string;
  started_at?: string;
  updated_at: string;
  completed_at?: string;
  live_progress?: ProgressEvent;
}

export interface SessionConfig {
  source_type: string;
  /** Secrets come back masked, so these cannot be replayed against an API. */
  source_credentials: Record<string, string>;
  destination_credentials: Record<string, string>;
  /**
   * The saved connections behind those credentials. Unlike the credentials
   * these survive the round trip, which is what lets a loaded session go on
   * describing schemas. Empty on sessions created before they were recorded.
   */
  source_connection_id: string;
  destination_connection_id: string;
  selected_objects: string[];
  options: MigrationOptions;
}

export interface SessionConnectionCandidate {
  id: string;
  name: string;
  type: string;
  /** Reaches the same instance and account the session was created against. */
  matches_session: boolean;
}

export interface SessionConnectionSide {
  connection_id: string;
  candidates: SessionConnectionCandidate[];
}

export interface SessionConnections {
  session_id: string;
  source_type: string;
  source: SessionConnectionSide;
  destination: SessionConnectionSide;
}

export interface MigrationOptions {
  start_date: string;
  end_date: string;
  /** Caps rows extracted per source table. Applied to each table separately. */
  service_now_batch_size: number;
  disable_load: boolean;
}

export interface ObjectCounts {
  processed: number;
  failed: number;
}

export interface ProgressEvent {
  session_id: string;
  phase: string;
  object: string;
  batch_current: number;
  batch_total: number;
  records_fetched: number;
  records_processed: number;
  records_failed: number;
  overall_percent: number;
  status: string;
  message: string;
  records_by_object: Record<string, ObjectCounts>;
  load_errors?: Record<string, string[]>;
  /** References loaded empty on purpose, each awaiting a decision. */
  pending_backfill?: BackfillUnit[];
  /**
   * References deferred by *earlier* runs that this run has just made fillable,
   * by loading the object they were waiting for. Reported here because this is
   * the moment the wait ends, and the run that recorded it is not one anybody
   * has a reason to go back to.
   */
  unblocked_backfill?: BackfillUnit[];
}

/**
 * One lookup that was loaded empty because no order could have resolved it, and
 * the state of the follow-up update that fills it.
 *
 * Nothing here runs on its own: a unit stays `pending` until someone approves
 * it, because filling it means updating records already in the destination.
 */
export interface AgentChange {
  id: number;
  run_id: string;
  session_id: string;
  agent: string;
  /** 'notice' is the agent reporting a concern; every other kind is a write. */
  kind: string;
  mapping_name: string;
  field: string;
  previous_value: unknown;
  new_value: unknown;
  rationale: string;
  /** For a notice: the concrete next step the agent recommends. */
  suggested_action?: string;
  severity: 'info' | 'warning' | 'blocker' | string;
  /** False for a notice, which changes nothing and cannot be reverted. */
  applied: boolean;
  applied_at: string | null;
  reverted: boolean;
}

export interface DestinationRecord {
  id: string;
  name: string;
  /** Disambiguating columns for targets that need them, e.g. DeveloperName. */
  extra?: Record<string, string | number | boolean | null>;
}

export interface DestinationRecordSearch {
  object_name: string;
  records: DestinationRecord[];
  /** Rows matching the query in the org, which may exceed those returned. */
  total: number;
  truncated: boolean;
  /** Columns the org refused, e.g. IsPersonType where the feature is off. */
  unavailable_fields: string[];
  /** Set when the query was narrowed to the mapping's destination object. */
  scoped_to: string;
  error: string;
}

export interface BackfillUnit {
  id: number;
  session_id: string;
  mapping_name: string;
  destination_object: string;
  source_table: string;
  field: string;
  /** Why it could not be resolved on the first pass. */
  reason: 'dependency_cycle' | 'self_reference' | 'parent_not_migrated' | string;
  /** Records that loaded without the reference. */
  rows_deferred: number;
  /** How many of those can be attached now, given what has since loaded. */
  rows_resolvable: number;
  /** Mappings whose ids this is waiting on, which may not be in the session. */
  awaiting?: string[];
  /**
   * The destination object whose ids are being waited for. Held as well as the
   * mapping names because the object may have no mapping yet, in which case
   * building one is what ends the wait.
   */
  awaiting_object?: string;
  status: 'pending' | 'approved' | 'running' | 'completed' | 'failed' | 'skipped';
  records_updated: number;
  records_failed: number;
  detail: string;
  created_at: string;
  decided_at: string | null;
  completed_at: string | null;
  pending: boolean;
  /** A sentence describing what filling this would do, ready to display. */
  summary: string;
}

/** One lookup this run will load empty on purpose, named before launch. */
export interface DeferredLookupNotice {
  mapping: string;
  destination_object: string;
  field: string;
  reason: string;
  /** The object whose ids it is waiting for, when that is what it waits on. */
  awaiting_object?: string;
  /** Mappings that would end the wait, and are not already in this run. */
  awaiting?: string[];
  /** False when nothing migrates the awaited object yet, so one must be built. */
  buildable?: boolean;
}

export interface PreflightResult {
  ok: boolean;
  enforced: boolean;
  blockers: { kind: string; mapping: string; field: string; detail: string }[];
  warnings: { kind: string; mapping: string; field: string; detail: string }[];
  order: string[];
  has_cycle: boolean;
  checked: boolean;
  note: string;
  deferred: DeferredLookupNotice[];
  /** Ready-to-show sentence, empty when nothing will be deferred. */
  deferred_notice: string;
}

/** One object's standing in a session, as the run plan reports it. */
export interface RunPlanEntry {
  object: string;
  destination_object: string;
  state: 'completed' | 'in_progress' | 'pending' | 'unmapped';
  loaded: number;
  source_rows: number;
}

/**
 * What a run of an existing session would do. Offered before launching, because
 * the difference between the two lists is the difference between adding to a
 * session and migrating it all over again.
 */
export interface SessionRunPlan {
  session_id: string;
  status: string;
  will_run: string[];
  will_skip: string[];
  pending: RunPlanEntry[];
  skipped: RunPlanEntry[];
}

export interface BackfillReport {
  units: BackfillUnit[];
  pending: BackfillUnit[];
  /** Undecided or approved, and with something that can actually be filled. */
  ready: BackfillUnit[];
}

/** One selected object's source, destination and row counts. */
export interface SessionObjectStatus {
  object: string;
  source_table: string;
  source_tables: string[];
  destination_object: string;
  id_mapping_table: string;
  /** Rows the source had for this object, from the staged extract. */
  source_rows: number;
  source_rows_origin: 'stage' | 'checkpoint' | 'none';
  /** Rows written to the destination, counted from the id map. */
  loaded: number;
  processed: number;
  failed: number;
  percent: number;
  state: 'completed' | 'in_progress' | 'pending' | 'unmapped';
  current: boolean;
  errors: string[];
}

export interface SessionStatusReport {
  session_id: string;
  source_type: string;
  destination_type: string;
  status: string;
  phase: string;
  started_at: string;
  updated_at: string;
  completed_at: string;
  live: boolean;
  dry_run: boolean;
  objects: SessionObjectStatus[];
  totals: {
    objects: number;
    objects_completed: number;
    source_rows: number;
    loaded: number;
    failed: number;
    percent: number;
  };
  checkpoint: {
    sequence: number;
    saved_at: string;
    phase: string;
    completed_objects: string[];
    id_mapping_count: number;
    stage_available: boolean;
    credentials_required: boolean;
    last_error: string;
  } | null;
}

export interface CreateMigrationRequest {
  source_type: string;
  destination_type?: string;
  source_credentials: Record<string, string>;
  destination_credentials: Record<string, string>;
  /** Recorded so the session can keep reaching the same places on its own. */
  source_connection_id?: string;
  destination_connection_id?: string;
  selected_objects: string[];
  options: Partial<MigrationOptions>;
}

export interface SalesforceDiagnosticsResponse {
  valid: boolean;
  message: string;
  run_as_user_id?: string;
  required_objects?: string[];
  visibility?: Record<string, boolean>;
  missing_objects?: string[];
}

export interface FieldResult {
  source_field: string;
  destination_field: string;
  status: 'mapped' | 'type_mismatch' | 'missing_in_source' | 'no_mapping';
  source_type: string;
  destination_type: string;
  suggestion: string;
  suggested_custom_field: string;
  suggested_field_type?: string;  // Inferred Salesforce field type for creation
}

export interface CreateCustomFieldRequest {
  destination_credentials: Record<string, string>;
  salesforce_object: string;
  field_label: string;
  field_api_name: string;
  field_type?: string;
  length?: number;
  description?: string;
  /**
   * Marks the field as an external id, which is what lets a load upsert on it
   * and so recognise records it created on an earlier run rather than inserting
   * duplicates. Salesforce restricts both flags to Text, Number and Email.
   */
  external_id?: boolean;
  unique?: boolean;
}

export interface CreateCustomFieldResponse {
  success: boolean;
  message: string;
  field_api_name: string;
}

export interface SuggestFieldNameResponse {
  suggested_label: string;
  suggested_api_name: string;
  suggested_type: string;
  suggested_length: number;
}

export interface ObjectCompatibility {
  object_name: string;
  source_label: string;
  salesforce_object: string;
  total_fields: number;
  mapped_fields: number;
  missing_fields: number;
  type_mismatches: number;
  compatibility_pct: number;
  fields: FieldResult[];
  blockers: string[];
}

export interface DependencyEdge {
  from_mapping: string;
  to_mapping: string;
  field: string;
  required: boolean;
}

export interface DependencyWarning {
  mapping: string;
  field: string;
  references: string;
  reason: string;
}

export interface BlockingRequiredField {
  mapping: string;
  field: string;
  reason: string;
}

export interface CompatibilityReport {
  source_type: string;
  overall_pct: number;
  objects: ObjectCompatibility[];
  summary: string;
  dependency_order?: string[];
  dependency_edges?: DependencyEdge[];
  dependency_warnings?: DependencyWarning[];
  dependency_missing_parents?: DependencyWarning[];
  dependency_has_cycle?: boolean;
  // Hard blockers that must be resolved before launch (EC-33 / guardrail).
  blocking_required_fields?: BlockingRequiredField[];
  // Non-blocking: picklist destinations with no/partial value map.
  picklist_mapping_warnings?: BlockingRequiredField[];
}

export interface SavedConnection {
  id: string;
  name: string;
  type: string;
  credentials: Record<string, string>;
  created_at: string;
  updated_at: string;
}

// ── Dynamic mapping types ───────────────────────────────────────────────────────

export interface MappingMeta {
  name: string;
  source_type: string;
  priority: number;
  is_system: boolean;
  created_at: string;
  updated_at: string;
}

export interface SourceFieldInfo {
  name: string;
  label: string;
  field_type: string;
  required: boolean;
  example: string;
  reference_table?: string;
}

export interface PrerequisiteNode {
  object: string;
  depth: number;
  status: 'selected' | 'mapping_exists' | 'needs_mapping';
  selected: boolean;
  is_target: boolean;
  mapping_names: string[];
  source_tables: string[];
  self_reference_fields: string[];
  schema_unavailable: boolean;
  needed_by: { object: string; field: string; mapped: boolean; required: boolean }[];
  needed_by_count: number;
  required_by_someone: boolean;
}

export interface PrerequisiteEdge {
  from_object: string;
  to_object: string;
  field: string;
  mapped: boolean;
  required: boolean;
  polymorphic: boolean;
}

/** A lookup field with no migratable object on the other end. */
export interface PrerequisiteGap {
  object: string;
  field: string;
  candidates: string[];
  candidate_count: number;
  polymorphic: boolean;
}

export interface PrerequisiteReport {
  object: string;
  /** Parent-first load order, containing the object that was asked about. */
  order: string[];
  nodes: PrerequisiteNode[];
  edges: PrerequisiteEdge[];
  /** Prerequisites not yet covered by the run. */
  missing_objects: string[];
  ready: boolean;
  /** Groups of objects that reference each other, so no strict order exists. */
  cycles: string[][];
  /** Salesforce requires these lookups but nothing migrates their target. */
  required_gaps: PrerequisiteGap[];
  /** Lookups the mapping populates but whose target nothing migrates. */
  unresolved_references: PrerequisiteGap[];
  other_references: PrerequisiteGap[];
  truncated: boolean;
  truncation_reason: string;
  max_depth: number;
}

/** One object in the ancestor graph above a selection of destination objects. */
export interface HierarchyNode {
  object: string;
  /** 0 is the top of the graph; every object sits below what it depends on. */
  level: number;
  /** Chosen by the user, rather than pulled in as a parent. */
  selected: boolean;
  has_mapping: boolean;
  mapping_names: string[];
  source_tables: string[];
  self_reference_fields: string[];
  schema_unavailable: boolean;
  depends_on: {
    object: string;
    field: string;
    required: boolean;
    mapped: boolean;
    polymorphic: boolean;
  }[];
  needed_by: { object: string; field: string; required: boolean; mapped: boolean }[];
  depends_on_count: number;
  needed_by_count: number;
  required_by_someone: boolean;
  /** Salesforce will not accept the child record without this parent. */
  required_chain: boolean;
  /** Required, or a lookup some mapping fills — so this migration as configured
   *  cannot load the selection without it. Everything else is merely above it. */
  needed: boolean;
  in_cycle: boolean;
}

export interface HierarchyEdge {
  child: string;
  parent: string;
  field: string;
  required: boolean;
  mapped: boolean;
  polymorphic: boolean;
}

export interface HierarchyGraph {
  selected_objects: string[];
  /** Parent-first, so it reads as a load order on its own. */
  nodes: HierarchyNode[];
  edges: HierarchyEdge[];
  /** Object names grouped by level, top row first. */
  levels: string[][];
  order: string[];
  cycles: string[][];
  /** The ancestors Salesforce will not let the load skip, in the same order. */
  required_objects: string[];
  /** The ancestors this migration cannot load the selection without. A superset
   *  of required_objects: it also covers lookups the mappings actually fill. */
  needed_objects: string[];
  /** Salesforce requires these lookups but nothing in the graph fills them. */
  required_gaps: PrerequisiteGap[];
  /** Lookups accepting many objects, none of which are in the migration. */
  polymorphic_choices: PrerequisiteGap[];
  objects_without_mapping: string[];
  truncated: boolean;
  truncation_reason: string;
  max_depth: number;
  max_nodes: number;
}

export interface OrderStep {
  object: string;
  /** Empty for the objects the model did not think worth a comment. */
  reason: string;
  /** Set when the model placed this object above something it depends on. */
  conflict: string;
}

export interface OrderDecision {
  object: string;
  field: string;
  issue: string;
  recommendation: string;
  severity: 'blocker' | 'warning' | 'info';
}

/** The model's advice on sequencing. Advisory: the run obeys the graph. */
export interface OrderSuggestion {
  selected_objects: string[];
  computed_order: string[];
  steps: OrderStep[];
  decisions: OrderDecision[];
  summary: string;
  appended: string[];
  dropped: { object: string; reason: string }[];
  /** Whether the suggestion loads every parent before its children. */
  valid: boolean;
  matches_computed: boolean;
  model_invoked: boolean;
  usage: Record<string, number>;
  error: string;
}

export interface SourceChoiceValue {
  value: string;
  label: string;
}

export interface DestinationFieldInfo {
  name: string;
  label: string;
  type: string;
  length: number;
  required: boolean;
  createable: boolean;
  updateable: boolean;
  /** Usable as an upsert key — an External Id, or a unique standard field. */
  id_lookup: boolean;
  reference_to: string[];
  /**
   * The values this field will accept. Narrowed to the record type when one
   * was supplied, in which case it can be a strict subset of what the field
   * defines — see `picklist_unavailable`.
   */
  picklist_values: string[];
  /**
   * A restricted picklist rejects the row when handed anything off the list;
   * an unrestricted one stores the value regardless. Absent on non-picklists.
   */
  picklist_restricted?: boolean;
  picklist_dependent?: boolean;
  /** The field whose value decides which entries here are legal. */
  picklist_controller?: string;
  /**
   * Defined by the field but withheld by the record type. These look valid in
   * any describe and are still rejected at load, which is why they are named
   * rather than simply omitted.
   */
  picklist_unavailable?: string[];
  /** Controller value to the entries it permits. Only sent when scoped. */
  picklist_valid_for?: Record<string, string[]>;
}

export interface MappingFieldDef {
  source_field?: string | string[];
  destination_field: string;
  transformation_function?: string;
  transformation_params?: Record<string, unknown>;
  use_constant?: string;
  // Per-migration literal value chosen in the wizard (UI-specific, not env-based).
  constant_value?: string | number | boolean;
  // How this field is populated. Inferred when absent for backward compatibility.
  value_source?: 'field' | 'constant' | 'lookup';
  skip_if_not_found?: boolean;
  /**
   * Load the record with this reference empty when it cannot be resolved, and
   * record which source id it wanted so a later pass can attach it. Without
   * this a reference that is not yet loadable is simply lost: the record still
   * arrives, but nothing remembers what it pointed at.
   */
  defer_to_backfill?: boolean;
  /**
   * The destination object whose ids this field waits for. Recorded because the
   * id-mapping table it reads may not exist yet — its name comes from the
   * configuration of whichever mapping ends up migrating that object — so the
   * wait has to be expressed as the object, not as a table name.
   */
  defer_object?: string;
  /** Why it was deferred, which decides how the wait is explained. */
  defer_reason?: 'dependency_cycle' | 'self_reference' | 'parent_not_migrated' | string;
  // Present on model-proposed fields only:
  confidence?: number;
  match_reason?: string;
  type_warning?: string;
}

/** Result of a model-assisted field-matching run. Proposals only — the wizard
 *  decides what to keep. */
export interface SemanticSuggestReport {
  source_table: string;
  destination_object: string;
  matches: MappingFieldDef[];
  rejected: { match: Record<string, unknown>; reason: string }[];
  summary: string;
  considered_source_fields: number;
  considered_destination_fields: number;
  truncated: boolean;
  usage: { calls?: number; total_tokens?: number };
  error: string;
}

/** Result of a model-assisted picklist value-matching run. Proposals only. */
export interface ValueMapSuggestReport {
  source_field: string;
  destination_field: string;
  matches: {
    source_value: string;
    destination_value: string;
    confidence: number;
    rationale: string;
    /** An approximation rather than an equivalence — applied, but worth reading. */
    needs_review: boolean;
  }[];
  rejected: { match: Record<string, unknown>; reason: string }[];
  /** Last resort: values the picklist had no plausible home for at all. */
  unmapped: { source_value: string; reason: string }[];
  suggested_default: string;
  summary: string;
  considered_source_values: number;
  considered_destination_values: number;
  truncated: boolean;
  /** Whether an unmatched value fails the row or is stored off-list. */
  restricted: boolean;
  usage: { calls?: number; total_tokens?: number };
  error: string;
}

export interface TypeCheckRequest {
  source_type: string;
  destination_type: string;
}

export interface TypeCheckResponse {
  compatible: boolean;
  coercible: boolean;
  transform: string;
  params: Record<string, unknown>;
  warning: string;
}

export interface TransformDef {
  name: string;
  label: string;
  description: string;
  params_schema: Record<string, { type: string; default: unknown; description: string }>;
}

export interface MappingConfig {
  name?: string;
  source_type?: string;
  destination_object: string;
  source_tables: string[];
  /** @deprecated use destination_object */
  salesforce_object?: string;
  /** @deprecated use source_tables */
  servicenow_tables?: string[];
  operation: 'insert' | 'update' | 'upsert';
  priority: number;
  fields: MappingFieldDef[];
  joins?: unknown[];
  filters?: unknown[];
  source_id_column?: string;
  transformation_name?: string;
}

/**
 * Where a lookup to one destination object can get its ids. A lookup reads an
 * id-mapping table that some other mapping writes on load, and that table's
 * name is built from the parent's own configuration, so it has to be resolved
 * rather than guessed from the object name.
 */
export interface LookupResolution {
  destination_object: string;
  candidates: {
    mapping_name: string;
    source_table: string;
    lookup_table: string;
    /** Ids on hand now. Zero means the table exists only once its mapping runs. */
    migrated_count: number;
  }[];
  /** False when nothing in the catalogue can ever populate this object. */
  resolvable: boolean;
}

export interface MappingValidationResult {
  name: string;
  valid: boolean;
  errors: string[];
  warnings: string[];
}

// ── Sources ───────────────────────────────────────────────────────────────────

export const api = {
  sources: {
    list: (): Promise<Source[]> => request('/sources/'),
    get: (id: string): Promise<Source> => request(`/sources/${id}`),
    listObjects: (id: string): Promise<MigratableObject[]> => request(`/sources/${id}/objects`),
    getObject: (sourceId: string, objectName: string): Promise<MigratableObject> =>
      request(`/sources/${sourceId}/objects/${objectName}`),
    validateCredentials: (
      sourceId: string,
      credentials: Record<string, string>,
    ): Promise<{ valid: boolean }> =>
      request(`/sources/${sourceId}/validate-credentials`, {
        method: 'POST',
        body: JSON.stringify({ credentials }),
      }),
    getRowCount: (
      sourceId: string,
      objectName: string,
      credentials: Record<string, string>,
    ): Promise<{ object_name: string; row_count: number }> =>
      request(`/sources/${sourceId}/row-count`, {
        method: 'POST',
        body: JSON.stringify({ object_name: objectName, credentials }),
      }),
  },

  destinations: {
    validateSalesforceCredentials: (
      credentials: Record<string, string>,
    ): Promise<{ valid: boolean; message?: string }> =>
      request('/destinations/salesforce/validate-credentials', {
        method: 'POST',
        body: JSON.stringify({ credentials }),
      }),
    checkSalesforceDiagnostics: (
      credentials: Record<string, string>,
      selected_objects: string[],
    ): Promise<SalesforceDiagnosticsResponse> =>
      request('/destinations/salesforce/diagnostics', {
        method: 'POST',
        body: JSON.stringify({ credentials, selected_objects }),
      }),
  },

  migrations: {
    create: (body: CreateMigrationRequest): Promise<{ session_id: string; status: string }> =>
      request('/migrations/', { method: 'POST', body: JSON.stringify(body) }),
    list: (): Promise<MigrationSession[]> => request('/migrations/'),
    get: (id: string): Promise<MigrationSession> => request(`/migrations/${id}`),
    status: (id: string, includeCheckpoint = true): Promise<SessionStatusReport> =>
      request(`/migrations/${id}/status?include_checkpoint=${includeCheckpoint}`),
    start: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/start`, { method: 'POST' }),
    pause: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/pause`, { method: 'POST' }),
    resume: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/resume`, { method: 'POST' }),
    stop: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/stop`, { method: 'POST' }),
    retry: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/retry`, { method: 'POST' }),

    /**
     * Add objects to an existing session without running anything. Adding is the
     * only supported edit: dropping one that has already loaded would leave
     * records in the destination the session no longer accounts for.
     */
    addObjects: (
      id: string,
      objects: string[],
    ): Promise<{ selected_objects: string[]; added: string[] }> =>
      request(`/migrations/${id}/objects`, {
        method: 'POST',
        body: JSON.stringify({ objects }),
      }),
    /**
     * The saved connections this session resolves through, plus the ones it
     * could resolve through, each flagged with whether it reaches the same
     * place the session was created against.
     */
    connections: (id: string): Promise<SessionConnections> =>
      request(`/migrations/${id}/connections`),
    /** Point the session at saved connections and adopt their credentials. */
    linkConnections: (
      id: string,
      body: { source_connection_id?: string; destination_connection_id?: string },
    ): Promise<{ source_connection_id: string; destination_connection_id: string }> =>
      request(`/migrations/${id}/connections`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),

    /** What a run would work on and what it would leave alone. Runs nothing. */
    runPlan: (id: string): Promise<SessionRunPlan> => request(`/migrations/${id}/run-plan`),
    /**
     * Run only the objects that still need it. Left empty, the plan's own list is
     * used; naming objects runs exactly those.
     */
    runRemaining: (
      id: string,
      objects: string[] = [],
    ): Promise<{ session_id: string; status: string; running: string[]; skipped: string[] }> =>
      request(`/migrations/${id}/run-remaining`, {
        method: 'POST',
        body: JSON.stringify({ objects }),
      }),

    backfill: {
      /** Read the deferred references. Re-checks the id map; runs nothing. */
      list: (sessionId?: string): Promise<BackfillReport> =>
        request(sessionId ? `/migrations/backfill?session_id=${sessionId}` : '/migrations/backfill'),
      /** Record a decision. Approving marks intent; it does not run the fill. */
      decide: (unitId: number, approve: boolean, note = ''): Promise<BackfillUnit> =>
        request(`/migrations/backfill/${unitId}/decision`, {
          method: 'POST',
          body: JSON.stringify({ approve, note }),
        }),
      /** Fill one approved reference. Refused for anything not approved. */
      run: (unitId: number): Promise<BackfillUnit> =>
        request(`/migrations/backfill/${unitId}/run`, { method: 'POST' }),
      runApproved: (
        sessionId: string,
      ): Promise<{ session_id: string; ran: BackfillUnit[]; count: number }> =>
        request(`/migrations/${sessionId}/backfill/run-approved`, { method: 'POST' }),
    },
  },

  connections: {
    list: (type?: string): Promise<SavedConnection[]> =>
      request(type ? `/connections/?type=${type}` : '/connections/'),
    get: (id: string): Promise<SavedConnection> => request(`/connections/${id}`),
    create: (name: string, type: string, credentials: Record<string, string>): Promise<SavedConnection> =>
      request('/connections/', {
        method: 'POST',
        body: JSON.stringify({ name, type, credentials }),
      }),
    update: (id: string, body: { name?: string; credentials?: Record<string, string> }): Promise<SavedConnection> =>
      request(`/connections/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
    delete: (id: string): Promise<{ ok: boolean }> =>
      request(`/connections/${id}`, { method: 'DELETE' }),
  },

  compat: {
    check: (
      source_type: string,
      source_credentials: Record<string, string>,
      destination_credentials: Record<string, string>,
      selected_objects: string[],
    ): Promise<CompatibilityReport> =>
      request('/compat/check', {
        method: 'POST',
        body: JSON.stringify({ source_type, source_credentials, destination_credentials, selected_objects }),
      }),
    typeCheck: (
      source_type: string,
      destination_type: string,
    ): Promise<TypeCheckResponse> =>
      request('/compat/type-check', {
        method: 'POST',
        body: JSON.stringify({ source_type, destination_type }),
      }),
  },

  customFields: {
    suggest: (
      source_field: string,
      salesforce_object: string,
    ): Promise<SuggestFieldNameResponse> =>
      request('/custom-fields/suggest', {
        method: 'POST',
        body: JSON.stringify({ source_field, salesforce_object }),
      }),
    create: (
      body: CreateCustomFieldRequest,
    ): Promise<CreateCustomFieldResponse> =>
      request('/custom-fields/create', {
        method: 'POST',
        body: JSON.stringify(body),
      }),
  },

  /**
   * Schema introspection. Every call authenticates one of two ways: an inline
   * credentials dict, or a `connection_id` the server resolves against the
   * saved connections. The id matters for anything opened on an existing
   * session, whose credentials come back masked and so cannot be replayed.
   */
  schema: {
    sourceTables: (
      source_type: string,
      credentials: Record<string, string>,
      opts: { connection_id?: string } = {},
    ): Promise<{ source_type: string; tables: string[] }> =>
      request('/schema/source-tables', {
        method: 'POST',
        body: JSON.stringify({ source_type, credentials, ...opts }),
      }),
    sourceFields: (
      source_type: string,
      credentials: Record<string, string>,
      opts: { object_name?: string; table?: string; connection_id?: string },
    ): Promise<{ source_type: string; fields: SourceFieldInfo[] }> =>
      request('/schema/source-fields', {
        method: 'POST',
        body: JSON.stringify({ source_type, credentials, ...opts }),
      }),
    prerequisites: (
      object_name: string,
      credentials: Record<string, string>,
      opts: {
        source_type?: string;
        selected_objects?: string[];
        draft_config?: unknown;
        max_depth?: number;
        max_nodes?: number;
        connection_id?: string;
      } = {},
    ): Promise<PrerequisiteReport> =>
      request('/schema/prerequisites', {
        method: 'POST',
        body: JSON.stringify({ object_name, credentials, ...opts }),
      }),
    destinationHierarchy: (
      selected_objects: string[],
      credentials: Record<string, string>,
      opts: {
        source_type?: string;
        max_depth?: number;
        max_nodes?: number;
        connection_id?: string;
      } = {},
    ): Promise<HierarchyGraph> =>
      request('/schema/destination-hierarchy', {
        method: 'POST',
        body: JSON.stringify({ selected_objects, credentials, ...opts }),
      }),
    sourceValues: (
      source_type: string,
      credentials: Record<string, string>,
      opts: { table: string; field: string; limit?: number; connection_id?: string },
    ): Promise<{ source_type: string; values: SourceChoiceValue[] }> =>
      request('/schema/source-values', {
        method: 'POST',
        body: JSON.stringify({ source_type, credentials, ...opts }),
      }),
    destinationObjects: (
      credentials: Record<string, string>,
      destination_type = 'salesforce',
      opts: { connection_id?: string } = {},
    ): Promise<{ destination_type: string; objects: { name: string; label: string; createable: boolean }[] }> =>
      request('/schema/destination-objects', {
        method: 'POST',
        body: JSON.stringify({ destination_type, credentials, ...opts }),
      }),
    destinationFields: (
      object_name: string,
      credentials: Record<string, string>,
      opts: { connection_id?: string; record_type_id?: string } = {},
    ): Promise<{ object_name: string; fields: DestinationFieldInfo[] }> =>
      request('/schema/destination-fields', {
        method: 'POST',
        body: JSON.stringify({ object_name, credentials, ...opts }),
      }),
    destinationRecords: (
      object_name: string,
      credentials: Record<string, string>,
      opts: { name_field?: string; limit?: number; connection_id?: string } = {},
    ): Promise<{ object_name: string; records: { id: string; name: string }[] }> =>
      request('/schema/destination-records', {
        method: 'POST',
        body: JSON.stringify({ object_name, credentials, ...opts }),
      }),
    /** On-demand lookup: a short page plus the total, so it can say "10 of 143". */
    destinationRecordSearch: (
      object_name: string,
      credentials: Record<string, string>,
      opts: {
        name_field?: string;
        limit?: number;
        search?: string;
        for_object?: string;
        connection_id?: string;
      } = {},
    ): Promise<DestinationRecordSearch> =>
      request('/schema/destination-record-search', {
        method: 'POST',
        body: JSON.stringify({ object_name, credentials, ...opts }),
      }),
  },

  mappings: {
    list: (source_type?: string): Promise<MappingMeta[]> =>
      request(source_type ? `/mappings/?source_type=${source_type}` : '/mappings/'),
    get: (name: string): Promise<{ name: string; config: MappingConfig }> =>
      request(`/mappings/${encodeURIComponent(name)}`),
    create: (name: string, config: MappingConfig, source_type: string): Promise<MappingMeta> =>
      request('/mappings/', {
        method: 'POST',
        body: JSON.stringify({ name, config, source_type }),
      }),
    update: (name: string, config: MappingConfig, source_type: string): Promise<MappingMeta> =>
      request(`/mappings/${encodeURIComponent(name)}`, {
        method: 'PUT',
        body: JSON.stringify({ config, source_type }),
      }),
    remove: (name: string): Promise<{ ok: boolean }> =>
      request(`/mappings/${encodeURIComponent(name)}`, { method: 'DELETE' }),
    semanticSuggest: (body: {
      source_type: string;
      source_table: string;
      salesforce_object: string;
      source_credentials: Record<string, string>;
      destination_credentials: Record<string, string>;
      source_connection_id?: string;
      destination_connection_id?: string;
      existing_fields?: MappingFieldDef[];
      other_mappings?: { source_table: string; sf_object: string; mapping_name: string }[];
      operation?: string;
    }): Promise<SemanticSuggestReport> =>
      request('/mappings/semantic-suggest', { method: 'POST', body: JSON.stringify(body) }),
    /** Pair one source column's values with a picklist's accepted values. */
    suggestValueMap: (body: {
      source_type: string;
      source_table: string;
      source_field: string;
      salesforce_object: string;
      destination_field: string;
      source_credentials: Record<string, string>;
      destination_credentials: Record<string, string>;
      source_connection_id?: string;
      destination_connection_id?: string;
      existing_fields?: MappingFieldDef[];
      source_values?: { value: string; label: string }[];
    }): Promise<ValueMapSuggestReport> =>
      request('/mappings/suggest-value-map', { method: 'POST', body: JSON.stringify(body) }),
    /** Which id-mapping table a lookup to this object should read. */
    lookupResolution: (
      destination_object: string,
      source_type?: string,
    ): Promise<LookupResolution> =>
      request(
        `/mappings/lookup-resolution?destination_object=${encodeURIComponent(destination_object)}` +
          (source_type ? `&source_type=${encodeURIComponent(source_type)}` : ''),
      ),
    validate: (
      name: string,
      destination_credentials: Record<string, string>,
    ): Promise<MappingValidationResult> =>
      request(`/mappings/${encodeURIComponent(name)}/validate`, {
        method: 'POST',
        body: JSON.stringify({ destination_credentials }),
      }),
  },

  transforms: {
    /** Scoped to the source system, so a mapping is only offered transforms it
     *  can actually resolve at transform time. */
    list: (source_type?: string): Promise<TransformDef[]> =>
      request(
        source_type
          ? `/transforms/?source_type=${encodeURIComponent(source_type)}`
          : '/transforms/',
      ),
  },

  agents: {
    /** Ask the model to sequence a selection and explain the sequence. The
     *  graph and its own parent-first order come back alongside the advice. */
    suggestOrder: (
      selected_objects: string[],
      destination_credentials: Record<string, string>,
      opts: {
        source_type?: string;
        max_depth?: number;
        max_nodes?: number;
        destination_connection_id?: string;
      } = {},
    ): Promise<{ graph: HierarchyGraph; suggestion: OrderSuggestion }> =>
      request('/mapping-agent/suggest-order', {
        method: 'POST',
        body: JSON.stringify({ selected_objects, destination_credentials, ...opts }),
      }),

    /** The deterministic launch gate, without starting anything. */
    preflight: (body: {
      source_type: string;
      selected_objects: string[];
      source_credentials?: Record<string, string>;
      destination_credentials?: Record<string, string>;
    }): Promise<PreflightResult> =>
      request('/mapping-agent/preflight', { method: 'POST', body: JSON.stringify(body) }),

    /**
     * What the agent changed, and what it only wanted to say.
     *
     * Filterable by session or by mapping: the mapping editor is opened on a
     * mapping rather than a run, so it has a name and no session id.
     * `scope: 'latest'` returns only the current migration attempt.
     */
    changes: (
      filter: string | { session_id?: string; mapping_name?: string; scope?: 'all' | 'latest' },
    ): Promise<AgentChange[]> => {
      const { session_id, mapping_name, scope } =
        typeof filter === 'string'
          ? { session_id: filter, mapping_name: undefined, scope: undefined }
          : filter;
      const params = new URLSearchParams();
      if (session_id) params.set('session_id', session_id);
      if (mapping_name) params.set('mapping_name', mapping_name);
      if (scope) params.set('scope', scope);
      return request(`/mapping-agent/changes?${params.toString()}`);
    },

    /** Delete the session's notices from attempts before the current one. */
    discardEarlierNotices: (session_id: string): Promise<{ discarded: number }> =>
      request(`/mapping-agent/changes/notices?session_id=${encodeURIComponent(session_id)}`, {
        method: 'DELETE',
      }),
  },

  health: (): Promise<{ status: string }> => request('/health'),
};
