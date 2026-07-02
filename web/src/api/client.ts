// Typed API client for the ITSM Migration backend

const BASE = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
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
  source_credentials: Record<string, string>;
  destination_credentials: Record<string, string>;
  selected_objects: string[];
  options: MigrationOptions;
}

export interface MigrationOptions {
  start_date: string;
  end_date: string;
  service_now_batch_size: number;
  salesforce_batch_size: number;
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
}

export interface CreateMigrationRequest {
  source_type: string;
  destination_type?: string;
  source_credentials: Record<string, string>;
  destination_credentials: Record<string, string>;
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

export interface CompatibilityReport {
  source_type: string;
  overall_pct: number;
  objects: ObjectCompatibility[];
  summary: string;
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

export interface DestinationFieldInfo {
  name: string;
  label: string;
  type: string;
  length: number;
  required: boolean;
  createable: boolean;
  updateable: boolean;
  reference_to: string[];
  picklist_values: string[];
}

export interface MappingFieldDef {
  source_field?: string | string[];
  destination_field: string;
  transformation_function?: string;
  transformation_params?: Record<string, unknown>;
  use_constant?: string;
  skip_if_not_found?: boolean;
  // Present on auto-suggested fields only:
  confidence?: number;
  match_reason?: string;
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
  _meta?: {
    source_field_count: number;
    destination_field_count: number;
    suggested_count: number;
  };
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
    start: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/start`, { method: 'POST' }),
    pause: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/pause`, { method: 'POST' }),
    resume: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/resume`, { method: 'POST' }),
    stop: (id: string): Promise<{ session_id: string; status: string }> =>
      request(`/migrations/${id}/stop`, { method: 'POST' }),
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

  schema: {
    sourceTables: (
      source_type: string,
      credentials: Record<string, string>,
    ): Promise<{ source_type: string; tables: string[] }> =>
      request('/schema/source-tables', {
        method: 'POST',
        body: JSON.stringify({ source_type, credentials }),
      }),
    sourceFields: (
      source_type: string,
      credentials: Record<string, string>,
      opts: { object_name?: string; table?: string },
    ): Promise<{ source_type: string; fields: SourceFieldInfo[] }> =>
      request('/schema/source-fields', {
        method: 'POST',
        body: JSON.stringify({ source_type, credentials, ...opts }),
      }),
    destinationObjects: (
      credentials: Record<string, string>,
      destination_type = 'salesforce',
    ): Promise<{ destination_type: string; objects: { name: string; label: string; createable: boolean }[] }> =>
      request('/schema/destination-objects', {
        method: 'POST',
        body: JSON.stringify({ destination_type, credentials }),
      }),
    destinationFields: (
      object_name: string,
      credentials: Record<string, string>,
    ): Promise<{ object_name: string; fields: DestinationFieldInfo[] }> =>
      request('/schema/destination-fields', {
        method: 'POST',
        body: JSON.stringify({ object_name, credentials }),
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
    autoSuggest: (body: {
      source_type: string;
      source_table: string;
      salesforce_object: string;
      source_credentials: Record<string, string>;
      destination_credentials: Record<string, string>;
      other_mappings?: { source_table: string; sf_object: string; mapping_name: string }[];
    }): Promise<MappingConfig> =>
      request('/mappings/auto-suggest', { method: 'POST', body: JSON.stringify(body) }),
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
    list: (): Promise<TransformDef[]> => request('/transforms/'),
  },

  health: (): Promise<{ status: string }> => request('/health'),
};
