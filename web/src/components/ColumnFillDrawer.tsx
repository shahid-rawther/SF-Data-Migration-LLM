import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { SearchableSelect } from './SearchableSelect';
import type { SelectOption } from './SearchableSelect';
import {
  COLUMN_FILL_OPERATORS,
  COLUMN_FILL_OPERATORS_WITHOUT_VALUE,
} from '../api/client';
import type {
  ColumnFillMode,
  ColumnFillOperator,
  ColumnFillPreview,
  ColumnFillRule,
  ColumnFillSpec,
  ColumnFillTarget,
  ColumnFillUnmatched,
  DestinationFieldInfo,
  SourceFieldInfo,
  TransformDef,
  ValueMapSuggestReport,
} from '../api/client';

interface Props {
  sessionId: string;
  sourceType: string;
  sourceCreds: Record<string, string>;
  destCreds: Record<string, string>;
  sourceConnectionId?: string;
  destConnectionId?: string;
  open: boolean;
  onClose: () => void;
  /** Called after a fill has run, so the page can re-read what changed. */
  onFilled: () => void;
}

/** How each operator reads in the rule builder. */
const OPERATOR_LABELS: Record<ColumnFillOperator, string> = {
  equals: 'is',
  not_equals: 'is not',
  contains: 'contains',
  not_contains: 'does not contain',
  starts_with: 'starts with',
  ends_with: 'ends with',
  is_empty: 'is empty',
  is_not_empty: 'has any value',
  in: 'is one of',
  not_in: 'is none of',
  matches: 'matches pattern',
  gt: 'is greater than',
  gte: 'is at least',
  lt: 'is less than',
  lte: 'is at most',
};

const emptyRule = (): ColumnFillRule => ({
  all: [{ field: '', op: 'equals', value: '' }],
  then: { value: '' },
});

/**
 * Filling one column on records that have already been migrated.
 *
 * Three sections in one scroll rather than a wizard: the choices are few enough
 * that stepping through them would add clicks without adding clarity, and seeing
 * the preview next to the rules that produced it is the point.
 *
 * Preview before apply is enforced rather than encouraged. This writes over
 * whatever is already in the column on records that are live in the destination,
 * so the counts have to have been looked at at least once.
 */
export function ColumnFillDrawer({
  sessionId,
  sourceType,
  sourceCreds,
  destCreds,
  sourceConnectionId,
  destConnectionId,
  open,
  onClose,
  onFilled,
}: Props) {
  const [targets, setTargets] = useState<ColumnFillTarget[]>([]);
  const [mappingName, setMappingName] = useState('');
  const [field, setField] = useState('');
  const [mode, setMode] = useState<ColumnFillMode>('constant');

  const [destFields, setDestFields] = useState<DestinationFieldInfo[]>([]);
  const [sourceFields, setSourceFields] = useState<SourceFieldInfo[]>([]);
  const [transforms, setTransforms] = useState<TransformDef[]>([]);

  const [constantValue, setConstantValue] = useState('');
  const [mapSourceField, setMapSourceField] = useState('');
  const [valueMap, setValueMap] = useState<Record<string, string>>({});
  const [sourceValues, setSourceValues] = useState<string[]>([]);
  const [rules, setRules] = useState<ColumnFillRule[]>([emptyRule()]);
  const [unmatched, setUnmatched] = useState<ColumnFillUnmatched>('skip');
  const [unmatchedValue, setUnmatchedValue] = useState('');
  const [saveToMapping, setSaveToMapping] = useState(false);

  const [preview, setPreview] = useState<ColumnFillPreview | null>(null);
  /**
   * What the spec looked like when the preview was taken. Held rather than
   * cleared on every edit, so "you changed something since previewing" is
   * derived from the spec itself instead of an effect racing the edits.
   */
  const [previewedAs, setPreviewedAs] = useState('');
  const [aiReport, setAiReport] = useState<ValueMapSuggestReport | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState('');

  const target = useMemo(
    () => targets.find((t) => t.mapping_name === mappingName),
    [targets, mappingName],
  );
  const destMeta = useMemo(
    () => destFields.find((f) => f.name === field),
    [destFields, field],
  );

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    api.migrations.columnFill
      .targets(sessionId)
      .then((r) => {
        setTargets(r.targets);
        if (r.targets.length === 1) setMappingName(r.targets[0].mapping_name);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : 'Could not read this session'),
      );
    api.transforms.list(sourceType).then(setTransforms).catch(() => {});
  }, [open, sessionId, sourceType]);

  // Describing the destination is what makes a picklist column offer its allowed
  // values instead of a free-text box, so it is read as soon as an object is
  // chosen rather than when a column is.
  useEffect(() => {
    if (!open || !target?.destination_object) {
      setDestFields([]);
      return;
    }
    let live = true;
    api.schema
      .destinationFields(target.destination_object, destCreds, {
        connection_id: destConnectionId,
      })
      .then((r) => {
        if (live) setDestFields(r.fields);
      })
      .catch((e) => {
        if (live) {
          setError(
            e instanceof Error
              ? e.message
              : `Could not describe ${target.destination_object}`,
          );
        }
      });
    return () => {
      live = false;
    };
  }, [open, target?.destination_object, destCreds, destConnectionId]);

  useEffect(() => {
    if (!open || !target?.source_table) {
      setSourceFields([]);
      return;
    }
    let live = true;
    api.schema
      .sourceFields(sourceType, sourceCreds, {
        table: target.source_table,
        connection_id: sourceConnectionId,
      })
      .then((r) => {
        if (live) setSourceFields(r.fields);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [open, target?.source_table, sourceType, sourceCreds, sourceConnectionId]);

  const spec: ColumnFillSpec | null = useMemo(() => {
    if (!mappingName || !field) return null;
    const base: ColumnFillSpec = {
      mapping_name: mappingName,
      field,
      mode,
      unmatched,
      save_to_mapping: saveToMapping,
    };
    if (unmatched === 'value') base.unmatched_value = unmatchedValue;
    if (mode === 'constant') return { ...base, constant_value: constantValue };
    if (mode === 'value_map') {
      return {
        ...base,
        source_field: mapSourceField,
        value_map: Object.fromEntries(
          Object.entries(valueMap).filter(([, v]) => v),
        ),
      };
    }
    return { ...base, rules };
  }, [
    mappingName, field, mode, unmatched, unmatchedValue, saveToMapping,
    constantValue, mapSourceField, valueMap, rules,
  ]);

  // Everything that decides what gets written. Whether it is also stored in the
  // mapping is left out, because ticking that box does not change the values and
  // should not send anyone back to preview again.
  const signature = useMemo(
    () => (spec ? JSON.stringify({ ...spec, save_to_mapping: false }) : ''),
    [spec],
  );
  const previewStale = Boolean(preview) && signature !== previewedAs;

  const loadSourceValues = useCallback(
    async (column: string) => {
      if (!target?.source_table || !column) return;
      setBusy('values');
      try {
        const r = await api.schema.sourceValues(sourceType, sourceCreds, {
          table: target.source_table,
          field: column.split('.').pop() ?? column,
          connection_id: sourceConnectionId,
        });
        const values = r.values.map((v) => v.value).filter(Boolean);
        setSourceValues(values);
        setValueMap((prev) =>
          Object.fromEntries(values.map((v) => [v, prev[v] ?? ''])),
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Could not read the source values');
      } finally {
        setBusy('');
      }
    },
    [target?.source_table, sourceType, sourceCreds, sourceConnectionId],
  );

  const aiMatch = async () => {
    if (!target || !mapSourceField || !field) return;
    setBusy('ai');
    setError('');
    setAiReport(null);
    try {
      const report = await api.mappings.suggestValueMap({
        source_type: sourceType,
        source_table: target.source_table,
        source_field: mapSourceField,
        salesforce_object: target.destination_object,
        destination_field: field,
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
        source_connection_id: sourceConnectionId,
        destination_connection_id: destConnectionId,
        existing_fields: [],
      });
      setAiReport(report);
      if (report.matches.length) {
        setValueMap((prev) => {
          const next = { ...prev };
          for (const m of report.matches) next[m.source_value] = m.destination_value;
          return next;
        });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Value matching failed');
    } finally {
      setBusy('');
    }
  };

  const runPreview = async () => {
    if (!spec) return;
    setBusy('preview');
    setError('');
    setDone('');
    try {
      setPreview(await api.migrations.columnFill.preview(sessionId, spec));
      setPreviewedAs(signature);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not preview the fill');
    } finally {
      setBusy('');
    }
  };

  const apply = async () => {
    if (!spec || !preview || previewStale) return;
    setBusy('apply');
    setError('');
    try {
      const job = await api.migrations.columnFill.create(
        sessionId,
        spec,
        preview.rows_to_write,
      );
      const ran = await api.migrations.columnFill.run(job.id);
      setDone(ran.summary);
      onFilled();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The fill did not run');
    } finally {
      setBusy('');
    }
  };

  const objectOptions: SelectOption[] = targets.map((t) => ({
    value: t.mapping_name,
    label: `${t.destination_object} — ${t.loaded.toLocaleString()} record${t.loaded === 1 ? '' : 's'} (${t.mapping_name})`,
  }));

  const columnOptions: SelectOption[] = destFields
    .filter((f) => f.updateable !== false)
    .map((f) => ({
      value: f.name,
      label: `${f.name}${f.picklist_values.length ? '  ·  picklist' : `  ·  ${f.type}`}${f.required ? '  ·  required' : ''}`,
    }));

  const sourceOptions: SelectOption[] = useMemo(() => {
    const table = target?.source_table ?? '';
    return sourceFields.map((f) => {
      const short = f.name.includes('.') ? f.name.split('.').pop()! : f.name;
      // A reference column holds an id rather than anything readable, which is
      // worth knowing before writing a condition against it.
      const kind = f.reference_table ? `ref → ${f.reference_table}` : f.field_type;
      return {
        value: `${table}.${short}`,
        label: `${short}${kind ? `  ·  ${kind}` : ''}`,
      };
    });
  }, [sourceFields, target?.source_table]);

  const outputTransforms = useMemo(
    () => transforms.filter((t) => !t.builder_only && t.name !== 'generic_lookup'),
    [transforms],
  );

  if (!open) return null;

  const picklistOptions = destMeta?.picklist_values ?? [];
  const restricted = Boolean(destMeta?.picklist_restricted);
  const ready = Boolean(spec);

  return (
    <>
      <div style={backdrop} onClick={onClose} />
      <aside style={drawer} role="dialog" aria-label="Fill a column">
        <header style={head}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: '#171717' }}>
              Fill a column
            </div>
            <div style={{ fontSize: 12, color: '#737373', marginTop: 2 }}>
              Writes one column on records this session already migrated
            </div>
          </div>
          <button type="button" onClick={onClose} style={closeBtn} aria-label="Close">
            ✕
          </button>
        </header>

        <div style={body}>
          {error && <div style={errorBox}>{error}</div>}
          {done && <div style={doneBox}>{done}</div>}

          {!targets.length && !error && (
            <p style={muted}>
              Nothing in this session has loaded records yet, so there is no column to
              fill. Run an object first.
            </p>
          )}

          {targets.length > 0 && (
            <>
              {/* ── 1. What to fill ───────────────────────────────────── */}
              <section style={section}>
                <h4 style={sectionTitle}>1 · What to fill</h4>
                <label style={label}>Object</label>
                <SearchableSelect
                  value={mappingName}
                  options={objectOptions}
                  placeholder="-- choose a migrated object --"
                  onChange={(v) => {
                    setMappingName(v);
                    setField('');
                    setValueMap({});
                    setSourceValues([]);
                  }}
                />

                {mappingName && (
                  <>
                    <label style={{ ...label, marginTop: 10 }}>Column</label>
                    <SearchableSelect
                      value={field}
                      options={columnOptions}
                      placeholder="-- choose a column --"
                      onChange={(v) => {
                        setField(v);
                        setConstantValue('');
                      }}
                    />
                    {destMeta && (
                      <div style={fieldNote}>
                        <code>{destMeta.type}</code>
                        {destMeta.length > 0 && ` · up to ${destMeta.length} characters`}
                        {picklistOptions.length > 0 &&
                          ` · ${picklistOptions.length} allowed value${picklistOptions.length === 1 ? '' : 's'}`}
                        {restricted && ' · restricted, anything else is rejected'}
                      </div>
                    )}
                  </>
                )}
              </section>

              {/* ── 2. How to fill ────────────────────────────────────── */}
              {field && (
                <section style={section}>
                  <h4 style={sectionTitle}>2 · How to decide the value</h4>
                  {(
                    [
                      ['constant', 'The same value for every record'],
                      ['value_map', 'Match it from a source column'],
                      ['rules', 'Work it out with conditions'],
                    ] as [ColumnFillMode, string][]
                  ).map(([value, text]) => (
                    <label key={value} style={radioRow}>
                      <input
                        type="radio"
                        checked={mode === value}
                        onChange={() => setMode(value)}
                      />
                      <span>{text}</span>
                    </label>
                  ))}

                  {mode === 'constant' && (
                    <div style={{ marginTop: 8 }}>
                      {picklistOptions.length > 0 ? (
                        <select
                          value={constantValue}
                          onChange={(e) => setConstantValue(e.target.value)}
                          style={input}
                        >
                          <option value="">-- choose a value --</option>
                          {picklistOptions.map((v) => (
                            <option key={v} value={v}>
                              {v}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          value={constantValue}
                          onChange={(e) => setConstantValue(e.target.value)}
                          placeholder="The value to write"
                          style={input}
                        />
                      )}
                    </div>
                  )}

                  {mode === 'value_map' && (
                    <div style={{ marginTop: 8 }}>
                      <label style={label}>Source column</label>
                      <SearchableSelect
                        value={mapSourceField}
                        options={sourceOptions}
                        placeholder="-- choose a source column --"
                        onChange={(v) => {
                          setMapSourceField(v);
                          setValueMap({});
                          setAiReport(null);
                          void loadSourceValues(v);
                        }}
                      />
                      {mapSourceField && (
                        <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
                          <button
                            type="button"
                            onClick={() => void loadSourceValues(mapSourceField)}
                            disabled={busy === 'values'}
                            style={quietBtn}
                          >
                            {busy === 'values' ? 'Reading…' : 'Reload source values'}
                          </button>
                          <button
                            type="button"
                            onClick={aiMatch}
                            disabled={busy === 'ai' || !sourceValues.length}
                            style={primaryBtn(busy === 'ai' || !sourceValues.length)}
                          >
                            {busy === 'ai' ? 'Matching…' : 'Auto-match with AI'}
                          </button>
                        </div>
                      )}

                      {aiReport?.summary && <p style={aiNote}>{aiReport.summary}</p>}

                      {sourceValues.length > 0 && (
                        <div style={{ marginTop: 8 }}>
                          {sourceValues.map((v) => (
                            <div key={v} style={mapRow}>
                              <code style={mapSource}>{v}</code>
                              <span style={{ color: '#a3a3a3', fontSize: 12 }}>→</span>
                              {picklistOptions.length > 0 ? (
                                <select
                                  value={valueMap[v] ?? ''}
                                  onChange={(e) =>
                                    setValueMap({ ...valueMap, [v]: e.target.value })
                                  }
                                  style={{ ...input, flex: 1, marginBottom: 0 }}
                                >
                                  <option value="">— leave alone —</option>
                                  {picklistOptions.map((p) => (
                                    <option key={p} value={p}>
                                      {p}
                                    </option>
                                  ))}
                                </select>
                              ) : (
                                <input
                                  value={valueMap[v] ?? ''}
                                  onChange={(e) =>
                                    setValueMap({ ...valueMap, [v]: e.target.value })
                                  }
                                  placeholder="leave alone"
                                  style={{ ...input, flex: 1, marginBottom: 0 }}
                                />
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {mode === 'rules' && (
                    <div style={{ marginTop: 8 }}>
                      {rules.map((rule, index) => (
                        <RuleEditor
                          key={index}
                          rule={rule}
                          position={index + 1}
                          last={index === rules.length - 1}
                          sourceOptions={sourceOptions}
                          picklistOptions={picklistOptions}
                          transforms={outputTransforms}
                          onChange={(next) =>
                            setRules(rules.map((r, i) => (i === index ? next : r)))
                          }
                          onRemove={
                            rules.length > 1
                              ? () => setRules(rules.filter((_, i) => i !== index))
                              : undefined
                          }
                        />
                      ))}
                      <button
                        type="button"
                        onClick={() => setRules([...rules, emptyRule()])}
                        style={{ ...quietBtn, marginTop: 4 }}
                      >
                        + Add a rule
                      </button>
                    </div>
                  )}

                  {mode !== 'constant' && (
                    <div style={unmatchedBox}>
                      <label style={label}>
                        Records {mode === 'rules' ? 'no rule matches' : 'with an unmapped value'}
                      </label>
                      {(
                        [
                          ['skip', 'Leave exactly as they are'],
                          ['blank', 'Set the column to blank'],
                          ['value', 'Set the column to a value'],
                        ] as [ColumnFillUnmatched, string][]
                      ).map(([value, text]) => (
                        <label key={value} style={radioRow}>
                          <input
                            type="radio"
                            checked={unmatched === value}
                            onChange={() => setUnmatched(value)}
                          />
                          <span>{text}</span>
                        </label>
                      ))}
                      {unmatched === 'value' && (
                        <input
                          value={unmatchedValue}
                          onChange={(e) => setUnmatchedValue(e.target.value)}
                          placeholder="The value for everything else"
                          style={{ ...input, marginTop: 6 }}
                        />
                      )}
                    </div>
                  )}
                </section>
              )}

              {/* ── 3. Check and apply ────────────────────────────────── */}
              {field && (
                <section style={{ ...section, borderBottom: 'none' }}>
                  <h4 style={sectionTitle}>3 · Check and apply</h4>
                  <button
                    type="button"
                    onClick={runPreview}
                    disabled={!ready || busy === 'preview'}
                    style={primaryBtn(!ready || busy === 'preview')}
                  >
                    {busy === 'preview'
                      ? 'Working it out…'
                      : previewStale
                        ? 'Preview again'
                        : 'Preview'}
                  </button>

                  {preview && (
                    <div style={{ opacity: previewStale ? 0.5 : 1 }}>
                      <PreviewReport report={preview} />
                    </div>
                  )}

                  {previewStale && (
                    <p style={warnLine}>
                      You have changed the fill since this preview. Run it again to see
                      what would be written now.
                    </p>
                  )}

                  {preview && (
                    <>
                      <label style={{ ...radioRow, marginTop: 12 }}>
                        <input
                          type="checkbox"
                          checked={saveToMapping}
                          onChange={(e) => setSaveToMapping(e.target.checked)}
                        />
                        <span>
                          Also save this into the mapping, so a later run of{' '}
                          {target?.destination_object} fills it too
                        </span>
                      </label>
                      <button
                        type="button"
                        onClick={apply}
                        disabled={
                          busy === 'apply' || previewStale || preview.rows_to_write === 0
                        }
                        style={{
                          ...primaryBtn(
                            busy === 'apply' || previewStale || preview.rows_to_write === 0,
                          ),
                          marginTop: 8,
                        }}
                      >
                        {busy === 'apply'
                          ? 'Writing…'
                          : previewStale
                            ? 'Preview again first'
                            : preview.rows_to_write === 0
                              ? 'Nothing to write'
                              : `Apply to ${preview.rows_to_write.toLocaleString()} record${preview.rows_to_write === 1 ? '' : 's'}`}
                      </button>
                    </>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  );
}

/** One rule: the conditions that select a record, and the value it then gets. */
function RuleEditor({
  rule,
  position,
  last,
  sourceOptions,
  picklistOptions,
  transforms,
  onChange,
  onRemove,
}: {
  rule: ColumnFillRule;
  position: number;
  last: boolean;
  sourceOptions: SelectOption[];
  picklistOptions: string[];
  transforms: TransformDef[];
  onChange: (rule: ColumnFillRule) => void;
  onRemove?: () => void;
}) {
  // Kept behind a toggle so the ordinary case — set a literal value — is one
  // input rather than a transform picker nobody asked for.
  const [showTransform, setShowTransform] = useState(Boolean(rule.then.transform));
  const join: 'all' | 'any' = rule.any?.length ? 'any' : 'all';
  const conditions = rule[join] ?? [];

  const setConditions = (next: typeof conditions) =>
    onChange({ ...rule, all: undefined, any: undefined, [join]: next });

  return (
    <div style={ruleBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <span style={rulePill}>{position}</span>
        <span style={{ fontSize: 11.5, color: '#737373' }}>
          {position === 1 ? 'When' : 'Otherwise when'}
        </span>
        {conditions.length > 1 && (
          <select
            value={join}
            onChange={(e) => {
              const next = e.target.value as 'all' | 'any';
              onChange({ ...rule, all: undefined, any: undefined, [next]: conditions });
            }}
            style={miniSelect}
          >
            <option value="all">all of these</option>
            <option value="any">any of these</option>
          </select>
        )}
        {onRemove && (
          <button type="button" onClick={onRemove} style={removeBtn} title="Remove rule">
            ✕
          </button>
        )}
      </div>

      {conditions.map((condition, index) => {
        const needsValue = !COLUMN_FILL_OPERATORS_WITHOUT_VALUE.includes(condition.op);
        return (
          <div key={index} style={conditionRow}>
            <div style={{ flex: '1 1 150px', minWidth: 0 }}>
              <SearchableSelect
                value={condition.field}
                options={sourceOptions}
                placeholder="source column"
                onChange={(v) =>
                  setConditions(
                    conditions.map((c, i) => (i === index ? { ...c, field: v } : c)),
                  )
                }
              />
            </div>
            <select
              value={condition.op}
              onChange={(e) =>
                setConditions(
                  conditions.map((c, i) =>
                    i === index ? { ...c, op: e.target.value as ColumnFillOperator } : c,
                  ),
                )
              }
              style={{ ...miniSelect, flex: '0 0 auto' }}
            >
              {COLUMN_FILL_OPERATORS.map((op) => (
                <option key={op} value={op}>
                  {OPERATOR_LABELS[op]}
                </option>
              ))}
            </select>
            {needsValue && (
              <input
                value={typeof condition.value === 'string' ? condition.value : ''}
                onChange={(e) =>
                  setConditions(
                    conditions.map((c, i) =>
                      i === index ? { ...c, value: e.target.value } : c,
                    ),
                  )
                }
                placeholder={
                  condition.op === 'in' || condition.op === 'not_in'
                    ? 'a, b, c'
                    : 'value'
                }
                style={{ ...input, flex: '1 1 100px', marginBottom: 0, minWidth: 60 }}
              />
            )}
            {conditions.length > 1 && (
              <button
                type="button"
                onClick={() => setConditions(conditions.filter((_, i) => i !== index))}
                style={removeBtn}
                title="Remove condition"
              >
                ✕
              </button>
            )}
          </div>
        );
      })}

      <button
        type="button"
        onClick={() =>
          setConditions([...conditions, { field: '', op: 'equals', value: '' }])
        }
        style={addConditionBtn}
      >
        + condition
      </button>

      <div style={thenRow}>
        <span style={{ fontSize: 11.5, color: '#737373', flex: '0 0 auto' }}>then set</span>
        {showTransform ? (
          <>
            <select
              value={rule.then.transform ?? ''}
              onChange={(e) =>
                onChange({
                  ...rule,
                  then: { ...rule.then, value: undefined, transform: e.target.value },
                })
              }
              style={{ ...miniSelect, flex: '1 1 120px' }}
            >
              <option value="">-- transform --</option>
              {transforms.map((t) => (
                <option key={t.name} value={t.name}>
                  {t.label}
                </option>
              ))}
            </select>
            <div style={{ flex: '1 1 130px', minWidth: 0 }}>
              <SearchableSelect
                value={rule.then.from ?? ''}
                options={sourceOptions}
                placeholder="of this column"
                onChange={(v) => onChange({ ...rule, then: { ...rule.then, from: v } })}
              />
            </div>
          </>
        ) : picklistOptions.length > 0 ? (
          <select
            value={rule.then.value ?? ''}
            onChange={(e) => onChange({ ...rule, then: { value: e.target.value } })}
            style={{ ...input, flex: 1, marginBottom: 0 }}
          >
            <option value="">-- choose a value --</option>
            {picklistOptions.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={rule.then.value ?? ''}
            onChange={(e) => onChange({ ...rule, then: { value: e.target.value } })}
            placeholder="value"
            style={{ ...input, flex: 1, marginBottom: 0 }}
          />
        )}
        <button
          type="button"
          onClick={() => {
            setShowTransform(!showTransform);
            onChange({ ...rule, then: showTransform ? { value: '' } : { transform: '' } });
          }}
          style={removeBtn}
          title={showTransform ? 'Use a plain value' : 'Build the value with a transform'}
        >
          {showTransform ? '↤' : '⋯'}
        </button>
      </div>

      {last && (
        <div style={{ fontSize: 10.5, color: '#a3a3a3', marginTop: 4 }}>
          Rules are tried top to bottom and the first match wins.
        </div>
      )}
    </div>
  );
}

/** What the fill would do, in the three numbers that are easy to conflate. */
function PreviewReport({ report }: { report: ColumnFillPreview }) {
  return (
    <div style={previewBox}>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 8 }}>
        <Stat label="will be written" value={report.rows_to_write} strong />
        <Stat label={report.untouched_means} value={report.rows_untouched} />
        <Stat label="records in the destination" value={report.rows_migrated} />
      </div>

      {report.picklist_rejected.length > 0 && (
        <div style={rejectBox}>
          <strong>
            {report.picklist_rejected.length} value
            {report.picklist_rejected.length === 1 ? '' : 's'} the destination will refuse
          </strong>
          <p style={{ margin: '3px 0 0', lineHeight: 1.5 }}>
            This column only accepts values from its list, so these rows would fail the
            load. Change them before applying.
          </p>
          <ul style={{ margin: '5px 0 0', paddingLeft: 18 }}>
            {report.picklist_rejected.map((r) => (
              <li key={r.value}>
                <code>{r.value}</code> — {r.rows.toLocaleString()} row
                {r.rows === 1 ? '' : 's'}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.warnings.map((w) => (
        <p key={w} style={warnLine}>
          {w}
        </p>
      ))}

      {report.rule_hits.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {report.rule_hits.map((hit) => (
            <div key={hit.index} style={hitRow}>
              <span style={{ color: hit.index === -1 ? '#a3a3a3' : '#404040' }}>
                {hit.index === -1 ? 'nothing matched' : `${hit.index + 1}. ${hit.label}`}
              </span>
              <span style={{ fontWeight: 700 }}>{hit.rows.toLocaleString()}</span>
            </div>
          ))}
        </div>
      )}

      {report.value_hits.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={summaryToggle}>
            {report.value_hits.filter((v) => v.mapped).length} of{' '}
            {report.value_hits.length} source values mapped
          </summary>
          <div style={{ marginTop: 5 }}>
            {report.value_hits.map((hit) => (
              <div key={`${hit.source}->${hit.destination}`} style={hitRow}>
                <span style={{ color: hit.mapped ? '#404040' : '#a3a3a3' }}>
                  <code>{hit.source || '(blank)'}</code>
                  {hit.mapped ? ` → ${hit.destination}` : ' — left alone'}
                </span>
                <span style={{ fontWeight: 700 }}>{hit.rows.toLocaleString()}</span>
              </div>
            ))}
          </div>
        </details>
      )}

      {report.samples.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={summaryToggle}>See {report.samples.length} example rows</summary>
          <div style={{ marginTop: 5 }}>
            {report.samples.map((s) => (
              <div key={s.record} style={{ ...hitRow, fontSize: 11 }}>
                <span style={{ color: '#737373' }}>
                  {Object.entries(s.source)
                    .map(([k, v]) => `${k}=${v || '∅'}`)
                    .join('  ') || s.record}
                </span>
                <code style={{ fontWeight: 700 }}>{s.value || '∅'}</code>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  strong,
}: {
  label: string;
  value: number;
  strong?: boolean;
}) {
  return (
    <div>
      <div
        style={{
          fontSize: strong ? 20 : 16,
          fontWeight: 700,
          color: strong ? '#171717' : '#404040',
          lineHeight: 1.1,
        }}
      >
        {value.toLocaleString()}
      </div>
      <div style={{ fontSize: 10.5, color: '#737373' }}>{label}</div>
    </div>
  );
}

const backdrop: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(17,24,39,0.28)',
  zIndex: 90,
};

const drawer: React.CSSProperties = {
  position: 'fixed',
  top: 0,
  right: 0,
  bottom: 0,
  width: 'min(560px, 96vw)',
  background: '#fff',
  borderLeft: '1px solid #e5e5e5',
  boxShadow: '-8px 0 28px rgba(17,24,39,0.14)',
  zIndex: 91,
  display: 'flex',
  flexDirection: 'column',
};

const head: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 10,
  padding: '14px 16px',
  borderBottom: '1px solid #e5e5e5',
};

const closeBtn: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontSize: 15,
  color: '#737373',
  padding: 4,
  lineHeight: 1,
};

const body: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '0 16px 24px',
};

const section: React.CSSProperties = {
  padding: '14px 0',
  borderBottom: '1px solid #f5f5f5',
};

const sectionTitle: React.CSSProperties = {
  margin: '0 0 10px',
  fontSize: 11,
  fontWeight: 700,
  color: '#737373',
  textTransform: 'uppercase',
  letterSpacing: 0.5,
};

const label: React.CSSProperties = {
  display: 'block',
  fontSize: 11.5,
  fontWeight: 600,
  color: '#404040',
  marginBottom: 4,
};

const input: React.CSSProperties = {
  width: '100%',
  padding: '7px 9px',
  border: '1px solid #d4d4d4',
  borderRadius: 7,
  fontSize: 12.5,
  boxSizing: 'border-box',
  marginBottom: 0,
};

const miniSelect: React.CSSProperties = {
  padding: '6px 7px',
  border: '1px solid #d4d4d4',
  borderRadius: 7,
  fontSize: 12,
  background: '#fff',
};

const radioRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 7,
  fontSize: 12.5,
  color: '#404040',
  padding: '3px 0',
  cursor: 'pointer',
  lineHeight: 1.45,
};

const fieldNote: React.CSSProperties = {
  fontSize: 11.5,
  color: '#737373',
  marginTop: 5,
};

const unmatchedBox: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 10px',
  background: '#fafafa',
  border: '1px solid #f5f5f5',
  borderRadius: 8,
};

const ruleBox: React.CSSProperties = {
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  padding: '8px 10px',
  marginBottom: 7,
  background: '#fff',
};

const rulePill: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  color: '#262626',
  background: '#e5e5e5',
  borderRadius: 999,
  padding: '1px 6px',
};

const conditionRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 5,
  marginBottom: 5,
  flexWrap: 'wrap',
};

const thenRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 5,
  marginTop: 7,
  paddingTop: 7,
  borderTop: '1px dashed #e5e5e5',
  flexWrap: 'wrap',
};

const addConditionBtn: React.CSSProperties = {
  fontSize: 11,
  color: '#525252',
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
};

const removeBtn: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  color: '#a3a3a3',
  fontSize: 12,
  padding: '2px 4px',
  lineHeight: 1,
};

const previewBox: React.CSSProperties = {
  marginTop: 10,
  padding: '10px 12px',
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  fontSize: 12,
  color: '#404040',
};

const hitRow: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: 10,
  fontSize: 11.5,
  padding: '2px 0',
  borderBottom: '1px solid #f5f5f5',
};

const rejectBox: React.CSSProperties = {
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 7,
  padding: '8px 10px',
  color: '#171717',
  fontSize: 11.5,
  marginBottom: 8,
};

const warnLine: React.CSSProperties = {
  margin: '4px 0 0',
  fontSize: 11.5,
  color: '#404040',
  lineHeight: 1.5,
};

const aiNote: React.CSSProperties = {
  margin: '7px 0 0',
  fontSize: 11.5,
  color: '#171717',
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 7,
  padding: '7px 9px',
  lineHeight: 1.5,
};

const summaryToggle: React.CSSProperties = {
  fontSize: 11.5,
  color: '#737373',
  cursor: 'pointer',
};

const muted: React.CSSProperties = {
  fontSize: 12.5,
  color: '#737373',
  margin: '16px 0',
  lineHeight: 1.6,
};

const primaryBtn = (disabled: boolean): React.CSSProperties => ({
  fontSize: 12.5,
  fontWeight: 600,
  color: disabled ? '#a3a3a3' : '#fff',
  background: disabled ? '#f5f5f5' : '#171717',
  border: `1px solid ${disabled ? '#e5e5e5' : '#171717'}`,
  borderRadius: 7,
  padding: '7px 14px',
  cursor: disabled ? 'not-allowed' : 'pointer',
});

const quietBtn: React.CSSProperties = {
  fontSize: 12,
  color: '#404040',
  background: '#fff',
  border: '1px solid #d4d4d4',
  borderRadius: 7,
  padding: '6px 11px',
  cursor: 'pointer',
};

const errorBox: React.CSSProperties = {
  fontSize: 12,
  color: '#171717',
  background: '#f5f5f5',
  border: '1px solid #e5e5e5',
  borderRadius: 7,
  padding: '8px 10px',
  marginTop: 12,
};

const doneBox: React.CSSProperties = {
  fontSize: 12,
  color: '#171717',
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 7,
  padding: '8px 10px',
  marginTop: 12,
};

const mapRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  marginBottom: 5,
};

const mapSource: React.CSSProperties = {
  flex: '0 0 40%',
  fontSize: 11.5,
  color: '#171717',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
