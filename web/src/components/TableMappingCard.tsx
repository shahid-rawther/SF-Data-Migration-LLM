import { useState, useCallback } from 'react';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo } from '../api/client';
import { FieldMapper } from './FieldMapper';

export interface TableMappingState {
  sourceTable: string;
  sfObject: string;
  mappingName: string;
  operation: 'insert' | 'update' | 'upsert';
  fields: MappingFieldDef[];
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  fieldsLoaded: boolean;
}

interface Props {
  state: TableMappingState;
  sfObjects: { name: string; label: string }[];
  sourceType: string;
  sourceCreds: Record<string, string>;
  destCreds: Record<string, string>;
  onChange: (next: TableMappingState) => void;
}

export function TableMappingCard({
  state,
  sfObjects,
  sourceType,
  sourceCreds,
  destCreds,
  onChange,
}: Props) {
  const [loadingFields, setLoadingFields] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [expanded, setExpanded] = useState(true);

  const patch = useCallback(
    (p: Partial<TableMappingState>) => onChange({ ...state, ...p }),
    [state, onChange],
  );

  const loadFields = async () => {
    if (!state.sfObject) return;
    setLoadingFields(true);
    try {
      const [srcRes, dstRes] = await Promise.all([
        api.schema.sourceFields(sourceType, sourceCreds, { table: state.sourceTable }),
        api.schema.destinationFields(state.sfObject, destCreds),
      ]);
      patch({
        sourceFields: srcRes.fields,
        destFields: dstRes.fields,
        fieldsLoaded: true,
      });
    } catch (err) {
      console.error('Failed to load fields', err);
    } finally {
      setLoadingFields(false);
    }
  };

  const autoSuggest = async () => {
    if (!state.sfObject) return;
    setSuggesting(true);
    try {
      const result = await api.mappings.autoSuggest({
        source_type: sourceType,
        source_table: state.sourceTable,
        salesforce_object: state.sfObject,
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
      });
      patch({
        fields: result.fields ?? [],
        sourceFields:
          state.sourceFields.length > 0
            ? state.sourceFields
            : (await api.schema.sourceFields(sourceType, sourceCreds, { table: state.sourceTable }))
                .fields,
        destFields:
          state.destFields.length > 0
            ? state.destFields
            : (await api.schema.destinationFields(state.sfObject, destCreds)).fields,
        fieldsLoaded: true,
      });
    } catch (err) {
      console.error('Auto-suggest failed', err);
    } finally {
      setSuggesting(false);
    }
  };

  return (
    <div style={cardStyle}>
      {/* Header */}
      <div
        style={headerStyle}
        onClick={() => setExpanded((e) => !e)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && setExpanded((v) => !v)}
      >
        <span style={{ fontFamily: 'monospace', fontWeight: 600, fontSize: 14 }}>
          {state.sourceTable}
        </span>
        <span style={{ fontSize: 12, color: '#6b7280' }}>
          {state.sfObject ? `-> ${state.sfObject}` : '(not mapped)'}
          {' | '}
          {state.fields.length} field(s)
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 16, userSelect: 'none' }}>
          {expanded ? '\u25B2' : '\u25BC'}
        </span>
      </div>

      {expanded && (
        <div style={{ padding: 16 }}>
          {/* Controls row */}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 14, alignItems: 'end' }}>
            <div style={{ flex: '1 1 200px' }}>
              <label style={labelStyle}>Salesforce Object</label>
              <select
                value={state.sfObject}
                onChange={(e) =>
                  patch({ sfObject: e.target.value, fields: [], fieldsLoaded: false, sourceFields: [], destFields: [] })
                }
                style={selectStyle}
              >
                <option value="">-- select --</option>
                {sfObjects.map((o) => (
                  <option key={o.name} value={o.name}>
                    {o.label} ({o.name})
                  </option>
                ))}
              </select>
            </div>
            <div style={{ flex: '1 1 200px' }}>
              <label style={labelStyle}>Mapping Name</label>
              <input
                value={state.mappingName}
                onChange={(e) => patch({ mappingName: e.target.value })}
                style={inputStyle}
              />
            </div>
            <div style={{ flex: '0 0 130px' }}>
              <label style={labelStyle}>Operation</label>
              <select
                value={state.operation}
                onChange={(e) =>
                  patch({ operation: e.target.value as 'insert' | 'update' | 'upsert' })
                }
                style={selectStyle}
              >
                <option value="insert">Insert</option>
                <option value="update">Update</option>
                <option value="upsert">Upsert</option>
              </select>
            </div>
          </div>

          {/* Action buttons */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
            <button
              type="button"
              disabled={!state.sfObject || loadingFields}
              onClick={loadFields}
              style={actionBtn}
            >
              {loadingFields ? 'Loading...' : 'Load Fields'}
            </button>
            <button
              type="button"
              disabled={!state.sfObject || suggesting}
              onClick={autoSuggest}
              style={{ ...actionBtn, background: '#f0fdf4', color: '#16a34a', borderColor: '#bbf7d0' }}
            >
              {suggesting ? 'Suggesting...' : 'Auto-suggest'}
            </button>
          </div>

          {/* Field mapping table */}
          {state.fieldsLoaded && (
            <FieldMapper
              fields={state.fields}
              sourceFields={state.sourceFields}
              destFields={state.destFields}
              onChange={(f) => patch({ fields: f })}
            />
          )}

          {!state.fieldsLoaded && state.sfObject && (
            <p style={{ color: '#9ca3af', fontSize: 13, textAlign: 'center', padding: '16px 0' }}>
              Click "Load Fields" or "Auto-suggest" to configure field mappings.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

const cardStyle: React.CSSProperties = {
  border: '1px solid #e5e7eb',
  borderRadius: 10,
  overflow: 'hidden',
  marginBottom: 16,
  background: '#fff',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '12px 16px',
  background: '#f8fafc',
  cursor: 'pointer',
  borderBottom: '1px solid #e5e7eb',
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 12,
  fontWeight: 600,
  color: '#475569',
  marginBottom: 4,
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 10px',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
};

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  background: '#fff',
};

const actionBtn: React.CSSProperties = {
  padding: '6px 14px',
  borderRadius: 6,
  border: '1px solid #bfdbfe',
  background: '#eff6ff',
  color: '#2563eb',
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
};
