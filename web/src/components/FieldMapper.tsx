import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../api/client';
import type {
  MappingFieldDef,
  SourceFieldInfo,
  DestinationFieldInfo,
  TransformDef,
  TypeCheckResponse,
  AgentChange,
  DestinationRecord,
  DestinationRecordSearch,
  ValueMapSuggestReport,
  LookupResolution,
} from '../api/client';
import { SearchableSelect } from './SearchableSelect';
import type { SelectOption } from './SearchableSelect';
import { REMOTE_SEARCH_DEBOUNCE_MS, useDebouncedValue } from '../hooks/useDebouncedValue';

const CREATE_NEW_SENTINEL = '__create_new__';

// Salesforce only allows the External Id and unique flags on these three types,
// so the option is hidden rather than offered and then rejected on create.
const EXTERNAL_ID_TYPES = ['Text', 'Number', 'Email'];

interface Props {
  fields: MappingFieldDef[];
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  sfObject: string;
  destCreds: Record<string, string>;
  sourceType?: string;
  sourceCreds?: Record<string, string>;
  sourceTable?: string;
  /**
   * Concerns raised against this mapping. Their presence is what turns on the
   * on-demand value pickers: querying an object for a list of records is only
   * worth offering where something has already gone wrong.
   */
  notices?: AgentChange[];
  onDestFieldsChange: (fields: DestinationFieldInfo[]) => void;
  onChange: (fields: MappingFieldDef[]) => void;
}

export function FieldMapper({
  fields, sourceFields, destFields, sfObject, destCreds,
  sourceType = 'servicenow', sourceCreds = {}, sourceTable = '', notices = [],
  onDestFieldsChange, onChange,
}: Props) {
  const [transforms, setTransforms] = useState<TransformDef[]>([]);

  useEffect(() => {
    api.transforms.list(sourceType).then(setTransforms).catch(() => {});
  }, [sourceType]);
  const update = (idx: number, patch: Partial<MappingFieldDef>) => {
    const next = fields.map((f, i) => (i === idx ? { ...f, ...patch } : f));
    onChange(next);
  };

  const remove = (idx: number) => onChange(fields.filter((_, i) => i !== idx));

  const add = () =>
    onChange([...fields, { source_field: '', destination_field: '' }]);

  const showConfidence = fields.some((f) => f.confidence !== undefined);

  return (
    <div>
      <div style={{ overflowX: 'auto', border: '1px solid #e5e5e5', borderRadius: 8 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#fafafa', textAlign: 'left' }}>
              <th style={th}>Source field</th>
              <th style={th}>Destination field</th>
              <th style={th}>Transform</th>
              {showConfidence && <th style={th} title="How sure the AI match was">Match</th>}
              <th style={th}></th>
            </tr>
          </thead>
          <tbody>
            {fields.length === 0 && (
              <tr>
                <td colSpan={showConfidence ? 5 : 4} style={{ ...td, textAlign: 'center', color: '#a3a3a3' }}>
                  No field mappings yet. Use "AI match remaining" or add rows manually.
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
                notices={notices}
                siblingFields={fields}
                onDestFieldsChange={onDestFieldsChange}
                transforms={transforms}
                showConfidence={showConfidence}
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
  notices = [],
  siblingFields = [],
  onDestFieldsChange,
  transforms,
  showConfidence,
  onUpdate,
  onRemove,
}: {
  showConfidence: boolean;
  field: MappingFieldDef;
  sourceFields: SourceFieldInfo[];
  destFields: DestinationFieldInfo[];
  sfObject: string;
  destCreds: Record<string, string>;
  sourceType?: string;
  sourceCreds?: Record<string, string>;
  sourceTable?: string;
  notices?: AgentChange[];
  /**
   * Every row in this mapping, not just this one. The value matcher needs the
   * record type and the controlling picklist's mapping, both of which live on
   * sibling rows.
   */
  siblingFields?: MappingFieldDef[];
  onDestFieldsChange: (fields: DestinationFieldInfo[]) => void;
  transforms: TransformDef[];
  onUpdate: (patch: Partial<MappingFieldDef>) => void;
  onRemove: () => void;
}) {
  const isMultiSource = Array.isArray(field.source_field);
  // Narrowed on the value itself rather than the flag above, so the joined form
  // is a string to the type checker as well as at runtime.
  const sourceValue = Array.isArray(field.source_field)
    ? field.source_field.join(', ')
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
  const [newExternalId, setNewExternalId] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState('');

  const [refRecords, setRefRecords] = useState<DestinationRecord[]>([]);
  const [refLoading, setRefLoading] = useState(false);
  // The total behind the capped page, and anything the org would not return.
  const [refMeta, setRefMeta] = useState<DestinationRecordSearch | null>(null);
  const [valuesOpen, setValuesOpen] = useState(false);
  const [refSearch, setRefSearch] = useState('');

  const destInfo = destFields.find((d) => d.name === field.destination_field);
  const isReference = !!(destInfo?.reference_to && destInfo.reference_to.length > 0);
  const referenceObject = destInfo?.reference_to?.[0] ?? '';
  const picklistValues = destInfo?.picklist_values ?? [];

  // Which id-mapping table a lookup on this row should read, and whether
  // anything can populate it. Resolved from the catalogue rather than derived
  // from the object name: the table name is built from the *parent* mapping's
  // configuration, so the object name alone never produces a table that exists.
  // Fetched as soon as the row is known to be a reference — it is a local query,
  // and having the answer before Lookup mode is chosen is what lets that choice
  // write a working configuration instead of a placeholder.
  const [lookupRes, setLookupRes] = useState<LookupResolution | null>(null);
  useEffect(() => {
    if (!isReference || !referenceObject) {
      setLookupRes(null);
      return;
    }
    let live = true;
    api.mappings
      .lookupResolution(referenceObject, sourceType)
      .then((res) => { if (live) setLookupRes(res); })
      .catch(() => { if (live) setLookupRes(null); });
    return () => { live = false; };
  }, [isReference, referenceObject, sourceType]);

  // The best id source: most ids on hand, so a lookup resolves now where it can.
  const lookupTarget = lookupRes?.candidates[0] ?? null;

  // A notice naming this field is the strongest hint we have about where the
  // operator needs to look, so it draws attention to that row's button. It does
  // not gate the button: the value is just as often needed on a mapping the
  // agent had nothing to say about.
  const fieldNamedInNotice =
    !!field.destination_field &&
    notices.some(
      (n) =>
        n.field === field.destination_field ||
        `${n.rationale} ${n.suggested_action ?? ''}`.includes(field.destination_field),
    );

  // Infer the value source for legacy rows that predate the explicit field.
  const valueSource: 'field' | 'constant' | 'lookup' =
    field.value_source ??
    (field.constant_value !== undefined
      ? 'constant'
      : field.transformation_function === 'generic_lookup'
        ? 'lookup'
        : 'field');

  // Constant mode presents the same way for every field, so the operator does
  // not have to know in advance whether this one can be picked from a list. A
  // lookup fetches records from the org, a picklist reveals the values the
  // describe already gave us, and anything else says plainly that it is free
  // text rather than silently offering nothing.
  const constantKind: 'lookup' | 'picklist' | 'plain' = isReference
    ? 'lookup'
    : picklistValues.length > 0
      ? 'picklist'
      : 'plain';

  const constantHint =
    constantKind === 'lookup'
      ? `Lookup to ${referenceObject} — expects a record id`
      : constantKind === 'picklist'
        ? `Picklist — ${picklistValues.length} allowed value${picklistValues.length === 1 ? '' : 's'}`
        : 'Free text — this field has no fixed set of values';

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

  const loadRefValues = async (search: string) => {
    if (!referenceObject) return;
    setRefLoading(true);
    try {
      const result = await api.schema.destinationRecordSearch(referenceObject, destCreds, {
        search,
        for_object: sfObject,
        limit: 10,
      });
      setRefMeta(result);
      setRefRecords(result.records);
    } catch (e) {
      setRefRecords([]);
      setRefMeta({
        object_name: referenceObject,
        records: [],
        total: 0,
        truncated: false,
        unavailable_fields: [],
        scoped_to: '',
        error: e instanceof Error ? e.message : 'The lookup failed',
      });
    } finally {
      setRefLoading(false);
    }
  };

  // Opening runs immediately; typing is debounced so a search costs one query
  // per pause rather than one per keystroke. A picklist needs no query — its
  // values came with the describe — so opening one costs nothing.
  const [debouncedRefSearch] = useDebouncedValue(refSearch, REMOTE_SEARCH_DEBOUNCE_MS);

  useEffect(() => {
    if (!valuesOpen || constantKind !== 'lookup') return;
    void loadRefValues(debouncedRefSearch);
  }, [valuesOpen, debouncedRefSearch, constantKind]); // eslint-disable-line react-hooks/exhaustive-deps

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
        defer_to_backfill: undefined,
        defer_object: undefined,
        defer_reason: undefined,
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
        defer_to_backfill: clearTransform ? undefined : field.defer_to_backfill,
        defer_object: clearTransform ? undefined : field.defer_object,
        defer_reason: clearTransform ? undefined : field.defer_reason,
        constant_value: field.constant_value ?? '',
      });
      setTypeWarning(null);
      if (clearTransform) { setParamsText(''); setParamsError(false); }
    } else if (mode === 'lookup') {
      // The table the parent load writes, never the object name. The object name
      // was what this used to write, and no such table is ever created, so every
      // lookup built here resolved empty for every row.
      const params = {
        lookup_tables: lookupTarget ? [lookupTarget.lookup_table] : [],
        default: '',
      };
      onUpdate({
        value_source: 'lookup',
        constant_value: undefined,
        source_field: collapsedSource,
        transformation_function: 'generic_lookup',
        transformation_params: params,
        // Leave the reference empty when it cannot be resolved rather than
        // discarding the record. Skipping was the default here, which meant one
        // unresolvable lookup silently cost every row that had it — and since
        // dropped rows never reach the load, the run still reported no failures.
        skip_if_not_found: false,
        // Always defer, including when nothing migrates the object yet. Rows that
        // resolve are unaffected, since only an empty result is recorded, so this
        // costs nothing where it is not needed and preserves the link where it
        // is. Withholding it from the not-yet-migrated case would lose exactly
        // the references most likely to be recoverable later — that object being
        // migrated is the normal way such a wait ends.
        defer_to_backfill: true,
        // The object waited for, so the wait can end on its own. The table name
        // above is empty until something migrates this object, and it is built
        // from that future mapping's own configuration, so it cannot be written
        // down in advance — naming the object lets the backfill find the table
        // whenever it looks, instead of watching a name that never appears.
        defer_object: referenceObject,
        defer_reason: lookupTarget ? undefined : 'parent_not_migrated',
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
        defer_to_backfill: clearTransform ? undefined : field.defer_to_backfill,
        defer_object: clearTransform ? undefined : field.defer_object,
        defer_reason: clearTransform ? undefined : field.defer_reason,
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
      const detail = describeRecord(r, sfObject);
      opts.push({
        value: r.id,
        label: detail ? `${r.name} — ${detail} (${r.id})` : `${r.name} (${r.id})`,
      });
    }
    return opts;
  }, [refRecords, field.constant_value, sfObject]);

  // A value already saved against this row is kept selectable even when the org
  // no longer offers it, so opening the list cannot silently drop the mapping.
  const picklistOptions: SelectOption[] = useMemo(() => {
    const current = field.constant_value;
    const opts = picklistValues.map((v) => ({ value: v, label: v }));
    if (typeof current === 'string' && current && !picklistValues.includes(current)) {
      opts.unshift({ value: current, label: `${current} (not in this org)` });
    }
    return opts;
  }, [picklistValues, field.constant_value]);

  // ── Picklist value-map editor (generic_value_map, non-legacy) ────────────────
  const isPicklist = picklistValues.length > 0;
  const showValueMapEligible = isPicklist && valueSource === 'field' && !isMultiSource;
  const [choiceValues, setChoiceValues] = useState<{ value: string; label: string }[]>([]);
  const [choiceLoading, setChoiceLoading] = useState(false);
  const [valueMapOpen, setValueMapOpen] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiNote, setAiNote] = useState('');
  const [aiReport, setAiReport] = useState<ValueMapSuggestReport | null>(null);

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

  // The pairings the model called approximations. Kept out of the map itself —
  // they are applied like any other — and used to mark those rows so a
  // judgement call is not indistinguishable from an exact match.
  const approximate = useMemo(
    () => (aiReport?.matches ?? []).filter((m) => m.needs_review),
    [aiReport],
  );
  const approximateRows = useMemo(
    () => new Set(approximate.map((m) => m.source_value)),
    [approximate],
  );

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

  /**
   * Fill in only the values that agree outright, ignoring case and surrounding
   * space. Deliberately not fuzzy: this used to accept a substring match in
   * either direction, which pairs 'Closed' with 'Closed Incomplete' and changes
   * what a record means. Anything less than identical is the model's job below,
   * where a pairing arrives with a rationale and a confidence attached.
   */
  const matchExact = () => {
    const byNormalised = new Map(picklistValues.map((v) => [v.trim().toLowerCase(), v]));
    const next = { ...valueMap };
    let filled = 0;
    for (const row of mapRows) {
      if (next[row.source]) continue;
      const hit =
        byNormalised.get(row.source.trim().toLowerCase()) ??
        byNormalised.get(row.label.trim().toLowerCase());
      if (hit) {
        next[row.source] = hit;
        filled += 1;
      }
    }
    setAiNote(
      filled > 0
        ? `Filled ${filled} value${filled === 1 ? '' : 's'} that matched exactly.`
        : 'No source value matches a destination value exactly. Try "AI match values".',
    );
    if (filled > 0) writeMap(next);
  };

  const aiMatchValues = async () => {
    const srcName = isMultiSource ? '' : ((field.source_field as string) ?? '');
    if (!sourceTable || !srcName || !field.destination_field) return;
    setAiBusy(true);
    setAiNote('');
    setAiReport(null);
    try {
      const report = await api.mappings.suggestValueMap({
        source_type: sourceType ?? 'servicenow',
        source_table: sourceTable,
        source_field: srcName,
        salesforce_object: sfObject,
        destination_field: field.destination_field,
        source_credentials: sourceCreds ?? {},
        destination_credentials: destCreds,
        // The whole draft goes over: it carries the record type, the controlling
        // field's mapping, and the pairs a person already made by hand.
        existing_fields: siblingFields,
        source_values: choiceValues.length > 0 ? choiceValues : undefined,
      });
      setAiReport(report);
      if (report.error) {
        setAiNote(report.error);
        return;
      }
      if (report.matches.length === 0) {
        setAiNote(report.summary || 'The model proposed nothing it was confident about.');
        return;
      }
      const next = { ...valueMap };
      for (const m of report.matches) next[m.source_value] = m.destination_value;
      writeMap(next, report.suggested_default || mapDefault);
      setValueMapOpen(true);
    } catch (e: unknown) {
      setAiNote(e instanceof Error ? e.message : 'Value matching failed');
    } finally {
      setAiBusy(false);
    }
  };

  const conf = field.confidence;
  const confColor =
    conf === undefined ? '#a3a3a3' : conf >= 0.9 ? '#171717' : conf >= 0.7 ? '#525252' : '#0a0a0a';

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
      setNewExternalId(false);
      setCreateError('');
    } else {
      onUpdate({ destination_field: value });
      const srcName = isMultiSource ? '' : (field.source_field as string) ?? '';
      checkTypeCompat(srcName, value);
      // The open list belongs to the field being replaced; leaving it open
      // would offer the previous target's records against the new field.
      setValuesOpen(false);
      setRefRecords([]);
      setRefMeta(null);
      setRefSearch('');
    }
  };

  const handleCreateField = async () => {
    if (!newLabel.trim()) return;
    setCreateBusy(true);
    setCreateError('');
    const apiName = newApiName.trim() || newLabel.trim().replace(/\s+/g, '_');
    const finalApiName = apiName.endsWith('__c') ? apiName : apiName + '__c';
    const asExternalId = newExternalId && EXTERNAL_ID_TYPES.includes(newType);
    try {
      const res = await api.customFields.create({
        destination_credentials: destCreds,
        salesforce_object: sfObject,
        field_label: newLabel.trim(),
        field_api_name: finalApiName,
        field_type: newType,
        length: newType === 'LongTextArea' ? 32768 : 255,
        external_id: asExternalId,
        unique: asExternalId,
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
          // Only an External Id can serve as an upsert key, so the flag decides
          // whether this field appears as one without waiting for a re-describe.
          id_lookup: asExternalId,
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
      background: typeWarning.coercible ? '#f5f5f5' : '#fafafa',
      color: typeWarning.coercible ? '#404040' : '#171717',
      border: `1px solid ${typeWarning.coercible ? '#e5e5e5' : '#e5e5e5'}`,
    }}>
      {typeWarning.coercible ? '\u26A0' : '\u274C'}{' '}
      {typeWarning.warning}
    </div>
  ) : null;

  // What a lookup on this row will actually resolve against, and what happens to
  // the rows it cannot resolve. Both were previously invisible, which is how a
  // lookup pointing at a table that never existed went unnoticed until every
  // record had been discarded.
  const lookupPanel = valueSource === 'lookup' ? (
    <div style={{
      marginTop: 4,
      padding: '5px 8px',
      borderRadius: 4,
      fontSize: 11,
      lineHeight: '16px',
      background: lookupTarget ? '#fafafa' : '#fafafa',
      color: lookupTarget ? '#404040' : '#404040',
      border: `1px solid ${lookupTarget ? '#e5e5e5' : '#e5e5e5'}`,
    }}>
      {!lookupRes ? (
        <span style={{ color: '#737373' }}>Resolving where {referenceObject} ids come from…</span>
      ) : (
        <>
          <div>
            {lookupTarget ? (
              <>
                Reads ids from <b>{lookupTarget.mapping_name}</b> ({lookupTarget.source_table} →{' '}
                {referenceObject}) —{' '}
                {lookupTarget.migrated_count > 0
                  ? `${lookupTarget.migrated_count.toLocaleString()} available now`
                  : 'none yet, so these resolve once that object has run'}
              </>
            ) : (
              <>
                Nothing migrates {referenceObject} yet, so every row loads with this
                empty. Ticked below, each one is recorded and offered for filling as
                soon as {referenceObject} is migrated — you will be told when that
                happens. Untick it only if the link is not worth keeping.
              </>
            )}
          </div>
          <label style={{ display: 'flex', gap: 6, alignItems: 'flex-start', marginTop: 4 }}>
            <input
              type="checkbox"
              checked={!!field.defer_to_backfill}
              onChange={(e) =>
                onUpdate({
                  defer_to_backfill: e.target.checked,
                  // Kept with the flag, since it is what the wait is on.
                  defer_object: e.target.checked ? referenceObject : undefined,
                })
              }
              style={{ marginTop: 2 }}
            />
            <span>
              Remember unresolved references for a later pass
              <span style={{ display: 'block', color: '#737373' }}>
                Unticked, a record whose reference cannot be resolved still loads
                but nothing records what it pointed at, so the link is lost.
              </span>
            </span>
          </label>
        </>
      )}
    </div>
  ) : null;

  const refGuardrail =
    isReference && valueSource === 'field' && !!sourceValue && !field.transformation_function;
  const guardrailBadge = refGuardrail ? (
    <div style={{
      marginTop: 4,
      padding: '3px 8px',
      borderRadius: 4,
      fontSize: 11,
      lineHeight: '16px',
      background: '#fafafa',
      color: '#171717',
      border: '1px solid #e5e5e5',
    }}>
      {'\u274C'} '{field.destination_field}' is a lookup to {referenceObject || 'another object'}.
      Raw source values cause MALFORMED_ID — switch this row to <b>Lookup</b> or <b>Constant</b>.
    </div>
  ) : null;

  const valueMapPanel = showValueMapEligible ? (
    <div style={{
      marginTop: 4, padding: '8px 10px', borderRadius: 6,
      background: '#fafafa', border: '1px solid #e5e5e5',
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
        <button
          type="button"
          onClick={aiMatchValues}
          disabled={aiBusy || isMultiSource || !sourceValue || !sourceTable}
          style={{ ...linkBtn, fontWeight: 600 }}
          title={
            isMultiSource
              ? 'Only single-source rows have one column of values to map'
              : !sourceValue
                ? 'Pick a source field first'
                : 'Ask the model to pair these values by meaning. Every pair is checked against the values this record type accepts before it is offered.'
          }
        >
          {aiBusy ? 'Matching…' : 'AI match values'}
        </button>
        {valueMapOpen && mapRows.length > 0 && (
          <button
            type="button"
            onClick={matchExact}
            style={linkBtn}
            title="Fill in only the values that are spelled the same on both sides."
          >
            Exact matches
          </button>
        )}
      </div>

      {(aiNote || aiReport) && (
        <div style={{ fontSize: 12, color: '#404040', marginTop: 6 }}>
          {aiNote && <div style={{ color: '#404040' }}>{aiNote}</div>}
          {aiReport && !aiReport.error && (
            <>
              {aiReport.matches.length > 0 && (
                <div style={{ color: '#171717' }}>
                  Mapped {aiReport.matches.length} of {aiReport.considered_source_values} source
                  value{aiReport.considered_source_values === 1 ? '' : 's'}
                  {aiReport.suggested_default
                    ? `, with '${aiReport.suggested_default}' as the fallback`
                    : ''}
                  . Check them before saving.
                </div>
              )}
              {aiReport.summary && (
                <div style={{ color: '#737373', marginTop: 2 }}>{aiReport.summary}</div>
              )}
              {approximate.length > 0 && (
                <details style={{ marginTop: 4 }} open>
                  <summary style={{ cursor: 'pointer', color: '#404040' }}>
                    {approximate.length} of these {approximate.length === 1 ? 'is a' : 'are'}{' '}
                    judgement call{approximate.length === 1 ? '' : 's'} — the nearest available
                    value rather than a true equivalent
                  </summary>
                  <ul style={{ margin: '4px 0 0 16px', padding: 0, color: '#737373' }}>
                    {approximate.map((m) => (
                      <li key={m.source_value}>
                        <strong>{m.source_value}</strong> {'\u2192'} {m.destination_value}:{' '}
                        {m.rationale}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {aiReport.unmapped.length > 0 && (
                <details style={{ marginTop: 4 }} open>
                  <summary style={{ cursor: 'pointer', color: '#404040' }}>
                    {aiReport.unmapped.length} value
                    {aiReport.unmapped.length === 1 ? '' : 's'} had no home on this picklist at all
                    {aiReport.restricted
                      ? ' — these rows will be rejected until mapped'
                      : ' — these will be written through off the picklist'}
                  </summary>
                  <ul style={{ margin: '4px 0 0 16px', padding: 0, color: '#737373' }}>
                    {aiReport.unmapped.map((u) => (
                      <li key={u.source_value}>
                        <strong>{u.source_value}</strong>: {u.reason}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {aiReport.rejected.length > 0 && (
                <details style={{ marginTop: 4 }}>
                  <summary style={{ cursor: 'pointer', color: '#737373' }}>
                    {aiReport.rejected.length} proposal
                    {aiReport.rejected.length === 1 ? '' : 's'} refused by the schema check
                  </summary>
                  <ul style={{ margin: '4px 0 0 16px', padding: 0, color: '#737373' }}>
                    {aiReport.rejected.map((r, i) => (
                      <li key={i}>{r.reason}</li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          )}
        </div>
      )}

      {valueMapOpen && (
        <div style={{ marginTop: 8 }}>
          {mapRows.length === 0 && (
            <div style={{ fontSize: 12, color: '#737373', marginBottom: 6 }}>
              No source values yet. Click "Load source values" or add one below.
            </div>
          )}
          {mapRows.map((row) => (
            <div key={row.source} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span style={{
                fontSize: 12, minWidth: 140, maxWidth: 200, overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#404040',
              }} title={row.label !== row.source ? `${row.label} (${row.source})` : row.source}>
                {row.label}{row.label !== row.source ? ` (${row.source})` : ''}
              </span>
              <span
                style={{ color: approximateRows.has(row.source) ? '#525252' : '#a3a3a3' }}
                title={
                  approximateRows.has(row.source)
                    ? 'Approximate match — the nearest available value, not a true equivalent'
                    : undefined
                }
              >
                {approximateRows.has(row.source) ? '\u2248' : '\u2192'}
              </span>
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

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, paddingTop: 8, borderTop: '1px solid #e5e5e5' }}>
            <span style={{ fontSize: 12, color: '#404040', minWidth: 140 }}>Default (unmatched)</span>
            <span style={{ color: '#a3a3a3' }}>{'\u2192'}</span>
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
      <tr style={{ borderTop: '1px solid #f5f5f5' }}>
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

            {valueSource === 'constant' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 200 }}>
                {!valuesOpen ? (
                  <>
                    <input
                      value={field.constant_value === undefined ? '' : String(field.constant_value)}
                      placeholder={
                        constantKind === 'lookup' ? `${referenceObject} record id` : 'constant value'
                      }
                      onChange={(e) => onUpdate({ constant_value: e.target.value })}
                      style={inputStyle}
                    />
                    {constantKind !== 'plain' && (
                      <button
                        type="button"
                        onClick={() => setValuesOpen(true)}
                        style={fieldNamedInNotice ? { ...linkBtn, ...linkBtnFlagged } : linkBtn}
                        title={
                          constantKind === 'lookup'
                            ? `Query the org for ${referenceObject} records. Nothing is fetched until you ask.`
                            : `Show the ${picklistValues.length} value(s) this picklist accepts.`
                        }
                      >
                        Load values
                      </button>
                    )}
                    <span style={{ fontSize: 11, color: '#a3a3a3' }}>{constantHint}</span>
                  </>
                ) : (
                  <>
                    <SearchableSelect
                      value={typeof field.constant_value === 'string' ? field.constant_value : ''}
                      options={constantKind === 'lookup' ? refOptions : picklistOptions}
                      placeholder={
                        refLoading
                          ? 'Loading…'
                          : constantKind === 'lookup'
                            ? `-- pick a ${referenceObject} record --`
                            : '-- pick a value --'
                      }
                      onChange={(v) => onUpdate({ constant_value: v })}
                      style={{ minWidth: 200 }}
                    />
                    {constantKind === 'lookup' && (refMeta?.truncated || refSearch) && (
                      <input
                        value={refSearch}
                        placeholder={`Search ${referenceObject} by name`}
                        onChange={(e) => setRefSearch(e.target.value)}
                        style={inputStyle}
                      />
                    )}
                    <span style={{ fontSize: 11, color: '#737373' }}>
                      {constantKind !== 'lookup'
                        ? constantHint
                        : refLoading
                          ? 'Loading…'
                          : refMeta?.error
                            ? refMeta.error
                            : refMeta
                              ? `${refMeta.records.length} of ${refMeta.total}${
                                  refMeta.scoped_to ? ` on ${refMeta.scoped_to}` : ''
                                }${refMeta.truncated ? ' — search to narrow' : ''}`
                              : ''}
                    </span>
                    {constantKind === 'lookup' &&
                      (refMeta?.unavailable_fields ?? []).map((name) => (
                        <span key={name} style={{ fontSize: 11, color: '#404040' }}>
                          {unavailableFieldMeaning(name)}
                        </span>
                      ))}
                  </>
                )}
              </div>
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
                style={{ ...inputStyle, fontSize: 11, color: '#737373' }}
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
              {EXTERNAL_ID_TYPES.includes(newType) && (
                <label style={{ display: 'flex', gap: 6, alignItems: 'flex-start', fontSize: 11, color: '#404040' }}>
                  <input
                    type="checkbox"
                    checked={newExternalId}
                    onChange={(e) => setNewExternalId(e.target.checked)}
                    style={{ marginTop: 2 }}
                  />
                  <span>
                    External Id (unique)
                    <span style={{ display: 'block', color: '#737373' }}>
                      Hold the source record's id here to make re-runs safe: the load
                      then updates the records it created before instead of inserting
                      them again.
                    </span>
                  </span>
                </label>
              )}
              {createError && (
                <div style={{ fontSize: 11, color: '#0a0a0a' }}>{createError}</div>
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
          {field.transformation_function && (
            <input
              value={paramsText}
              placeholder='Parameters, e.g. {"default":""}'
              title="Transform parameters (JSON)"
              onChange={(e) => commitParams(e.target.value)}
              style={{
                ...inputStyle,
                marginTop: 4,
                fontSize: 11.5,
                fontFamily: 'monospace',
                borderColor: paramsError ? '#0a0a0a' : '#e5e5e5',
              }}
            />
          )}
        </td>
        {showConfidence && (
          <td style={{ ...td, color: confColor, fontWeight: 600, whiteSpace: 'nowrap' }}>
            {conf === undefined ? '\u2014' : `${Math.round(conf * 100)}%`}
          </td>
        )}
        <td style={td}>
          <button type="button" onClick={onRemove} style={removeBtn} title="Remove">
            ✕
          </button>
        </td>
      </tr>
      {(warningBadge || guardrailBadge || lookupPanel || valueMapPanel) && (
        <tr>
          <td colSpan={showConfidence ? 5 : 4} style={{ padding: '0 10px 6px' }}>
            {warningBadge}
            {guardrailBadge}
            {lookupPanel}
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

/**
 * Fold a record's disambiguating columns into one readable phrase.
 *
 * Two record types both called "Person Account" are indistinguishable by name;
 * their DeveloperName is not. Anything that merely repeats the name or the
 * object already on screen is dropped rather than shown twice.
 */
function describeRecord(record: DestinationRecord, sfObject: string): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(record.extra ?? {})) {
    if (value === null || value === '' || value === false) continue;
    if (value === true) {
      parts.push(key.replace(/^Is/, '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase());
      continue;
    }
    const text = String(value);
    if (text === record.name || text === sfObject) continue;
    parts.push(text);
  }
  return parts.join(' · ');
}

/**
 * What it means that the org refused a column.
 *
 * The refusal is usually the answer to the question being asked: an org that
 * rejects IsPersonType has Person Accounts switched off, which is exactly what
 * an operator hunting for a person-enabled record type needs to hear.
 */
function unavailableFieldMeaning(name: string): string {
  if (name === 'IsPersonType') {
    return 'Person Accounts are not enabled in this org, so no record type can support one.';
  }
  return `This org does not expose ${name}, so that column is missing from the list.`;
}

const th: React.CSSProperties = { padding: '8px 10px', fontWeight: 600, color: '#525252', fontSize: 12 };
const linkBtn: React.CSSProperties = {
  background: 'none',
  border: 'none',
  color: '#171717',
  cursor: 'pointer',
  fontSize: 12,
  padding: '2px 4px',
};
// Drawn as an action rather than a link on the row a notice pointed at.
const linkBtnFlagged: React.CSSProperties = {
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 6,
  color: '#262626',
  fontWeight: 600,
};
const td: React.CSSProperties = { padding: '6px 10px', verticalAlign: 'middle' };
const selectStyle: React.CSSProperties = {
  width: '100%',
  minWidth: 120,
  padding: '6px 8px',
  border: '1px solid #d4d4d4',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
  background: '#fff',
};
const inputStyle: React.CSSProperties = {
  width: '100%',
  minWidth: 120,
  padding: '6px 8px',
  border: '1px solid #d4d4d4',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
};
const addBtn: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 14px',
  background: '#f5f5f5',
  color: '#171717',
  border: '1px solid #d4d4d4',
  borderRadius: 8,
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 600,
};
const removeBtn: React.CSSProperties = {
  background: '#fafafa',
  color: '#0a0a0a',
  border: '1px solid #e5e5e5',
  borderRadius: 6,
  cursor: 'pointer',
  padding: '4px 8px',
  fontSize: 12,
};
const createBtn: React.CSSProperties = {
  padding: '4px 8px',
  background: '#171717',
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
  color: '#404040',
  border: '1px solid #d4d4d4',
  borderRadius: 4,
  cursor: 'pointer',
  fontSize: 11,
};
