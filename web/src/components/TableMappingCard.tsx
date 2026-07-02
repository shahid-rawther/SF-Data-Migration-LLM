import { useState, useCallback, useMemo } from 'react';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo } from '../api/client';
import { FieldMapper } from './FieldMapper';

export interface TableMappingState {
  id: string;
  sourceTable: string;
  sfObject: string;
  mappingName: string;
  operation: 'insert' | 'update' | 'upsert';
  priority: number;
  fields: MappingFieldDef[];
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  fieldsLoaded: boolean;
}

interface Props {
  state: TableMappingState;
  index: number;
  total: number;
  sfObjects: { name: string; label: string }[];
  sourceType: string;
  sourceCreds: Record<string, string>;
  destCreds: Record<string, string>;
  selectedTables: string[];
  otherMappings: { source_table: string; sf_object: string; mapping_name: string }[];
  onChange: (next: TableMappingState) => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onAddTables: (tables: string[]) => void;
  onSplit: () => void;
  onRemove: () => void;
  canRemove: boolean;
}

export function TableMappingCard({
  state,
  index,
  total,
  sfObjects,
  sourceType,
  sourceCreds,
  destCreds,
  selectedTables,
  otherMappings,
  onChange,
  onMoveUp,
  onMoveDown,
  onAddTables,
  onSplit,
  onRemove,
  canRemove,
}: Props) {
  const [loadingFields, setLoadingFields] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [dismissedRefs, setDismissedRefs] = useState(false);
  const [checkedRefTables, setCheckedRefTables] = useState<Set<string>>(new Set());

  const patch = useCallback(
    (p: Partial<TableMappingState>) => onChange({ ...state, ...p }),
    [state, onChange],
  );

  // Detect reference fields that point to tables not in the current selection
  const missingRefTables = useMemo(() => {
    if (!state.fieldsLoaded || state.sourceFields.length === 0) return new Map<string, string[]>();
    const missing = new Map<string, string[]>();
    for (const f of state.sourceFields) {
      if (f.field_type === 'reference' && f.reference_table && !selectedTables.includes(f.reference_table)) {
        const shortName = f.name.includes('.') ? f.name.split('.').pop()! : f.name;
        const existing = missing.get(f.reference_table) ?? [];
        existing.push(shortName);
        missing.set(f.reference_table, existing);
      }
    }
    return missing;
  }, [state.sourceFields, state.fieldsLoaded, selectedTables]);

  const loadFields = async () => {
    if (!state.sfObject) return;
    setLoadingFields(true);
    setDismissedRefs(false);
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
    setDismissedRefs(false);
    try {
      const result = await api.mappings.autoSuggest({
        source_type: sourceType,
        source_table: state.sourceTable,
        salesforce_object: state.sfObject,
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
        other_mappings: otherMappings,
      });
      const srcFields =
        state.sourceFields.length > 0
          ? state.sourceFields
          : (await api.schema.sourceFields(sourceType, sourceCreds, { table: state.sourceTable }))
              .fields;
      patch({
        fields: result.fields ?? [],
        sourceFields: srcFields,
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
      <div style={headerStyle}>
        {/* Priority badge */}
        <span style={priorityBadge}>{index + 1}</span>

        <div
          style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, cursor: 'pointer' }}
          onClick={() => setExpanded((e) => !e)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => e.key === 'Enter' && setExpanded((v) => !v)}
        >
          <span style={{ fontFamily: 'monospace', fontWeight: 600, fontSize: 14 }}>
            {state.sourceTable}
          </span>
          <span style={{ fontSize: 12, color: '#6b7280' }}>
            {state.sfObject ? `${'->'}  ${state.sfObject}` : '(not mapped)'}
            {' | '}
            {state.fields.length} field(s)
          </span>
        </div>

        {/* Reorder & split buttons */}
        <div style={{ display: 'flex', gap: 2, marginLeft: 'auto', marginRight: 8 }}>
          <button
            type="button"
            disabled={index === 0}
            onClick={(e) => { e.stopPropagation(); onMoveUp(); }}
            style={reorderBtn}
            title="Move up (higher priority)"
          >
            &#9650;
          </button>
          <button
            type="button"
            disabled={index === total - 1}
            onClick={(e) => { e.stopPropagation(); onMoveDown(); }}
            style={reorderBtn}
            title="Move down (lower priority)"
          >
            &#9660;
          </button>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onSplit(); }}
            style={splitBtn}
            title="Split: map this source table to an additional Salesforce object"
          >
            &#8621;
          </button>
          <button
            type="button"
            disabled={!canRemove}
            onClick={(e) => { e.stopPropagation(); onRemove(); }}
            style={{
              ...removeCardBtn,
              opacity: canRemove ? 1 : 0.35,
              cursor: canRemove ? 'pointer' : 'not-allowed',
            }}
            title={canRemove ? 'Remove this mapping card' : 'Cannot remove the only card for this source table'}
          >
            &#10005;
          </button>
        </div>

        <span
          style={{ fontSize: 16, userSelect: 'none', cursor: 'pointer' }}
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? '\u25B2' : '\u25BC'}
        </span>
      </div>

      {expanded && (
        <div style={{ padding: 16 }}>
          {/* Missing reference tables warning */}
          {missingRefTables.size > 0 && !dismissedRefs && (
            <div style={refBannerStyle}>
              <div style={{ fontSize: 13, fontWeight: 600, color: '#92400e', marginBottom: 6 }}>
                This table references {missingRefTables.size} table(s) not in your selection:
              </div>
              {Array.from(missingRefTables.entries()).map(([table, fields]) => (
                <label key={table} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#78350f', marginBottom: 4, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={checkedRefTables.has(table)}
                    onChange={(e) => {
                      setCheckedRefTables((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) next.add(table); else next.delete(table);
                        return next;
                      });
                    }}
                  />
                  <strong>{table}</strong>
                  <span style={{ color: '#92400e' }}>(used by: {fields.join(', ')})</span>
                </label>
              ))}
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                <button
                  type="button"
                  disabled={checkedRefTables.size === 0}
                  onClick={() => {
                    onAddTables(Array.from(checkedRefTables));
                    setCheckedRefTables(new Set());
                  }}
                  style={{
                    ...actionBtn,
                    background: checkedRefTables.size > 0 ? '#fef3c7' : '#f3f4f6',
                    color: checkedRefTables.size > 0 ? '#92400e' : '#9ca3af',
                    borderColor: checkedRefTables.size > 0 ? '#fcd34d' : '#d1d5db',
                    cursor: checkedRefTables.size > 0 ? 'pointer' : 'not-allowed',
                  }}
                >
                  Add Selected ({checkedRefTables.size})
                </button>
                <button
                  type="button"
                  onClick={() => setDismissedRefs(true)}
                  style={{ ...actionBtn, background: '#fff', color: '#6b7280', borderColor: '#d1d5db' }}
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}

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
              sfObject={state.sfObject}
              destCreds={destCreds}
              onDestFieldsChange={(df) => patch({ destFields: df })}
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

const priorityBadge: React.CSSProperties = {
  width: 24,
  height: 24,
  borderRadius: '50%',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 11,
  fontWeight: 700,
  background: '#2563eb',
  color: '#fff',
  flexShrink: 0,
};

const reorderBtn: React.CSSProperties = {
  width: 22,
  height: 22,
  padding: 0,
  border: '1px solid #d1d5db',
  borderRadius: 4,
  background: '#fff',
  cursor: 'pointer',
  fontSize: 10,
  lineHeight: 1,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};

const splitBtn: React.CSSProperties = {
  ...reorderBtn,
  fontSize: 13,
  color: '#2563eb',
  border: '1px solid #bfdbfe',
  background: '#eff6ff',
  marginLeft: 4,
};

const removeCardBtn: React.CSSProperties = {
  ...reorderBtn,
  fontSize: 11,
  color: '#dc2626',
  border: '1px solid #fecaca',
  background: '#fef2f2',
  marginLeft: 4,
};

const refBannerStyle: React.CSSProperties = {
  padding: 12,
  marginBottom: 14,
  borderRadius: 8,
  background: '#fffbeb',
  border: '1px solid #fcd34d',
};
