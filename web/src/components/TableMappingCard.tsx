import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { api } from '../api/client';
import type {
  MappingFieldDef,
  SourceFieldInfo,
  DestinationFieldInfo,
  SemanticSuggestReport,
} from '../api/client';
import { Disclosure } from './Disclosure';
import { FieldMapper } from './FieldMapper';
import { MoreMenu } from './MoreMenu';
import { PrerequisiteNotice } from './PrerequisiteNotice';
import { SearchableSelect } from './SearchableSelect';
import { SemanticMatchPanel } from './SemanticMatchPanel';
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
  // The saved mapping this card was read from or has written. Saving under any
  // other name that already exists would replace someone else's mapping.
  savedAs?: string;
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
  const [matching, setMatching] = useState(false);
  const [matchReport, setMatchReport] = useState<SemanticSuggestReport | null>(null);
  const [matchError, setMatchError] = useState('');
  const [fieldsError, setFieldsError] = useState('');
  const [expanded, setExpanded] = useState(true);

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

  const loadFields = async () => {
    if (!state.sourceTable) {
      setFieldsError('Pick a source table first.');
      return;
    }
    if (!state.sfObject) {
      // There is one card per source table, each with its own dropdown and its
      // own button, so name the table rather than saying "a destination object".
      setFieldsError(
        `This card ("${state.sourceTable}") has no Salesforce Object set. ` +
          'Choose one in the "Salesforce Object" dropdown just above.',
      );
      return;
    }
    setLoadingFields(true);
    setFieldsError('');
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
      // A describe that reaches the system but returns nothing is a 200 with an
      // empty list, so an unreadable table and a healthy one look the same from
      // here. Name whichever side came back empty rather than opening an empty
      // mapping table and leaving the reason to the console.
      const empty = [
        srcRes.fields.length === 0 ? `source table "${state.sourceTable}"` : '',
        dstRes.fields.length === 0 ? `destination object "${state.sfObject}"` : '',
      ].filter(Boolean);
      if (empty.length > 0) {
        setFieldsError(
          `No fields came back for ${empty.join(' or ')}. The connection answered, ` +
            'so this is usually a name that does not exist in that system, or ' +
            'credentials without read access to it.',
        );
      }
    } catch (err) {
      console.error('Failed to load fields', err);
      setFieldsError(err instanceof Error ? err.message : 'Failed to load fields');
    } finally {
      setLoadingFields(false);
    }
  };

  // Asks the model to pair the fields that are still unmapped, by meaning
  // rather than by name (u_caller_id -> ContactId). It appends its proposals
  // for review; nothing already on screen is overwritten.
  const semanticMatch = async () => {
    if (!state.sfObject) return;
    setMatching(true);
    setMatchError('');
    setMatchReport(null);
    try {
      const report = await api.mappings.semanticSuggest({
        source_type: sourceType,
        source_table: state.sourceTable,
        salesforce_object: state.sfObject,
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
        existing_fields: state.fields,
        other_mappings: otherMappings,
        operation: state.operation,
      });
      setMatchReport(report);
      if (report.error) setMatchError(report.error);
      const taken = new Set(state.fields.map((f) => f.destination_field.toLowerCase()));
      const additions = report.matches.filter(
        (m) => !taken.has(m.destination_field.toLowerCase()),
      );
      if (additions.length > 0) patch({ fields: [...state.fields, ...additions] });
    } catch (err) {
      console.error('Semantic match failed', err);
      setMatchError(err instanceof Error ? err.message : 'Semantic match failed');
    } finally {
      setMatching(false);
    }
  };

  const advancedHint = [
    state.operation.charAt(0).toUpperCase() + state.operation.slice(1),
    `${state.joins.length} join${state.joins.length === 1 ? '' : 's'}`,
    `${state.filters.length} filter${state.filters.length === 1 ? '' : 's'}`,
  ].join(' · ');

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
          <span style={{ fontSize: 13, color: state.sfObject ? '#404040' : '#a3a3a3' }}>
            {state.sfObject ? `\u2192 ${state.sfObject}` : 'no Salesforce object yet'}
          </span>
          <span style={{ fontSize: 12, color: '#a3a3a3' }}>
            · {state.fields.length} field{state.fields.length === 1 ? '' : 's'}
          </span>
        </div>

        <div style={{ marginLeft: 'auto', marginRight: 8 }}>
          <MoreMenu
            compact
            items={[
              {
                label: 'Move up',
                description: 'Load this object earlier',
                disabled: index === 0,
                onSelect: onMoveUp,
              },
              {
                label: 'Move down',
                description: 'Load this object later',
                disabled: index === total - 1,
                onSelect: onMoveDown,
              },
              {
                label: 'Also map to another object',
                description: 'Send this source table to a second Salesforce object',
                onSelect: onSplit,
              },
              {
                label: 'Remove',
                disabled: !canRemove,
                title: canRemove ? undefined : 'This is the only card for this source table',
                danger: true,
                onSelect: onRemove,
              },
            ]}
          />
        </div>

        <span
          style={{ fontSize: 12, color: '#737373', userSelect: 'none', cursor: 'pointer' }}
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? '\u25B2' : '\u25BC'}
        </span>
      </div>

      {expanded && (
        <div style={{ padding: 16 }}>
          <PrerequisiteNotice
            sourceType={sourceType}
            destCreds={destCreds}
            sfObject={state.sfObject}
            selectedObjects={otherMappings.map((m) => m.sf_object).filter(Boolean)}
            fields={state.fields}
            onAddTables={onAddTables}
          />

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
          </div>

          {state.sfObject && (
          <Disclosure
            label="Advanced: operation, joins & row filters"
            count={state.joins.length + state.filters.length}
            hint={advancedHint}
            defaultOpen={state.joins.length + state.filters.length > 0 || state.operation !== 'insert'}
            style={{ marginBottom: 14 }}
          >
            <div style={{ ...joinBoxStyle, display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#404040' }}>Operation</div>
                <div style={{ fontSize: 11, color: '#737373', marginTop: 2 }}>
                  Insert creates new records. Update and upsert need a key to match existing ones.
                </div>
              </div>
              <select
                value={state.operation}
                onChange={(e) =>
                  patch({ operation: e.target.value as 'insert' | 'update' | 'upsert' })
                }
                style={{ ...selectStyle, width: 130 }}
              >
                <option value="insert">Insert</option>
                <option value="update">Update</option>
                <option value="upsert">Upsert</option>
              </select>
            </div>

            <div style={joinBoxStyle}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#404040' }}>Related tables / Joins</div>
                  <div style={{ fontSize: 11, color: '#737373', marginTop: 2 }}>
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
                <div style={{ fontSize: 12, color: '#a3a3a3', padding: '4px 0' }}>
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
                  <span style={{ fontSize: 12, color: '#737373' }}>=</span>
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

            <div style={joinBoxStyle}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#404040' }}>Row filters</div>
                  <div style={{ fontSize: 11, color: '#737373', marginTop: 2 }}>
                    Only rows matching all filters are migrated.
                  </div>
                </div>
                <button type="button" onClick={addFilter} style={{ ...actionBtn, padding: '5px 12px' }}>
                  + Add filter
                </button>
              </div>

              {state.filters.length === 0 && (
                <div style={{ fontSize: 12, color: '#a3a3a3', padding: '4px 0' }}>
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
                      <span style={{ fontSize: 12, color: '#a3a3a3', flex: '1 1 130px' }}>
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
                      <span style={{ fontSize: 12, color: '#a3a3a3', flex: '1 1 120px' }}>(no value)</span>
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
          </Disclosure>
          )}

          {/* Action buttons */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
            {/* Stays clickable without a destination object so the click can say
                what is missing; a disabled button is indistinguishable from one
                that ran and found nothing. */}
            <button
              type="button"
              disabled={loadingFields}
              onClick={loadFields}
              style={actionBtn}
            >
              {loadingFields ? 'Loading...' : 'Load Fields'}
            </button>
            <button
              type="button"
              disabled={!state.sfObject || !state.fieldsLoaded || matching}
              onClick={semanticMatch}
              title={
                state.fieldsLoaded
                  ? 'Ask the model to pair the remaining fields by meaning rather than name'
                  : 'Load fields first'
              }
              style={{ ...actionBtn, background: '#fafafa', color: '#262626', borderColor: '#e5e5e5' }}
            >
              {matching ? 'Matching...' : 'AI match remaining'}
            </button>
          </div>

          {fieldsError && (
            <div
              style={{
                background: '#fafafa',
                border: '1px solid #e5e5e5',
                color: '#171717',
                borderRadius: 6,
                padding: '8px 10px',
                fontSize: 12,
                marginBottom: 12,
              }}
            >
              {fieldsError}
            </div>
          )}

          <SemanticMatchPanel report={matchReport} error={matchError} />

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
            <p style={{ color: '#a3a3a3', fontSize: 13, textAlign: 'center', padding: '16px 0' }}>
              Click "Load Fields" to configure field mappings.
            </p>
          )}
        </div>
      )}
    </div>
  );
}


const cardStyle: React.CSSProperties = {
  border: '1px solid #e5e5e5',
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
  background: '#fafafa',
  cursor: 'pointer',
  borderBottom: '1px solid #e5e5e5',
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 12,
  fontWeight: 600,
  color: '#525252',
  marginBottom: 4,
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 10px',
  border: '1px solid #d4d4d4',
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
  border: '1px solid #d4d4d4',
  background: '#f5f5f5',
  color: '#171717',
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
  background: '#171717',
  color: '#fff',
  flexShrink: 0,
};

const joinBoxStyle: React.CSSProperties = {
  padding: 12,
  marginBottom: 10,
  borderRadius: 8,
  background: '#fafafa',
  border: '1px solid #e5e5e5',
};

const joinRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  flexWrap: 'wrap',
  padding: '6px 0',
  borderTop: '1px solid #f5f5f5',
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
  color: '#404040',
  background: '#e5e5e5',
  borderRadius: 4,
  padding: '4px 8px',
};

const removeJoinBtn: React.CSSProperties = {
  width: 22,
  height: 22,
  padding: 0,
  borderRadius: 4,
  cursor: 'pointer',
  lineHeight: 1,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 11,
  color: '#0a0a0a',
  border: '1px solid #e5e5e5',
  background: '#fafafa',
  marginLeft: 'auto',
};

const smallGhostBtn: React.CSSProperties = {
  flex: '0 0 auto',
  padding: '5px 10px',
  borderRadius: 6,
  border: '1px solid #d4d4d4',
  background: '#fff',
  color: '#525252',
  cursor: 'pointer',
  fontSize: 11,
  fontWeight: 600,
  whiteSpace: 'nowrap',
};
