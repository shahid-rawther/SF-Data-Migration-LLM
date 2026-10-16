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
  /** Totals of the most recent run, from its checkpoint, when none is live. */
  last_run?: {
    records_fetched: number;
    records_processed: number;
    records_failed: number;
    saved_at: string;
  };
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

/** One way to fix a failure. `resolutions` is empty when a person must act. */
export interface DiagnosisSuggestion {
  title: string;
  explanation: string;
  resolutions: Record<string, unknown>[];
  rejected?: { resolution: unknown; reason: string }[];
}

/** Why an object's rows are still rejected after the automatic repairs. */
export interface AgentDiagnosis {
  id: number;
  session_id: string;
  mapping_name: string;
  analysis: string;
  evidence: string[];
  suggestions: DiagnosisSuggestion[];
  created_at: string;
}

export interface ProposalPreviewRow {
  mapping: string;
  field: string;
  target: string;
  kind: string;
  rationale: string;
  applied: boolean;
  before: unknown;
  after: unknown;
}

/** Mapping changes the assistant proposed. Nothing is written until applied. */
export interface AgentProposal {
  id: number;
  session_id: string;
  mapping_name: string;
  summary: string;
  resolutions: Record<string, unknown>[];
  preview: ProposalPreviewRow[];
  rerun: string[];
  status: 'pending' | 'applied' | 'rejected' | 'failed' | string;
  run_id: string;
  error: string;
  created_at: string;
  decided_at: string | null;
  /** 'diagnosis' or 'chat'; empty for proposals stored before this was recorded. */
  origin: 'diagnosis' | 'chat' | '';
  diagnosis_id: number | null;
}

/** The changes one agent run wrote, and where they came from. */
export interface AgentResolutionGroup {
  run_id: string;
  source: 'auto_repair' | 'repair' | 'relationship' | 'proposal' | string;
  origin: 'diagnosis' | 'chat' | '';
  proposal: { id: number; summary: string; rerun: string[]; diagnosis_id: number | null } | null;
  applied_at: string;
  mappings: string[];
  changes: AgentChange[];
}

export interface AgentMessage {
  id: number;
  session_id: string;
  role: 'user' | 'assistant' | string;
  content: string;
  proposal_id: number | null;
  created_at: string;
}

export interface AgentConversation {
  messages: AgentMessage[];
  proposals: AgentProposal[];
  diagnoses: AgentDiagnosis[];
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

// ── Filling a column after the fact ───────────────────────────────────────────

/** How a fill decides the value it writes. */
export type ColumnFillMode = 'constant' | 'value_map' | 'rules';

/** What to do with a row no rule claimed and no map covered. */
export type ColumnFillUnmatched = 'skip' | 'blank' | 'value';

export const COLUMN_FILL_OPERATORS = [
  'equals',
  'not_equals',
  'contains',
  'not_contains',
  'starts_with',
  'ends_with',
  'is_empty',
  'is_not_empty',
  'in',
  'not_in',
  'matches',
  'gt',
  'gte',
  'lt',
  'lte',
] as const;

export type ColumnFillOperator = (typeof COLUMN_FILL_OPERATORS)[number];

/** Operators that ignore the comparison value, so no input is asked for. */
export const COLUMN_FILL_OPERATORS_WITHOUT_VALUE: ColumnFillOperator[] = [
  'is_empty',
  'is_not_empty',
];

export interface ColumnFillCondition {
  /** The source column to test, qualified as "table.column". */
  field: string;
  op: ColumnFillOperator;
  /** Absent for the operators that need nothing to compare against. */
  value?: string | string[];
}

/** What a matched rule sets: a literal, or a transform over a source column. */
export interface ColumnFillOutcome {
  value?: string;
  /** A transform name; requires `from` to say what it applies to. */
  transform?: string;
  from?: string;
  params?: Record<string, unknown>;
}

export interface ColumnFillRule {
  /** Every one of these must hold. */
  all?: ColumnFillCondition[];
  /** At least one of these must hold. */
  any?: ColumnFillCondition[];
  then: ColumnFillOutcome;
}

export interface ColumnFillSpec {
  mapping_name: string;
  field: string;
  mode: ColumnFillMode;
  /** mode: constant */
  constant_value?: string;
  /** mode: value_map — the source column whose values are mapped. */
  source_field?: string;
  value_map?: Record<string, string>;
  /** mode: rules — tried in order, first match wins. */
  rules?: ColumnFillRule[];
  unmatched?: ColumnFillUnmatched;
  unmatched_value?: string;
  case_insensitive?: boolean;
  /**
   * Also write the field into the mapping, so a later run of this object fills
   * it too. Stored without the skip flag the one-off pass uses, so a future
   * insert leaves the column blank rather than dropping the record.
   */
  save_to_mapping?: boolean;
}

/** One object in a session that has records a fill could update. */
export interface ColumnFillTarget {
  mapping_name: string;
  destination_object: string;
  source_table: string;
  loaded: number;
  source_rows: number;
  operation: string;
}

/** How many rows one rule claimed. */
export interface ColumnFillRuleHit {
  /** The rule's position, or -1 for the rows nothing matched. */
  index: number;
  label: string;
  value: string;
  rows: number;
}

/** One source value, what it maps to, and how common it is. */
export interface ColumnFillValueHit {
  source: string;
  destination: string;
  rows: number;
  mapped: boolean;
}

export interface ColumnFillPreview {
  mapping_name: string;
  destination_object: string;
  source_table: string;
  field: string;
  mode: ColumnFillMode;
  /** Rows in the staged extract. */
  rows_source: number;
  /** Of those, how many have a destination record an update could address. */
  rows_migrated: number;
  /** Rows a value was produced for. */
  rows_to_write: number;
  rows_untouched: number;
  /** Readable rendering of what happens to the untouched rows. */
  untouched_means: string;
  rule_hits: ColumnFillRuleHit[];
  value_hits: ColumnFillValueHit[];
  samples: { record: string; source: Record<string, string>; value: string }[];
  /** Values a restricted picklist would refuse, which would fail the load. */
  picklist_rejected: { value: string; rows: number }[];
  warnings: string[];
}

export interface ColumnFillJob {
  id: number;
  session_id: string;
  mapping_name: string;
  destination_object: string;
  source_table: string;
  field: string;
  mode: ColumnFillMode;
  spec: ColumnFillSpec;
  status: 'draft' | 'running' | 'completed' | 'failed' | 'cancelled';
  rows_targeted: number;
  records_updated: number;
  records_failed: number;
  saved_to_mapping: boolean;
  detail: string;
  created_at: string;
  completed_at: string | null;
  /** A sentence describing what this did, ready to display. */
  summary: string;
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
  source_rows_origin: 'source' | 'stage' | 'checkpoint' | 'none';
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

export interface WorkflowFinding {
  stage: string;
  status: string;
  name: string;
  detail: string;
}

export interface WorkflowStage {
  stage: string;
  label: string;
  status: string;
  detail: string;
  findings: WorkflowFinding[];
}

export interface ObjectWorkflow {
  mapping: string;
  salesforce_object: string;
  template_found: boolean;
  ok: boolean;
  failing_rows: number;
  summary: string;
  stages: WorkflowStage[];
}

export interface WorkflowReport {
  ok: boolean;
  summary: string;
  objects: ObjectWorkflow[];
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
  /**
   * Present when the transform is implemented for particular source systems
   * only. Absent means it lives in the shared module and works anywhere.
   */
  source_types?: string[];
  /**
   * Its parameters need a purpose-built editor, so a generic parameter form
   * would only show a raw blob. Leave these out of a plain transform picker.
   */
  builder_only?: boolean;
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

// ── Data readiness ────────────────────────────────────────────────────────────

export type ReadinessStatus =
  | 'covered'
  | 'partial'
  | 'unmapped_with_candidate'
  | 'no_source'
  | 'parent_migratable'
  | 'parent_creatable'
  | 'parent_not_creatable'
  | 'capacity';

/** One required destination field, and whether the source can fill it. */
export interface ReadinessRequirement {
  key: string;
  mapping: string;
  object: string;
  field: string;
  label: string;
  type: string;
  status: ReadinessStatus;
  detail: string;
  source_table: string;
  source_field: string;
  value_source: string;
  /** -1 when the source could not count. */
  total_rows: number;
  empty_rows: number;
  candidates: string[];
  reference_to: string[];
  parent_object: string;
  parent_createable: boolean | null;
  parent_mappings: string[];
  parent_in_run: boolean;
  parent_existing_records: number;
  /** The lookups that pulled a chain mapping in, e.g. "Incident.CallerId". */
  via: string[];
  in_run: boolean;
  is_gap: boolean;
}

export interface ReadinessCapacity {
  mapping: string;
  source_rows: number;
  licence: string;
  available: number;
  short: boolean;
  detail: string;
}

export interface ReadinessReport {
  selected: string[];
  requirements: ReadinessRequirement[];
  gap_count: number;
  status_counts: Record<string, number>;
  /** Mappings outside the run that the selection depends on. */
  chain_mappings: string[];
  capacity: ReadinessCapacity[];
  warnings: string[];
}

/** A write against the Salesforce org itself, run only when Apply is clicked. */
export interface ReadinessSalesforceAction {
  kind: 'create_record' | 'create_custom_field';
  object: string;
  rationale: string;
  values?: Record<string, unknown>;
  field_api_name?: string;
  field_label?: string;
  field_type?: string;
  length?: number;
  description?: string;
}

/** A write against the source system, run only when Apply is clicked. */
export interface ReadinessSourceAction {
  kind: 'fill_source_values';
  table: string;
  field: string;
  value: string;
  /** Extra encoded query narrowing the records; "is empty" is always added. */
  filter: string;
  rationale: string;
  /** How many records it would change when it was validated. */
  affected?: number;
  /** Set when the column is a reference and value is a sys_id. */
  reference_table?: string;
}

/** A mapping change in the resolution vocabulary the other agents use. */
export interface ReadinessResolution {
  kind: string;
  mapping: string;
  field: string;
  rationale: string;
  source_field?: string;
  transform?: string;
  transform_params?: Record<string, unknown>;
  constant_value?: string;
  parent_mappings?: string[];
  value_map?: Record<string, string>;
  value_map_default?: string;
  suggested_action?: string;
  [key: string]: unknown;
}

export interface ReadinessSuggestion {
  title: string;
  explanation: string;
  include_mappings: string[];
  /** Absent on checks stored before source fixes existed. */
  source_actions?: ReadinessSourceAction[];
  salesforce_actions: ReadinessSalesforceAction[];
  resolutions: ReadinessResolution[];
  manual_steps: string[];
  rejected: { reason: string; [key: string]: unknown }[];
  /** Every change in it validated, so Apply can run it whole. */
  applicable: boolean;
  /** Nothing to apply; the manual steps are the suggestion. */
  needs_person: boolean;
}

export interface ReadinessGap {
  key: string;
  mapping: string;
  object: string;
  field: string;
  status: ReadinessStatus;
  in_run: boolean;
  via: string[];
  detail: string;
  analysis: string;
  suggestions: ReadinessSuggestion[];
  /** "agent" when the model wrote the suggestions, "rules" when it could not. */
  source: 'agent' | 'rules';
}

export interface ReadinessApplied {
  run_id: string;
  source_change_ids?: number[];
  sf_change_ids: number[];
  change_ids: number[];
  include_mappings: string[];
  applied_at: string;
  /** Every change it made has since been undone, so it can be applied again. */
  reverted?: boolean;
}

export interface ReadinessSfChange {
  id: number;
  readiness_id: number;
  run_id: string;
  kind: string;
  object_name: string;
  record_id: string;
  field_name: string;
  payload: Record<string, unknown>;
  rationale: string;
  revertible: boolean;
  created_at: string;
  reverted: boolean;
}

export interface ReadinessSourceChange {
  id: number;
  readiness_id: number;
  run_id: string;
  source_type: string;
  kind: string;
  table_name: string;
  field_name: string;
  value: string;
  record_ids: string[];
  record_count: number;
  rationale: string;
  created_at: string;
  reverted: boolean;
}

export interface ReadinessCheck {
  id: number;
  report: ReadinessReport;
  summary: string;
  gaps: ReadinessGap[];
  /** Keyed "gap:suggestion". */
  applied: Record<string, ReadinessApplied>;
  sf_changes: ReadinessSfChange[];
  source_changes?: ReadinessSourceChange[];
  model_invoked: boolean;
  error: string;
  usage: { total_tokens?: number; calls?: number };
}

export interface ReadinessApplyResult {
  run_id: string;
  source_change_ids: number[];
  sf_change_ids: number[];
  change_ids: number[];
  include_mappings: string[];
  source: { kind: string; table: string; field: string; value: string; records: number }[];
  salesforce: { kind: string; object: string; record_id?: string; field?: string }[];
  changes: { mapping_name: string; field?: string; applied?: boolean }[];
}

export interface ReadinessCredentials {
  source_credentials?: Record<string, string>;
  source_connection_id?: string;
  destination_credentials?: Record<string, string>;
  destination_connection_id?: string;
}

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

    /** Writing one column on records a session already loaded. */
    columnFill: {
      /** Objects with loaded records, which are the only ones a fill can touch. */
      targets: (
        sessionId: string,
      ): Promise<{ session_id: string; targets: ColumnFillTarget[] }> =>
        request(`/migrations/${sessionId}/column-fill/targets`),
      /** What the fill would write. Sends nothing to the destination. */
      preview: (sessionId: string, spec: ColumnFillSpec): Promise<ColumnFillPreview> =>
        request(`/migrations/${sessionId}/column-fill/preview`, {
          method: 'POST',
          body: JSON.stringify({ spec }),
        }),
      /** Record the fill as a draft. Still writes nothing until run. */
      create: (
        sessionId: string,
        spec: ColumnFillSpec,
        rows_targeted = 0,
      ): Promise<ColumnFillJob> =>
        request(`/migrations/${sessionId}/column-fill`, {
          method: 'POST',
          body: JSON.stringify({ spec, rows_targeted }),
        }),
      run: (jobId: number): Promise<ColumnFillJob> =>
        request(`/migrations/column-fill/${jobId}/run`, { method: 'POST' }),
      cancel: (jobId: number): Promise<ColumnFillJob> =>
        request(`/migrations/column-fill/${jobId}/cancel`, { method: 'POST' }),
      list: (sessionId?: string): Promise<{ jobs: ColumnFillJob[]; count: number }> =>
        request(
          sessionId
            ? `/migrations/column-fill?session_id=${encodeURIComponent(sessionId)}`
            : '/migrations/column-fill',
        ),
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

  workflow: {
    check: (
      mapping_names: string[],
      destination_credentials: Record<string, string>,
    ): Promise<WorkflowReport> =>
      request('/workflow/check', {
        method: 'POST',
        body: JSON.stringify({ mapping_names, destination_credentials }),
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

    /** The newest diagnosis per object still failing in the current attempt. */
    diagnoses: (session_id: string): Promise<AgentDiagnosis[]> =>
      request(`/mapping-agent/diagnoses?session_id=${encodeURIComponent(session_id)}&scope=latest`),

    /** Diagnose one object's current failures on demand. */
    diagnose: (session_id: string, mapping_name: string): Promise<{ id: number }> =>
      request('/mapping-agent/diagnoses', {
        method: 'POST',
        body: JSON.stringify({ session_id, mapping_name }),
      }),

    /** Turn one suggested solution into a pending proposal. */
    proposeFromDiagnosis: (
      diagnosis_id: number,
      suggestion: number,
    ): Promise<{
      proposal_id: number | null;
      rejected: { resolution?: unknown; reason?: string }[];
      note?: string;
    }> =>
      request(`/mapping-agent/diagnoses/${diagnosis_id}/propose?suggestion=${suggestion}`, {
        method: 'POST',
      }),

    chatHistory: (session_id: string): Promise<AgentConversation> =>
      request(`/mapping-agent/chat?session_id=${encodeURIComponent(session_id)}`),

    chat: (
      session_id: string,
      message: string,
    ): Promise<{ reply: string; proposal: AgentProposal | null; error: string }> =>
      request('/mapping-agent/chat', {
        method: 'POST',
        body: JSON.stringify({ session_id, message }),
      }),

    clearChat: (session_id: string): Promise<{ cleared: boolean }> =>
      request(`/mapping-agent/chat?session_id=${encodeURIComponent(session_id)}`, {
        method: 'DELETE',
      }),

    applyProposal: (
      proposal_id: number,
    ): Promise<{ changes: unknown[]; change_ids: number[]; rerun: string[] }> =>
      request(`/mapping-agent/proposals/${proposal_id}/apply`, { method: 'POST' }),

    rejectProposal: (proposal_id: number): Promise<{ status: string }> =>
      request(`/mapping-agent/proposals/${proposal_id}/reject`, { method: 'POST' }),

    /** Every change an agent applied in the session, newest run first. */
    resolutions: (session_id: string): Promise<AgentResolutionGroup[]> =>
      request(`/mapping-agent/resolutions?session_id=${encodeURIComponent(session_id)}`),

    /** Put one field back the way it was before an agent changed it. */
    revertChange: (change_id: number): Promise<unknown> =>
      request(`/mapping-agent/changes/${change_id}/revert`, { method: 'POST' }),

    /** Undo every outstanding mapping change one agent run made, newest first. */
    revertRun: (run_id: string): Promise<{ run_id: string; reverted: number[]; count: number }> =>
      request(`/mapping-agent/runs/${encodeURIComponent(run_id)}/revert`, { method: 'POST' }),

    /**
     * Whether the source can supply every field and parent the destination
     * requires. Stores the report with validated suggestions; nothing is
     * written to a mapping or to Salesforce until one is applied.
     */
    readiness: (
      body: ReadinessCredentials & { source_type: string; selected_objects: string[] },
    ): Promise<ReadinessCheck> =>
      request('/mapping-agent/readiness', { method: 'POST', body: JSON.stringify(body) }),

    getReadiness: (readiness_id: number): Promise<ReadinessCheck> =>
      request(`/mapping-agent/readiness/${readiness_id}`),

    /** Run one approved suggestion: source writes, then Salesforce writes, then mappings. */
    applyReadiness: (
      readiness_id: number,
      gap: number,
      suggestion: number,
      credentials: ReadinessCredentials,
    ): Promise<ReadinessApplyResult> =>
      request(
        `/mapping-agent/readiness/${readiness_id}/apply?gap=${gap}&suggestion=${suggestion}`,
        { method: 'POST', body: JSON.stringify(credentials) },
      ),

    /** Delete a record a readiness suggestion created in Salesforce. */
    revertReadinessSfChange: (
      change_id: number,
      credentials: ReadinessCredentials,
    ): Promise<{ id: number; reverted: boolean; already: boolean }> =>
      request(`/mapping-agent/readiness/sf-changes/${change_id}/revert`, {
        method: 'POST',
        body: JSON.stringify(credentials),
      }),

    /** Empty again the source values a readiness suggestion wrote, where they are unchanged. */
    revertReadinessSourceChange: (
      change_id: number,
      credentials: ReadinessCredentials,
    ): Promise<{ id: number; reverted: boolean; already: boolean; restored: number; kept: number }> =>
      request(`/mapping-agent/readiness/source-changes/${change_id}/revert`, {
        method: 'POST',
        body: JSON.stringify(credentials),
      }),
  },

  health: (): Promise<{ status: string }> => request('/health'),
};
