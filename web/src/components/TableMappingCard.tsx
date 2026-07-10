import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo } from '../api/client';
import { FieldMapper } from './FieldMapper';
import { SearchableSelect } from './SearchableSelect';
import type { SelectOption } from './SearchableSelect';

export type JoinType = 'left' | 'right' | 'inner' | 'full';

export interface JoinDef {
  left_table: string;
  left_key: string;
  right_table: string;
  right_key: string;
  type: JoinType;
}

export type FilterOperator =
  | 'equals'
  | 'not_equals'
  | 'in'
  | 'not_in'
  | 'is_null'
  | 'is_not_null'
  | 'count';

export interface FilterDef {
  table: string;
  field: string;
  operator: FilterOperator;
  // string for equals/not_equals; string[] for in/not_in; unused for null ops.
  value?: string | string[];
  // used only by the 'count' (limit rows) operator.
  count?: number;
}

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
  // Extra tables joined to the driving table (sourceTable) and the join conditions.
  joinTables: string[];
  joins: JoinDef[];
  // Row filters: only source rows matching all filters are migrated.
  filters: FilterDef[];
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

  // Per-table raw field options (unprefixed names) used to populate the join
  // key dropdowns. Fetched lazily and cached; loadedTablesRef guards re-fetches.
  const [tableFields, setTableFields] = useState<Record<string, SelectOption[]>>({});
  const loadedTablesRef = useRef<Set<string>>(new Set());

  const fetchTableFields = useCallback(
    async (table: string) => {
      if (!table || loadedTablesRef.current.has(table)) return;
      loadedTablesRef.current.add(table);
      try {
        const res = await api.schema.sourceFields(sourceType, sourceCreds, { table });
        const opts: SelectOption[] = res.fields.map((f) => {
          const short = f.name.includes('.') ? f.name.split('.').pop()! : f.name;
          return { value: short, label: `${f.label} (${short})` };
        });
        setTableFields((prev) => ({ ...prev, [table]: opts }));
      } catch (err) {
        loadedTablesRef.current.delete(table);
        console.error('Failed to load fields for join table', table, err);
      }
    },
    [sourceType, sourceCreds],
  );

  // Keep field options loaded for the driving table and any join tables.
  useEffect(() => {
    fetchTableFields(state.sourceTable);
    for (const t of state.joinTables) fetchTableFields(t);
  }, [state.sourceTable, state.joinTables, fetchTableFields]);

  // Fetch the driving-table fields, then merge each join table's fields in with
  // a table-prefixed name (e.g. "core_company.name") so mappings can reference
  // joined columns. Driving-table fields keep their plain names.
  const buildMergedSourceFields = useCallback(
    async (drivingFields: SourceFieldInfo[]): Promise<SourceFieldInfo[]> => {
      const extra: SourceFieldInfo[] = [];
      for (const jt of state.joinTables) {
        try {
          const res = await api.schema.sourceFields(sourceType, sourceCreds, { table: jt });
          for (const f of res.fields) {
            const short = f.name.includes('.') ? f.name.split('.').pop()! : f.name;
            extra.push({ ...f, name: `${jt}.${short}` });
          }
        } catch (err) {
          console.error('Failed to merge fields for join table', jt, err);
        }
      }
      return [...drivingFields, ...extra];
    },
    [state.joinTables, sourceType, sourceCreds],
  );

  const availableJoinTables = useMemo(
    () =>
      selectedTables
        .filter((t) => t !== state.sourceTable && !state.joinTables.includes(t))
        .map((t) => ({ value: t, label: t })),
    [selectedTables, state.sourceTable, state.joinTables],
  );

  const joinTableOptions = useMemo<SelectOption[]>(
    () => [state.sourceTable, ...state.joinTables].map((t) => ({ value: t, label: t })),
    [state.sourceTable, state.joinTables],
  );

  const addJoinTable = (table: string) => {
    if (!table || state.joinTables.includes(table)) return;
    fetchTableFields(table);
    fetchTableFields(state.sourceTable);
    patch({
      joinTables: [...state.joinTables, table],
      joins: [
        ...state.joins,
        {
          left_table: state.sourceTable,
          left_key: '',
          right_table: table,
          right_key: '',
          type: 'left',
        },
      ],
    });
  };

  const removeJoin = (index: number) => {
    const removed = state.joins[index];
    const nextJoins = state.joins.filter((_, i) => i !== index);
    // Drop the join table only if no remaining join still references it.
    const stillUsed = nextJoins.some(
      (j) => j.right_table === removed.right_table || j.left_table === removed.right_table,
    );
    patch({
      joins: nextJoins,
      joinTables: stillUsed
        ? state.joinTables
        : state.joinTables.filter((t) => t !== removed.right_table),
    });
  };

  const updateJoin = (index: number, p: Partial<JoinDef>) => {
    patch({ joins: state.joins.map((j, i) => (i === index ? { ...j, ...p } : j)) });
  };

  // Distinct source values for the filter value picker, cached per "table.field".
  const [filterValueOptions, setFilterValueOptions] = useState<Record<string, SelectOption[]>>({});
  const [loadingValuesKey, setLoadingValuesKey] = useState<string | null>(null);

  const loadFilterValues = useCallback(
    async (table: string, field: string) => {
      if (!table || !field) return;
      const key = `${table}.${field}`;
      setLoadingValuesKey(key);
      try {
        const res = await api.schema.sourceValues(sourceType, sourceCreds, { table, field, limit: 200 });
        const opts: SelectOption[] = res.values.map((v) => ({ value: v.value, label: v.label }));
        setFilterValueOptions((prev) => ({ ...prev, [key]: opts }));
      } catch (err) {
        console.error('Failed to load values for', table, field, err);
      } finally {
        setLoadingValuesKey(null);
      }
    },
    [sourceType, sourceCreds],
  );

  const addFilter = () => {
    fetchTableFields(state.sourceTable);
    patch({
      filters: [
        ...state.filters,
        { table: state.sourceTable, field: '', operator: 'equals' as FilterOperator, value: '' },
      ],
    });
  };

  const removeFilter = (index: number) => {
    patch({ filters: state.filters.filter((_, i) => i !== index) });
  };

  const updateFilter = (index: number, p: Partial<FilterDef>) => {
    patch({ filters: state.filters.map((f, i) => (i === index ? { ...f, ...p } : f)) });
  };

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
      const merged = await buildMergedSourceFields(srcRes.fields);
      patch({
        sourceFields: merged,
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
          : await buildMergedSourceFields(
              (await api.schema.sourceFields(sourceType, sourceCreds, { table: state.sourceTable }))
                .fields,
            );
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

          {/* Related tables / Joins */}
          {state.sfObject && (
            <div style={joinBoxStyle}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#334155' }}>Related tables / Joins</div>
                  <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
                    Pull fields from related tables. Use columns whose names differ from the main table.
                  </div>
                </div>
                <div style={{ minWidth: 220 }}>
                  <SearchableSelect
                    value=""
                    options={availableJoinTables}
                    placeholder="+ add related table"
                    onChange={addJoinTable}
                  />
                </div>
              </div>

              {state.joins.length === 0 && (
                <div style={{ fontSize: 12, color: '#94a3b8', padding: '4px 0' }}>
                  No joins. The mapping uses only <strong>{state.sourceTable}</strong>.
                </div>
              )}

              {state.joins.map((join, i) => (
                <div key={i} style={joinRowStyle}>
                  <select
                    value={join.left_table}
                    onChange={(e) => updateJoin(i, { left_table: e.target.value, left_key: '' })}
                    style={joinSelect}
                    title="Left table"
                  >
                    {joinTableOptions
                      .filter((o) => o.value !== join.right_table)
                      .map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                  </select>
                  <div style={{ flex: '1 1 130px', minWidth: 120 }}>
                    <SearchableSelect
                      value={join.left_key}
                      options={tableFields[join.left_table] ?? []}
                      placeholder="left key"
                      onChange={(v) => updateJoin(i, { left_key: v })}
                    />
                  </div>
                  <span style={{ fontSize: 12, color: '#64748b' }}>=</span>
                  <span style={{ ...joinTableChip }} title="Right table">{join.right_table}</span>
                  <div style={{ flex: '1 1 130px', minWidth: 120 }}>
                    <SearchableSelect
                      value={join.right_key}
                      options={tableFields[join.right_table] ?? []}
                      placeholder="right key"
                      onChange={(v) => updateJoin(i, { right_key: v })}
                    />
                  </div>
                  <select
                    value={join.type}
                    onChange={(e) => updateJoin(i, { type: e.target.value as JoinType })}
                    style={{ ...joinSelect, flex: '0 0 80px' }}
                    title="Join type"
                  >
                    <option value="left">Left</option>
                    <option value="right">Right</option>
                    <option value="inner">Inner</option>
                    <option value="full">Full</option>
                  </select>
                  <button
                    type="button"
                    onClick={() => removeJoin(i)}
                    style={removeJoinBtn}
                    title="Remove this join"
                  >
                    &#10005;
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Row filters */}
          {state.sfObject && (
            <div style={joinBoxStyle}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#334155' }}>Row filters</div>
                  <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
                    Only rows matching all filters are migrated.
                  </div>
                </div>
                <button type="button" onClick={addFilter} style={{ ...actionBtn, padding: '5px 12px' }}>
                  + Add filter
                </button>
              </div>

              {state.filters.length === 0 && (
                <div style={{ fontSize: 12, color: '#94a3b8', padding: '4px 0' }}>
                  No filters. All rows from <strong>{state.sourceTable}</strong> are migrated.
                </div>
              )}

              {state.filters.map((filter, i) => {
                const needsField = filter.operator !== 'count';
                const isNullOp = filter.operator === 'is_null' || filter.operator === 'is_not_null';
                const isListOp = filter.operator === 'in' || filter.operator === 'not_in';
                const isCount = filter.operator === 'count';
                const valueKey = `${filter.table}.${filter.field}`;
                const pickerOptions = filterValueOptions[valueKey] ?? [];
                const listText = Array.isArray(filter.value) ? filter.value.join(', ') : '';
                return (
                  <div key={i} style={joinRowStyle}>
                    <select
                      value={filter.table}
                      onChange={(e) => updateFilter(i, { table: e.target.value, field: '' })}
                      style={joinSelect}
                      title="Table"
                    >
                      {joinTableOptions.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>

                    {needsField ? (
                      <div style={{ flex: '1 1 130px', minWidth: 120 }}>
                        <SearchableSelect
                          value={filter.field}
                          options={tableFields[filter.table] ?? []}
                          placeholder="field"
                          onChange={(v) => updateFilter(i, { field: v })}
                        />
                      </div>
                    ) : (
                      <span style={{ fontSize: 12, color: '#94a3b8', flex: '1 1 130px' }}>
                        (applies to whole result)
                      </span>
                    )}

                    <select
                      value={filter.operator}
                      onChange={(e) => {
                        const nextOp = e.target.value as FilterOperator;
                        // Reset value to the right shape for the new operator.
                        const nextValue =
                          nextOp === 'in' || nextOp === 'not_in'
                            ? []
                            : nextOp === 'equals' || nextOp === 'not_equals'
                              ? ''
                              : undefined;
                        updateFilter(i, {
                          operator: nextOp,
                          value: nextValue,
                          count: nextOp === 'count' ? (filter.count ?? 10) : undefined,
                        });
                      }}
                      style={{ ...joinSelect, flex: '0 0 110px' }}
                      title="Operator"
                    >
                      <option value="equals">Equals</option>
                      <option value="not_equals">Not equals</option>
                      <option value="in">In</option>
                      <option value="not_in">Not in</option>
                      <option value="is_null">Is null</option>
                      <option value="is_not_null">Is not null</option>
                      <option value="count">Limit rows</option>
                    </select>

                    {/* Value entry */}
                    {isNullOp ? (
                      <span style={{ fontSize: 12, color: '#94a3b8', flex: '1 1 120px' }}>(no value)</span>
                    ) : isCount ? (
                      <input
                        type="number"
                        min={1}
                        value={filter.count ?? ''}
                        placeholder="max rows"
                        onChange={(e) =>
                          updateFilter(i, { count: e.target.value ? Math.max(1, parseInt(e.target.value, 10)) : undefined })
                        }
                        style={{ ...inputStyle, flex: '0 0 100px', fontSize: 12, padding: '5px 8px' }}
                      />
                    ) : isListOp ? (
                      <div style={{ display: 'flex', gap: 6, flex: '1 1 260px', minWidth: 200, alignItems: 'center' }}>
                        <input
                          value={listText}
                          placeholder="value1, value2 (or paste ids)"
                          onChange={(e) =>
                            updateFilter(i, {
                              value: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                            })
                          }
                          style={{ ...inputStyle, flex: 1, fontSize: 12, padding: '5px 8px' }}
                          title="Comma-separated values"
                        />
                        <div style={{ flex: '0 0 130px' }}>
                          <SearchableSelect
                            value=""
                            options={pickerOptions}
                            placeholder={loadingValuesKey === valueKey ? 'loading...' : '+ pick'}
                            onChange={(v) => {
                              const current = Array.isArray(filter.value) ? filter.value : [];
                              if (v && !current.includes(v)) updateFilter(i, { value: [...current, v] });
                            }}
                          />
                        </div>
                        <button
                          type="button"
                          onClick={() => loadFilterValues(filter.table, filter.field)}
                          disabled={!filter.field || loadingValuesKey === valueKey}
                          style={{ ...smallGhostBtn }}
                          title="Load distinct values from the source"
                        >
                          Load values
                        </button>
                      </div>
                    ) : (
                      <div style={{ display: 'flex', gap: 6, flex: '1 1 260px', minWidth: 200, alignItems: 'center' }}>
                        <input
                          value={typeof filter.value === 'string' ? filter.value : ''}
                          placeholder="value"
                          onChange={(e) => updateFilter(i, { value: e.target.value })}
                          style={{ ...inputStyle, flex: 1, fontSize: 12, padding: '5px 8px' }}
                        />
                        <div style={{ flex: '0 0 130px' }}>
                          <SearchableSelect
                            value={typeof filter.value === 'string' ? filter.value : ''}
                            options={pickerOptions}
                            placeholder={loadingValuesKey === valueKey ? 'loading...' : '+ pick'}
                            onChange={(v) => updateFilter(i, { value: v })}
                          />
                        </div>
                        <button
                          type="button"
                          onClick={() => loadFilterValues(filter.table, filter.field)}
                          disabled={!filter.field || loadingValuesKey === valueKey}
                          style={{ ...smallGhostBtn }}
                          title="Load distinct values from the source"
                        >
                          Load values
                        </button>
                      </div>
                    )}

                    <button
                      type="button"
                      onClick={() => removeFilter(i)}
                      style={removeJoinBtn}
                      title="Remove this filter"
                    >
                      &#10005;
                    </button>
                  </div>
                );
              })}
            </div>
          )}

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
              sourceType={sourceType}
              sourceCreds={sourceCreds}
              sourceTable={state.sourceTable}
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

const joinBoxStyle: React.CSSProperties = {
  padding: 12,
  marginBottom: 14,
  borderRadius: 8,
  background: '#f8fafc',
  border: '1px solid #e2e8f0',
};

const joinRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  flexWrap: 'wrap',
  padding: '6px 0',
  borderTop: '1px solid #eef2f7',
};

const joinSelect: React.CSSProperties = {
  ...inputStyle,
  width: 'auto',
  flex: '0 0 120px',
  background: '#fff',
  fontSize: 12,
  padding: '5px 8px',
};

const joinTableChip: React.CSSProperties = {
  fontFamily: 'monospace',
  fontSize: 12,
  fontWeight: 600,
  color: '#334155',
  background: '#e2e8f0',
  borderRadius: 4,
  padding: '4px 8px',
};

const removeJoinBtn: React.CSSProperties = {
  ...reorderBtn,
  fontSize: 11,
  color: '#dc2626',
  border: '1px solid #fecaca',
  background: '#fef2f2',
  marginLeft: 'auto',
};

const smallGhostBtn: React.CSSProperties = {
  flex: '0 0 auto',
  padding: '5px 10px',
  borderRadius: 6,
  border: '1px solid #cbd5e1',
  background: '#fff',
  color: '#475569',
  cursor: 'pointer',
  fontSize: 11,
  fontWeight: 600,
  whiteSpace: 'nowrap',
};
