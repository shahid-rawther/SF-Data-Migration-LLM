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
  salesforce_object: string;
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
}

export interface CreateMigrationRequest {
  source_type: string;
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

  health: (): Promise<{ status: string }> => request('/health'),
};
