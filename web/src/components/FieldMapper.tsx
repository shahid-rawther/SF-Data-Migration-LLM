import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../api/client';
import type { MappingFieldDef, SourceFieldInfo, DestinationFieldInfo, TransformDef, TypeCheckResponse } from '../api/client';
import { SearchableSelect } from './SearchableSelect';
import type { SelectOption } from './SearchableSelect';

const CREATE_NEW_SENTINEL = '__create_new__';

interface Props {
  fields: MappingFieldDef[];
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  sfObject: string;
  destCreds: Record<string, string>;
  sourceType?: string;
  sourceCreds?: Record<string, string>;
  sourceTable?: string;
  onDestFieldsChange: (fields: DestinationFieldInfo[]) => void;
  onChange: (fields: MappingFieldDef[]) => void;
}

export function FieldMapper({
  fields, sourceFields, destFields, sfObject, destCreds,
  sourceType = 'servicenow', sourceCreds = {}, sourceTable = '',
  onDestFieldsChange, onChange,
}: Props) {
  const [transforms, setTransforms] = useState<TransformDef[]>([]);

  useEffect(() => {
    api.transforms.list().then(setTransforms).catch(() => {});
  }, []);
  const update = (idx: number, patch: Partial<MappingFieldDef>) => {
    const next = fields.map((f, i) => (i === idx ? { ...f, ...patch } : f));
    onChange(next);
  };

  const remove = (idx: number) => onChange(fields.filter((_, i) => i !== idx));

  const add = () =>
    onChange([...fields, { source_field: '', destination_field: '' }]);

  return (
    <div>
      <div style={{ overflowX: 'auto', border: '1px solid #e5e7eb', borderRadius: 8 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#f8fafc', textAlign: 'left' }}>
              <th style={th}>Source field</th>
              <th style={th}>Destination field</th>
              <th style={th}>Transform</th>
              <th style={th}>Params (JSON)</th>
              <th style={th}>Conf.</th>
              <th style={th}></th>
            </tr>
          </thead>
          <tbody>
            {fields.length === 0 && (
              <tr>
                <td colSpan={6} style={{ ...td, textAlign: 'center', color: '#9ca3af' }}>
                  No field mappings yet. Use "Auto-suggest" or add rows manually.
                </td>
              </tr>
            )}
            {fields.map((f, idx) => (
              <FieldRow
                key={idx}
                field={f}
                sourceFields={sourceFields}
                destFields={destFields}
                sfObject={sfObject}
                destCreds={destCreds}
                sourceType={sourceType}
                sourceCreds={sourceCreds}
                sourceTable={sourceTable}
                onDestFieldsChange={onDestFieldsChange}
                transforms={transforms}
                onUpdate={(patch) => update(idx, patch)}
                onRemove={() => remove(idx)}
              />
            ))}
          </tbody>
        </table>
      </div>
      <button type="button" onClick={add} style={addBtn}>
        + Add field mapping
      </button>
    </div>
  );
}

function FieldRow({
  field,
  sourceFields,
  destFields,
  sfObject,
  destCreds,
  sourceType,
  sourceCreds,
  sourceTable,
  onDestFieldsChange,
  transforms,
  onUpdate,
  onRemove,
}: {
  field: MappingFieldDef;
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  sfObject: string;
  destCreds: Record<string, string>;
  sourceType?: string;
  sourceCreds?: Record<string, string>;
  sourceTable?: string;
  onDestFieldsChange: (fields: DestinationFieldInfo[]) => void;
  transforms: TransformDef[];
  onUpdate: (patch: Partial<MappingFieldDef>) => void;
  onRemove: () => void;
}) {
  const isMultiSource = Array.isArray(field.source_field);
  const sourceValue = isMultiSource
    ? (field.source_field as string[]).join(', ')
    : field.source_field ?? '';

  const [paramsText, setParamsText] = useState(
    field.transformation_params ? JSON.stringify(field.transformation_params) : '',
  );
  const [paramsError, setParamsError] = useState(false);
  const [typeWarning, setTypeWarning] = useState<TypeCheckResponse | null>(null);

  // Raw text buffer for the multi-field (comma-separated) input. This holds
  // exactly what the user types (commas/spaces included) while a cleaned array
  // is persisted separately, so typing a comma is no longer erased on re-render.
  const [multiText, setMultiText] = useState(isMultiSource ? sourceValue : '');
  useEffect(() => {
    if (isMultiSource) setMultiText((field.source_field as string[]).join(', '));
  }, [isMultiSource, field.source_field]);

  const [creating, setCreating] = useState(false);
  const [newLabel, setNewLabel] = useState('');
  const [newApiName, setNewApiName] = useState('');
  const [newType, setNewType] = useState('Text');
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState('');

  const [refRecords, setRefRecords] = useState<{ id: string; name: string }[]>([]);
  const [refLoading, setRefLoading] = useState(false);

  const destInfo = destFields.find((d) => d.name === field.destination_field);
  const isReference = !!(destInfo?.reference_to && destInfo.reference_to.length > 0);
  const referenceObject = destInfo?.reference_to?.[0] ?? '';
  const picklistValues = destInfo?.picklist_values ?? [];

  // Infer the value source for legacy rows that predate the explicit field.
  const valueSource: 'field' | 'constant' | 'lookup' =
    field.value_source ??
    (field.constant_value !== undefined
      ? 'constant'
      : field.transformation_function === 'generic_lookup'
        ? 'lookup'
        : 'field');

  const checkTypeCompat = useCallback(async (srcFieldName: string, destFieldName: string) => {
    if (!srcFieldName || !destFieldName) {
      setTypeWarning(null);
      return;
    }
    const srcInfo = sourceFields.find((sf) => sf.name === srcFieldName);
    const destInfo = destFields.find((df) => df.name === destFieldName);
    if (!srcInfo || !destInfo) {
      setTypeWarning(null);
      return;
    }
    try {
      const result = await api.compat.typeCheck(srcInfo.field_type, destInfo.type);
      if (result.compatible) {
        setTypeWarning(null);
      } else {
        setTypeWarning(result);
        if (result.coercible && !field.transformation_function) {
          onUpdate({
            transformation_function: result.transform,
            transformation_params: result.params as Record<string, unknown>,
          });
          setParamsText(JSON.stringify(result.params));
          setParamsError(false);
        }
      }
    } catch {
      setTypeWarning(null);
    }
  }, [sourceFields, destFields, field.transformation_function, onUpdate]);

  useEffect(() => {
    if (field.type_warning) {
      setTypeWarning({ compatible: false, coercible: true, transform: '', params: {}, warning: field.type_warning });
    }
  }, [field.type_warning]);

  useEffect(() => {
    const srcName = isMultiSource ? '' : (field.source_field as string) ?? '';
    if (srcName && field.destination_field) {
      checkTypeCompat(srcName, field.destination_field);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const commitParams = (text: string) => {
    setParamsText(text);
    if (!text.trim()) {
      setParamsError(false);
      onUpdate({ transformation_params: undefined });
      return;
    }
    try {
      const parsed = JSON.parse(text);
      setParamsError(false);
      onUpdate({ transformation_params: parsed });
    } catch {
      setParamsError(true);
    }
  };

  // Load org records for the reference picker when in Constant mode on a lookup field.
  useEffect(() => {
    if (valueSource === 'constant' && isReference && referenceObject) {
      let cancelled = false;
      setRefLoading(true);
      api.schema
        .destinationRecords(referenceObject, destCreds)
        .then((r) => { if (!cancelled) setRefRecords(r.records); })
        .catch(() => { if (!cancelled) setRefRecords([]); })
        .finally(() => { if (!cancelled) setRefLoading(false); });
      return () => { cancelled = true; };
    }
  }, [valueSource, isReference, referenceObject, destCreds]);

  const handleValueSourceChange = (mode: 'field' | 'constant' | 'lookup' | 'multi') => {
    const wasLookup = field.transformation_function === 'generic_lookup';
    if (mode === 'multi') {
      const seed = sourceValue
        ? sourceValue.split(',').map((s) => s.trim()).filter(Boolean)
        : [];
      const concatParams =
        field.transformation_function === 'generic_concat' && field.transformation_params
          ? field.transformation_params
          : { separator: ' - ', skip_empty: true };
      onUpdate({
        value_source: 'field',
        constant_value: undefined,
        source_field: seed,
        transformation_function: 'generic_concat',
        transformation_params: concatParams,
        skip_if_not_found: undefined,
      });
      setParamsText(JSON.stringify(concatParams));
      setParamsError(false);
      setTypeWarning(null);
      return;
    }
    // Leaving multi mode: collapse an array source back to a single value so the
    // single-select picker works again.
    const collapsedSource = Array.isArray(field.source_field)
      ? (field.source_field[0] ?? '')
      : field.source_field;
    const wasConcat = field.transformation_function === 'generic_concat' && Array.isArray(field.source_field);
    if (mode === 'constant') {
      const clearTransform = wasLookup || wasConcat;
      onUpdate({
        value_source: 'constant',
        source_field: '',
        transformation_function: clearTransform ? undefined : field.transformation_function,
        transformation_params: clearTransform ? undefined : field.transformation_params,
        skip_if_not_found: clearTransform ? undefined : field.skip_if_not_found,
        constant_value: field.constant_value ?? '',
      });
      setTypeWarning(null);
      if (clearTransform) { setParamsText(''); setParamsError(false); }
    } else if (mode === 'lookup') {
      const params = { lookup_tables: [referenceObject], default: '' };
      onUpdate({
        value_source: 'lookup',
        constant_value: undefined,
        source_field: collapsedSource,
        transformation_function: 'generic_lookup',
        transformation_params: params,
        skip_if_not_found: true,
      });
      setParamsText(JSON.stringify(params));
      setParamsError(false);
      setTypeWarning(null);
    } else {
      const clearTransform = wasLookup || wasConcat;
      onUpdate({
        value_source: 'field',
        constant_value: undefined,
        source_field: collapsedSource,
        transformation_function: clearTransform ? undefined : field.transformation_function,
        transformation_params: clearTransform ? undefined : field.transformation_params,
        skip_if_not_found: clearTransform ? undefined : field.skip_if_not_found,
      });
      if (clearTransform) { setParamsText(''); setParamsError(false); }
    }
  };

  const refOptions: SelectOption[] = useMemo(() => {
    const opts: SelectOption[] = [];
    const current = field.constant_value;
    if (typeof current === 'string' && current && !refRecords.some((r) => r.id === current)) {
      opts.push({ value: current, label: current });
    }
    for (const r of refRecords) {
      opts.push({ value: r.id, label: `${r.name} (${r.id})` });
    }
    return opts;
  }, [refRecords, field.constant_value]);

  // ── Picklist value-map editor (generic_value_map, non-legacy) ────────────────
  const isPicklist = picklistValues.length > 0;
  const showValueMapEligible = isPicklist && valueSource === 'field' && !isMultiSource;
  const [choiceValues, setChoiceValues] = useState<{ value: string; label: string }[]>([]);
  const [choiceLoading, setChoiceLoading] = useState(false);
  const [valueMapOpen, setValueMapOpen] = useState(false);

  const valueMap: Record<string, string> = useMemo(
    () => (field.transformation_params?.map as Record<string, string>) ?? {},
    [field.transformation_params],
  );
  const mapDefault: string = (field.transformation_params?.default as string) ?? '';

  const mapRows = useMemo(() => {
    const keys = new Set<string>(Object.keys(valueMap));
    for (const c of choiceValues) keys.add(c.value);
    return Array.from(keys).map((src) => ({
      source: src,
      label: choiceValues.find((c) => c.value === src)?.label ?? src,
      dest: valueMap[src] ?? '',
    }));
  }, [valueMap, choiceValues]);

  const mappedCount = mapRows.filter((r) => r.dest).length;

  const writeMap = (nextMap: Record<string, string>, nextDefault = mapDefault) => {
    onUpdate({
      transformation_function: 'generic_value_map',
      transformation_params: { map: nextMap, default: nextDefault },
    });
    setParamsText(JSON.stringify({ map: nextMap, default: nextDefault }));
    setParamsError(false);
  };

  const setMapping = (src: string, dest: string) => {
    const next = { ...valueMap };
    if (dest) next[src] = dest;
    else delete next[src];
    writeMap(next);
  };

  const removeSourceRow = (src: string) => {
    const next = { ...valueMap };
    delete next[src];
    writeMap(next);
    setChoiceValues((prev) => prev.filter((c) => c.value !== src));
  };

  const addSourceRow = (src: string) => {
    const key = src.trim();
    if (key && !(key in valueMap)) writeMap({ ...valueMap, [key]: '' });
  };

  const loadChoices = async () => {
    const srcName = isMultiSource ? '' : (field.source_field as string) ?? '';
    const queryField = srcName || field.destination_field;
    if (!sourceTable || !queryField) return;
    setChoiceLoading(true);
    try {
      const res = await api.schema.sourceValues(sourceType ?? 'servicenow', sourceCreds ?? {}, {
        table: sourceTable,
        field: queryField,
      });
      setChoiceValues(res.values);
      setValueMapOpen(true);
    } catch {
      setChoiceValues([]);
    } finally {
      setChoiceLoading(false);
    }
  };

  const autoMatch = () => {
    const next = { ...valueMap };
    for (const row of mapRows) {
      if (next[row.source]) continue;
      const cand = [row.label.toLowerCase(), row.source.toLowerCase()];
      const match = picklistValues.find(
        (pv) =>
          cand.includes(pv.toLowerCase()) ||
          cand.some((c) => c && (pv.toLowerCase().includes(c) || c.includes(pv.toLowerCase()))),
      );
      if (match) next[row.source] = match;
    }
    writeMap(next);
  };

  const conf = field.confidence;
  const confColor =
    conf === undefined ? '#9ca3af' : conf >= 0.9 ? '#16a34a' : conf >= 0.7 ? '#d97706' : '#dc2626';

  const sourceNotInList = sourceValue && !isMultiSource && !sourceFields.some((sf) => sf.name === sourceValue);
  const destNotInList = field.destination_field && !destFields.some((df) => df.name === field.destination_field);

  const sourceOptions: SelectOption[] = useMemo(() => {
    const opts: SelectOption[] = [];
    if (sourceNotInList) {
      opts.push({ value: sourceValue, label: sourceValue });
    }
    for (const sf of sourceFields) {
      const hasPrefix = sf.name.includes('.');
      const table = hasPrefix ? sf.name.split('.')[0] : sourceTable;
      const shortName = hasPrefix ? sf.name.split('.').pop()! : sf.name;
      const tablePrefix = table ? `${table}.` : '';
      opts.push({
        value: sf.name,
        label: `${tablePrefix}${shortName} — ${sf.label} [${sf.field_type}]`,
      });
    }
    return opts;
  }, [sourceFields, sourceNotInList, sourceValue, sourceTable]);

  const destOptions: SelectOption[] = useMemo(() => {
    const opts: SelectOption[] = [];
    if (destNotInList) {
      opts.push({ value: field.destination_field, label: field.destination_field });
    }
    for (const df of destFields.filter((d) => d.createable)) {
      opts.push({ value: df.name, label: `${df.label} (${df.name}) [${df.type}]` });
    }
    opts.push({ value: CREATE_NEW_SENTINEL, label: '+ Create custom field...' });
    return opts;
  }, [destFields, destNotInList, field.destination_field]);

  const handleSourceChange = (value: string) => {
    onUpdate({ source_field: value });
    checkTypeCompat(value, field.destination_field);
  };

  const handleDestChange = (value: string) => {
    if (value === CREATE_NEW_SENTINEL) {
      setCreating(true);
      setNewLabel('');
      setNewApiName('');
      setNewType('Text');
      setCreateError('');
    } else {
      onUpdate({ destination_field: value });
      const srcName = isMultiSource ? '' : (field.source_field as string) ?? '';
      checkTypeCompat(srcName, value);
    }
  };

  const handleCreateField = async () => {
    if (!newLabel.trim()) return;
    setCreateBusy(true);
    setCreateError('');
    const apiName = newApiName.trim() || newLabel.trim().replace(/\s+/g, '_');
    const finalApiName = apiName.endsWith('__c') ? apiName : apiName + '__c';
    try {
      const res = await api.customFields.create({
        destination_credentials: destCreds,
        salesforce_object: sfObject,
        field_label: newLabel.trim(),
        field_api_name: finalApiName,
        field_type: newType,
        length: newType === 'LongTextArea' ? 32768 : 255,
      });
      if (res.success) {
        const newField: DestinationFieldInfo = {
          name: res.field_api_name,
          label: newLabel.trim(),
          type: newType,
          length: newType === 'LongTextArea' ? 32768 : 255,
          required: false,
          createable: true,
          updateable: true,
          reference_to: [],
          picklist_values: [],
        };
        onDestFieldsChange([...destFields, newField]);
        onUpdate({ destination_field: res.field_api_name });
        setCreating(false);
      } else {
        setCreateError(res.message);
      }
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : 'Failed to create field');
    } finally {
      setCreateBusy(false);
    }
  };

  const warningBadge = typeWarning && !typeWarning.compatible ? (
    <div style={{
      marginTop: 4,
      padding: '3px 8px',
      borderRadius: 4,
      fontSize: 11,
      lineHeight: '16px',
      background: typeWarning.coercible ? '#fef9c3' : '#fef2f2',
      color: typeWarning.coercible ? '#92400e' : '#991b1b',
      border: `1px solid ${typeWarning.coercible ? '#fde68a' : '#fecaca'}`,
    }}>
      {typeWarning.coercible ? '\u26A0' : '\u274C'}{' '}
      {typeWarning.warning}
    </div>
  ) : null;

  // Guardrail: a reference/lookup field fed a raw source value copies a source id
  // straight into a Salesforce lookup → MALFORMED_ID. Warn and steer to Lookup/Constant.
  const refGuardrail =
    isReference && valueSource === 'field' && !!sourceValue && !field.transformation_function;
  const guardrailBadge = refGuardrail ? (
    <div style={{
      marginTop: 4,
      padding: '3px 8px',
      borderRadius: 4,
      fontSize: 11,
      lineHeight: '16px',
      background: '#fef2f2',
      color: '#991b1b',
      border: '1px solid #fecaca',
    }}>
      {'\u274C'} '{field.destination_field}' is a lookup to {referenceObject || 'another object'}.
      Raw source values cause MALFORMED_ID — switch this row to <b>Lookup</b> or <b>Constant</b>.
    </div>
  ) : null;

  const valueMapPanel = showValueMapEligible ? (
    <div style={{
      marginTop: 4, padding: '8px 10px', borderRadius: 6,
      background: '#f8fafc', border: '1px solid #e2e8f0',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <button
          type="button"
          onClick={() => setValueMapOpen((o) => !o)}
          style={{ ...linkBtn, fontWeight: 600 }}
        >
          {valueMapOpen ? '\u25B4' : '\u25BE'} Map picklist values for '{field.destination_field}'
          {' '}({mappedCount}/{mapRows.length || 0} mapped)
        </button>
        <button type="button" onClick={loadChoices} disabled={choiceLoading} style={linkBtn}>
          {choiceLoading ? 'Loading…' : 'Load source values'}
        </button>
        {valueMapOpen && mapRows.length > 0 && (
          <button type="button" onClick={autoMatch} style={linkBtn}>Auto-match</button>
        )}
      </div>

      {valueMapOpen && (
        <div style={{ marginTop: 8 }}>
          {mapRows.length === 0 && (
            <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 6 }}>
              No source values yet. Click "Load source values" or add one below.
            </div>
          )}
          {mapRows.map((row) => (
            <div key={row.source} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span style={{
                fontSize: 12, minWidth: 140, maxWidth: 200, overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#374151',
              }} title={row.label !== row.source ? `${row.label} (${row.source})` : row.source}>
                {row.label}{row.label !== row.source ? ` (${row.source})` : ''}
              </span>
              <span style={{ color: '#9ca3af' }}>{'\u2192'}</span>
              <div style={{ flex: 1, minWidth: 160 }}>
                <SearchableSelect
                  value={row.dest}
                  options={picklistValues.map((v) => ({ value: v, label: v }))}
                  placeholder="-- destination value --"
                  onChange={(v) => setMapping(row.source, v)}
                  style={{ minWidth: 160 }}
                />
              </div>
              <button type="button" onClick={() => removeSourceRow(row.source)} style={removeBtn} title="Remove">
                ✕
              </button>
            </div>
          ))}

          <AddValueRow onAdd={addSourceRow} />

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, paddingTop: 8, borderTop: '1px solid #e2e8f0' }}>
            <span style={{ fontSize: 12, color: '#374151', minWidth: 140 }}>Default (unmatched)</span>
            <span style={{ color: '#9ca3af' }}>{'\u2192'}</span>
            <div style={{ flex: 1, minWidth: 160 }}>
              <SearchableSelect
                value={mapDefault}
                options={[{ value: '', label: '(leave blank)' }, ...picklistValues.map((v) => ({ value: v, label: v }))]}
                placeholder="(leave blank)"
                onChange={(v) => writeMap(valueMap, v)}
                style={{ minWidth: 160 }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  ) : null;

  return (
    <>
      <tr style={{ borderTop: '1px solid #f1f5f9' }}>
        <td style={td}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 180 }}>
            <select
              value={isMultiSource ? 'multi' : valueSource}
              onChange={(e) => handleValueSourceChange(e.target.value as 'field' | 'constant' | 'lookup' | 'multi')}
              style={{ ...selectStyle, fontSize: 11, padding: '3px 6px', minWidth: 0 }}
              title="How this destination field gets its value"
            >
              <option value="field">Source field</option>
              <option value="multi">Multiple fields (concat)</option>
              <option value="constant">Constant value</option>
              {isReference && <option value="lookup">Lookup (resolve id)</option>}
            </select>

            {(valueSource === 'field' || valueSource === 'lookup') && (
              isMultiSource ? (
                <input
                  value={multiText}
                  placeholder={
                    sourceFields.length >= 2
                      ? `${sourceFields[0].name}, ${sourceFields[1].name}`
                      : 'field1, field2'
                  }
                  onChange={(e) => {
                    setMultiText(e.target.value);
                    onUpdate({
                      source_field: e.target.value
                        .split(',')
                        .map((s) => s.trim())
                        .filter(Boolean),
                    });
                  }}
                  style={inputStyle}
                  title="Multi-field source (comma-separated) — joined via the Concatenate transform"
                />
              ) : (
                <SearchableSelect
                  value={sourceValue}
                  options={sourceOptions}
                  placeholder={valueSource === 'lookup' ? '-- source id field --' : '-- select source field --'}
                  onChange={handleSourceChange}
                  style={{ minWidth: 180 }}
                />
              )
            )}

            {valueSource === 'constant' && isReference && (
              refLoading ? (
                <span style={{ fontSize: 12, color: '#6b7280' }}>Loading {referenceObject} records…</span>
              ) : (
                <SearchableSelect
                  value={typeof field.constant_value === 'string' ? field.constant_value : ''}
                  options={refOptions}
                  placeholder={`-- pick a ${referenceObject} record --`}
                  onChange={(v) => onUpdate({ constant_value: v })}
                  style={{ minWidth: 180 }}
                />
              )
            )}

            {valueSource === 'constant' && !isReference && picklistValues.length > 0 && (
              <SearchableSelect
                value={typeof field.constant_value === 'string' ? field.constant_value : ''}
                options={picklistValues.map((v) => ({ value: v, label: v }))}
                placeholder="-- pick a value --"
                onChange={(v) => onUpdate({ constant_value: v })}
                style={{ minWidth: 180 }}
              />
            )}

            {valueSource === 'constant' && !isReference && picklistValues.length === 0 && (
              <input
                value={field.constant_value === undefined ? '' : String(field.constant_value)}
                placeholder="constant value"
                onChange={(e) => onUpdate({ constant_value: e.target.value })}
                style={inputStyle}
              />
            )}
          </div>
        </td>
        <td style={td}>
          {creating ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 220 }}>
              <input
                placeholder="Field Label"
                value={newLabel}
                onChange={(e) => {
                  setNewLabel(e.target.value);
                  setNewApiName(e.target.value.replace(/\s+/g, '_'));
                }}
                style={inputStyle}
                autoFocus
              />
              <input
                placeholder="API Name (auto)"
                value={newApiName}
                onChange={(e) => setNewApiName(e.target.value)}
                style={{ ...inputStyle, fontSize: 11, color: '#6b7280' }}
              />
              <select value={newType} onChange={(e) => setNewType(e.target.value)} style={selectStyle}>
                <option value="Text">Text</option>
                <option value="Number">Number</option>
                <option value="TextArea">TextArea</option>
                <option value="LongTextArea">Long Text Area</option>
                <option value="Checkbox">Checkbox</option>
                <option value="Date">Date</option>
                <option value="DateTime">DateTime</option>
                <option value="Url">URL</option>
                <option value="Email">Email</option>
                <option value="Phone">Phone</option>
              </select>
              {createError && (
                <div style={{ fontSize: 11, color: '#dc2626' }}>{createError}</div>
              )}
              <div style={{ display: 'flex', gap: 4 }}>
                <button
                  type="button"
                  disabled={createBusy || !newLabel.trim()}
                  onClick={handleCreateField}
                  style={{ ...createBtn, flex: 1 }}
                >
                  {createBusy ? 'Creating...' : 'Create'}
                </button>
                <button
                  type="button"
                  onClick={() => setCreating(false)}
                  style={{ ...cancelBtn, flex: 1 }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <SearchableSelect
              value={field.destination_field}
              options={destOptions}
              placeholder="-- select destination field --"
              onChange={handleDestChange}
              style={{ minWidth: 180 }}
            />
          )}
        </td>
        <td style={td}>
          <select
            value={field.transformation_function ?? ''}
            onChange={(e) => {
              const fn = e.target.value || undefined;
              const tdef = transforms.find((t) => t.name === fn);
              const defaultParams = tdef
                ? Object.fromEntries(Object.entries(tdef.params_schema).map(([k, v]) => [k, v.default]))
                : undefined;
              onUpdate({ transformation_function: fn, transformation_params: fn ? defaultParams : undefined });
              if (fn && defaultParams) {
                setParamsText(JSON.stringify(defaultParams));
                setParamsError(false);
              } else {
                setParamsText('');
                setParamsError(false);
              }
            }}
            style={selectStyle}
            title={transforms.find((t) => t.name === field.transformation_function)?.description ?? ''}
          >
            <option value="">(none)</option>
            {transforms.map((t) => (
              <option key={t.name} value={t.name}>
                {t.label}
              </option>
            ))}
            {field.transformation_function &&
              !transforms.some((t) => t.name === field.transformation_function) && (
                <option value={field.transformation_function}>{field.transformation_function}</option>
              )}
          </select>
        </td>
        <td style={td}>
          <input
            value={paramsText}
            placeholder='{"default":""}'
            onChange={(e) => commitParams(e.target.value)}
            style={{ ...inputStyle, borderColor: paramsError ? '#dc2626' : '#d1d5db' }}
          />
        </td>
        <td style={{ ...td, color: confColor, fontWeight: 600, whiteSpace: 'nowrap' }}>
          {conf === undefined ? '\u2014' : `${Math.round(conf * 100)}%`}
        </td>
        <td style={td}>
          <button type="button" onClick={onRemove} style={removeBtn} title="Remove">
            ✕
          </button>
        </td>
      </tr>
      {(warningBadge || guardrailBadge || valueMapPanel) && (
        <tr>
          <td colSpan={6} style={{ padding: '0 10px 6px' }}>
            {warningBadge}
            {guardrailBadge}
            {valueMapPanel}
          </td>
        </tr>
      )}
    </>
  );
}

function AddValueRow({ onAdd }: { onAdd: (value: string) => void }) {
  const [text, setText] = useState('');
  const commit = () => {
    if (text.trim()) {
      onAdd(text);
      setText('');
    }
  };
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
      <input
        value={text}
        placeholder="+ add source value"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commit(); } }}
        style={{ ...inputStyle, minWidth: 140, maxWidth: 200, fontSize: 12 }}
      />
      <button type="button" onClick={commit} disabled={!text.trim()} style={linkBtn}>Add</button>
    </div>
  );
}

const th: React.CSSProperties = { padding: '8px 10px', fontWeight: 600, color: '#475569', fontSize: 12 };
const linkBtn: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: '#2563eb',
  cursor: 'pointer',
  fontSize: 12,
  padding: '2px 4px',
};
const td: React.CSSProperties = { padding: '6px 10px', verticalAlign: 'middle' };
const selectStyle: React.CSSProperties = {
  width: '100%',
  minWidth: 120,
  padding: '6px 8px',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
  background: '#fff',
};
const inputStyle: React.CSSProperties = {
  width: '100%',
  minWidth: 120,
  padding: '6px 8px',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
};
const addBtn: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 14px',
  background: '#eff6ff',
  color: '#2563eb',
  border: '1px solid #bfdbfe',
  borderRadius: 8,
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
};
const removeBtn: React.CSSProperties = {
  background: '#fef2f2',
  color: '#dc2626',
  border: '1px solid #fecaca',
  borderRadius: 6,
  cursor: 'pointer',
  padding: '4px 8px',
  fontSize: 12,
};
const createBtn: React.CSSProperties = {
  padding: '4px 8px',
  background: '#16a34a',
  color: '#fff',
  border: 'none',
  borderRadius: 4,
  cursor: 'pointer',
  fontSize: 11,
  fontWeight: 600,
};
const cancelBtn: React.CSSProperties = {
  padding: '4px 8px',
  background: '#fff',
  color: '#374151',
  border: '1px solid #d1d5db',
  borderRadius: 4,
  cursor: 'pointer',
  fontSize: 11,
};
