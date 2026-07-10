import { useEffect, useState, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Source, SalesforceDiagnosticsResponse, SavedConnection, CompatibilityReport, CreateCustomFieldRequest } from '../api/client';
import { CredentialForm } from '../components/CredentialForm';
import { SourceTableSelector } from '../components/SourceTableSelector';
import { TableMappingCard } from '../components/TableMappingCard';
import type { TableMappingState } from '../components/TableMappingCard';

type Step = 1 | 2 | 3 | 4 | 5 | 6;

const SALESFORCE_SCHEMA = {
  type: 'object',
  required: ['base_url', 'client_id', 'client_secret'],
  properties: {
    base_url: { type: 'string', title: 'Instance URL', description: 'e.g. https://myorg.salesforce.com' },
    client_id: { type: 'string', title: 'Client ID' },
    client_secret: { type: 'string', title: 'Client Secret', format: 'password' },
    api_version: { type: 'string', title: 'API Version', description: 'e.g. v60.0' },
  },
};

export function NewMigrationWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>(1);
  const [sources, setSources] = useState<Source[]>([]);
  const [selectedSource, setSelectedSource] = useState<Source | null>(null);
  const [sourceCreds, setSourceCreds] = useState<Record<string, string>>({});
  const [destCreds, setDestCreds] = useState<Record<string, string>>({ api_version: 'v60.0' });
  // Live schema state
  const [sourceTables, setSourceTables] = useState<string[]>([]);
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  const [loadingTables, setLoadingTables] = useState(false);
  const [sfObjects, setSfObjects] = useState<{ name: string; label: string }[]>([]);
  const [loadingSfObjects, setLoadingSfObjects] = useState(false);
  const [tableMappings, setTableMappings] = useState<TableMappingState[]>([]);
  const [savingMappings, setSavingMappings] = useState(false);
  const [options, setOptions] = useState({
    start_date: '',
    end_date: '',
    source_batch_size: 5000,
    service_now_batch_size: 5000,
    salesforce_batch_size: 1000,
    disable_load: false,
  });
  const [validating, setValidating] = useState(false);
  const [validationMsg, setValidationMsg] = useState<string | null>(null);
  const [validatingDest, setValidatingDest] = useState(false);
  const [destValidationMsg, setDestValidationMsg] = useState<string | null>(null);
  const [destValidated, setDestValidated] = useState(false);
  const [checkingDiagnostics, setCheckingDiagnostics] = useState(false);
  const [diagnostics, setDiagnostics] = useState<SalesforceDiagnosticsResponse | null>(null);
  const [expandDetails, setExpandDetails] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Compatibility check state
  const [checkingCompat, setCheckingCompat] = useState(false);
  const [compatReport, setCompatReport] = useState<CompatibilityReport | null>(null);
  const [compatError, setCompatError] = useState<string | null>(null);
  const [expandedCompatObj, setExpandedCompatObj] = useState<string | null>(null);

  // Custom field creation state
  const [creatingField, setCreatingField] = useState<string | null>(null); // "objectName::fieldName" key
  const [customFieldName, setCustomFieldName] = useState<string>('');
  const [customFieldLabel, setCustomFieldLabel] = useState<string>('');
  const [customFieldType, setCustomFieldType] = useState<string>('Text');
  const [customFieldMsg, setCustomFieldMsg] = useState<{ key: string; success: boolean; message: string } | null>(null);
  const [bulkCreating, setBulkCreating] = useState(false);
  const [bulkResults, setBulkResults] = useState<{ field: string; success: boolean; message: string }[]>([]);

  // Saved connections state
  const [savedSourceConns, setSavedSourceConns] = useState<SavedConnection[]>([]);
  const [savedDestConns, setSavedDestConns] = useState<SavedConnection[]>([]);
  const [selectedSourceConn, setSelectedSourceConn] = useState<string>(''); // connection id or 'new'
  const [selectedDestConn, setSelectedDestConn] = useState<string>('');     // connection id or 'new'
  const [sourceConnName, setSourceConnName] = useState('');
  const [destConnName, setDestConnName] = useState('');
  const [savingSourceConn, setSavingSourceConn] = useState(false);
  const [savingDestConn, setSavingDestConn] = useState(false);
  const [sourceConnSaved, setSourceConnSaved] = useState(false);
  const [destConnSaved, setDestConnSaved] = useState(false);
  const [sourceValidated, setSourceValidated] = useState(false);

  useEffect(() => {
    api.sources.list().then(setSources);
    api.connections.list('salesforce').then(setSavedDestConns);
  }, []);

  // Reload source connections whenever the selected source changes
  useEffect(() => {
    if (selectedSource) {
      api.connections.list(selectedSource.id).then(setSavedSourceConns);
    } else {
      setSavedSourceConns([]);
    }
  }, [selectedSource]);

  const validateSource = async () => {
    if (!selectedSource) return;
    setValidating(true);
    setValidationMsg(null);
    try {
      const res = await api.sources.validateCredentials(selectedSource.id, sourceCreds);
      if (res.valid) {
        setValidationMsg('✅ Connection successful');
        setSourceValidated(true);
      } else {
        setValidationMsg('❌ Connection failed');
        setSourceValidated(false);
      }
    } catch {
      setValidationMsg('❌ Could not reach server');
      setSourceValidated(false);
    } finally {
      setValidating(false);
    }
  };

  const saveSourceConnection = async () => {
    if (!sourceConnName.trim() || !selectedSource) return;
    setSavingSourceConn(true);
    try {
      await api.connections.create(sourceConnName.trim(), selectedSource.id, sourceCreds);
      setSourceConnSaved(true);
      const updated = await api.connections.list(selectedSource.id);
      setSavedSourceConns(updated);
    } catch (e: unknown) {
      setValidationMsg(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSavingSourceConn(false);
    }
  };

  const saveDestConnection = async () => {
    if (!destConnName.trim()) return;
    setSavingDestConn(true);
    try {
      await api.connections.create(destConnName.trim(), 'salesforce', destCreds);
      setDestConnSaved(true);
      const updated = await api.connections.list('salesforce');
      setSavedDestConns(updated);
    } catch (e: unknown) {
      setDestValidationMsg(e instanceof Error ? e.message : 'Failed to save');
    } finally {
      setSavingDestConn(false);
    }
  };

  const onSelectSourceConn = (connId: string) => {
    setSelectedSourceConn(connId);
    setSourceValidated(false);
    setValidationMsg(null);
    setSourceConnSaved(false);
    setSourceConnName('');
    if (connId && connId !== 'new') {
      const conn = savedSourceConns.find((c) => c.id === connId);
      if (conn) setSourceCreds(conn.credentials);
    } else {
      setSourceCreds({});
    }
  };

  const onSelectDestConn = (connId: string) => {
    setSelectedDestConn(connId);
    setDestValidated(false);
    setDestValidationMsg(null);
    setDestConnSaved(false);
    setDestConnName('');
    setDiagnostics(null);
    setExpandDetails(false);
    if (connId && connId !== 'new') {
      const conn = savedDestConns.find((c) => c.id === connId);
      if (conn) setDestCreds({ api_version: 'v60.0', ...conn.credentials });
    } else {
      setDestCreds({ api_version: 'v60.0' });
    }
  };

  const [tableLoadError, setTableLoadError] = useState<string | null>(null);

  const loadSourceTables = useCallback(async () => {
    if (!selectedSource) return;
    setLoadingTables(true);
    setTableLoadError(null);
    try {
      const res = await api.schema.sourceTables(selectedSource.id, sourceCreds);
      setSourceTables(res.tables);
      if (res.tables.length === 0) {
        setTableLoadError(
          'The source returned 0 tables. The instance may be hibernating or the user lacks read access to sys_db_object.',
        );
      }
    } catch (err) {
      console.error('Failed to load source tables', err);
      setSourceTables([]);
      setTableLoadError(err instanceof Error ? err.message : 'Failed to fetch source tables');
    } finally {
      setLoadingTables(false);
    }
  }, [selectedSource, sourceCreds]);

  const loadSfObjects = useCallback(async () => {
    setLoadingSfObjects(true);
    try {
      const res = await api.schema.destinationObjects(destCreds);
      setSfObjects(res.objects.map((o) => ({ name: o.name, label: o.label })));
    } catch (err) {
      console.error('Failed to load SF objects', err);
      setSfObjects([]);
    } finally {
      setLoadingSfObjects(false);
    }
  }, [destCreds]);

  const defaultMappingName = (table: string) =>
    table.replace(/^(sc_|sys_)/, '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\s/g, '');

  const syncTableMappings = useCallback((tables: string[]) => {
    setTableMappings((prev) => {
      const existingByTable = new Map<string, TableMappingState[]>();
      for (const m of prev) {
        const arr = existingByTable.get(m.sourceTable) ?? [];
        arr.push(m);
        existingByTable.set(m.sourceTable, arr);
      }
      const tableSet = new Set(tables);
      const result: TableMappingState[] = [];
      for (const t of tables) {
        const existing = existingByTable.get(t);
        if (existing && existing.length > 0) {
          result.push(...existing);
          existingByTable.delete(t);
        } else {
          result.push({
            id: crypto.randomUUID(),
            sourceTable: t,
            sfObject: '',
            mappingName: defaultMappingName(t),
            operation: 'insert' as const,
            priority: 0,
            fields: [],
            sourceFields: [],
            destFields: [],
            fieldsLoaded: false,
            joinTables: [],
            joins: [],
            filters: [],
          });
        }
      }
      // Drop cards whose sourceTable was deselected (not in the new set)
      // but keep split duplicates as long as the base table is still selected
      return result
        .filter((m) => tableSet.has(m.sourceTable))
        .map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, []);

  const moveMapping = useCallback((fromIdx: number, toIdx: number) => {
    setTableMappings((prev) => {
      const next = [...prev];
      const [item] = next.splice(fromIdx, 1);
      next.splice(toIdx, 0, item);
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, []);

  const handleAddMissingTables = useCallback((requestingIdx: number, tables: string[]) => {
    const newTables = tables.filter((t) => !selectedTables.includes(t));
    if (newTables.length === 0) return;
    setSelectedTables((prev) => [...newTables, ...prev]);
    setTableMappings((prev) => {
      const newCards: TableMappingState[] = newTables.map((t) => ({
        id: crypto.randomUUID(),
        sourceTable: t,
        sfObject: '',
        mappingName: defaultMappingName(t),
        operation: 'insert' as const,
        priority: 0,
        fields: [],
        sourceFields: [],
        destFields: [],
        fieldsLoaded: false,
        joinTables: [],
        joins: [],
        filters: [],
      }));
      const next = [...prev];
      next.splice(requestingIdx, 0, ...newCards);
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, [selectedTables]);

  const handleSplitMapping = useCallback((idx: number) => {
    setTableMappings((prev) => {
      const source = prev[idx];
      const suffix = prev.filter((m) => m.sourceTable === source.sourceTable).length + 1;
      const newCard: TableMappingState = {
        id: crypto.randomUUID(),
        sourceTable: source.sourceTable,
        sfObject: '',
        mappingName: `${defaultMappingName(source.sourceTable)}_${suffix}`,
        operation: 'insert',
        priority: 0,
        fields: [],
        sourceFields: source.sourceFields,
        destFields: [],
        fieldsLoaded: false,
        joinTables: [...source.joinTables],
        joins: source.joins.map((j) => ({ ...j })),
        filters: source.filters.map((f) => ({ ...f })),
      };
      const next = [...prev];
      next.splice(idx + 1, 0, newCard);
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, []);

  const handleRemoveMapping = useCallback((idx: number) => {
    setTableMappings((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, []);

  const tableCardCounts = useMemo(() => {
    const counts = new Map<string, number>();
    tableMappings.forEach((m) => counts.set(m.sourceTable, (counts.get(m.sourceTable) ?? 0) + 1));
    return counts;
  }, [tableMappings]);

  const otherMappingsContext = useMemo(
    () =>
      tableMappings
        .filter((m) => m.sfObject && m.mappingName)
        .map((m) => ({
          source_table: m.sourceTable,
          sf_object: m.sfObject,
          mapping_name: m.mappingName,
        })),
    [tableMappings],
  );

  const saveMappingsToDb = async () => {
    setSavingMappings(true);
    try {
      for (let i = 0; i < tableMappings.length; i++) {
        const m = tableMappings[i];
        if (!m.sfObject || !m.mappingName) continue;
        // Emit only complete filters in the exact shape the DuckDB engine reads
        // (table/field always present; count carries its own key).
        const filters = m.filters
          .map((f) => {
            if (f.operator === 'count') {
              if (!f.count || f.count <= 0) return null;
              return { table: m.sourceTable, field: '', operator: 'count', count: f.count };
            }
            if (!f.field) return null;
            if (f.operator === 'is_null' || f.operator === 'is_not_null') {
              return { table: f.table, field: f.field, operator: f.operator };
            }
            if (f.operator === 'in' || f.operator === 'not_in') {
              const list = Array.isArray(f.value) ? f.value.filter(Boolean) : [];
              if (list.length === 0) return null;
              return { table: f.table, field: f.field, operator: f.operator, value: list };
            }
            // equals / not_equals
            const val = typeof f.value === 'string' ? f.value : '';
            if (val === '') return null;
            return { table: f.table, field: f.field, operator: f.operator, value: val };
          })
          .filter((f): f is NonNullable<typeof f> => f !== null);
        const config = {
          destination_object: m.sfObject,
          source_tables: [m.sourceTable, ...m.joinTables],
          joins: m.joins,
          filters,
          operation: m.operation,
          priority: i + 1,
          fields: m.fields,
        };
        const sourceType = selectedSource?.id ?? 'servicenow';
        try {
          await api.mappings.create(m.mappingName, config, sourceType);
        } catch {
          await api.mappings.update(m.mappingName, config, sourceType);
        }
      }
    } catch (err) {
      console.error('Failed to save mappings', err);
    } finally {
      setSavingMappings(false);
    }
  };

  const validateDestination = async () => {
    setValidatingDest(true);
    setDestValidationMsg(null);
    try {
      const res = await api.destinations.validateSalesforceCredentials(destCreds);
      if (res.valid) {
        setDestValidationMsg('Connection successful');
        setDestValidated(true);
      } else {
        setDestValidationMsg(res.message ? `Connection failed: ${res.message}` : 'Connection failed');
        setDestValidated(false);
      }
    } catch {
      setDestValidationMsg('Could not reach server');
      setDestValidated(false);
    } finally {
      setValidatingDest(false);
    }
  };

  const mappingNames = tableMappings.filter((m) => m.sfObject && m.mappingName).map((m) => m.mappingName);

  const duplicateMappingNames = useMemo(() => {
    const counts = new Map<string, number>();
    for (const name of mappingNames) {
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
    return [...counts.entries()].filter(([, c]) => c > 1).map(([name]) => name);
  }, [mappingNames]);

  const checkObjectVisibility = async () => {
    setCheckingDiagnostics(true);
    setDiagnostics(null);
    setExpandDetails(false);
    try {
      const sfObjNames = tableMappings.filter((m) => m.sfObject).map((m) => m.sfObject);
      const res = await api.destinations.checkSalesforceDiagnostics(destCreds, sfObjNames);
      setDiagnostics(res);
    } catch (e: unknown) {
      setDiagnostics({
        valid: false,
        message: e instanceof Error ? e.message : 'Failed to check object visibility',
      });
    } finally {
      setCheckingDiagnostics(false);
    }
  };

  const runCompatibilityCheck = async () => {
    if (!selectedSource) return;
    setCheckingCompat(true);
    setCompatError(null);
    setCompatReport(null);
    setExpandedCompatObj(null);
    try {
      const report = await api.compat.check(
        selectedSource.id,
        sourceCreds,
        destCreds,
        mappingNames,
      );
      setCompatReport(report);
    } catch (e: unknown) {
      setCompatError(e instanceof Error ? e.message : 'Compatibility check failed');
    } finally {
      setCheckingCompat(false);
    }
  };

  const openCreateFieldDialog = (sfObject: string, field: { destination_field: string; suggested_custom_field: string; source_field: string; suggested_field_type?: string }) => {
    const key = `${sfObject}::${field.destination_field}`;
    const suggestedName = field.suggested_custom_field || field.destination_field;
    const label = suggestedName.replace(/__c$/, '').replace(/_/g, ' ');
    setCreatingField(key);
    setCustomFieldName(suggestedName);
    setCustomFieldLabel(label);
    setCustomFieldType(field.suggested_field_type || 'Text');
    setCustomFieldMsg(null);
  };

  const handleCreateCustomField = async (sfObject: string) => {
    if (!customFieldName || !customFieldLabel) return;
    const key = creatingField || '';
    setCustomFieldMsg(null);
    try {
      const body: CreateCustomFieldRequest = {
        destination_credentials: destCreds,
        salesforce_object: sfObject,
        field_label: customFieldLabel,
        field_api_name: customFieldName.endsWith('__c') ? customFieldName : customFieldName + '__c',
        field_type: customFieldType,
        length: 255,
      };
      const res = await api.customFields.create(body);
      setCustomFieldMsg({ key, success: res.success, message: res.message });
      if (res.success) {
        // Update the compatibility report to reflect the newly created field
        setCompatReport((prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            objects: prev.objects.map((obj) => {
              if (obj.salesforce_object !== sfObject) return obj;
              return {
                ...obj,
                fields: obj.fields.map((f) => {
                  if (`${sfObject}::${f.destination_field}` !== key) return f;
                  return { ...f, status: 'mapped' as const, suggestion: `Custom field '${res.field_api_name}' created successfully.`, suggested_custom_field: '' };
                }),
                mapped_fields: obj.mapped_fields + 1,
                missing_fields: obj.missing_fields - 1,
                compatibility_pct: Math.round(((obj.mapped_fields + 1) / obj.total_fields) * 1000) / 10,
              };
            }),
          };
        });
        setCreatingField(null);
      }
    } catch (e: unknown) {
      setCustomFieldMsg({ key, success: false, message: e instanceof Error ? e.message : 'Failed to create custom field' });
    }
  };

  const handleBulkCreateFields = async (sfObject: string, fields: { destination_field: string; suggested_custom_field: string; source_field: string; suggested_field_type?: string }[]) => {
    setBulkCreating(true);
    setBulkResults([]);
    const results: { field: string; success: boolean; message: string }[] = [];

    for (const f of fields) {
      const apiName = f.suggested_custom_field || f.destination_field;
      const label = apiName.replace(/__c$/, '').replace(/_/g, ' ');
      const fieldType = f.suggested_field_type || 'Text';
      const fieldLength = fieldType === 'LongTextArea' ? 32768 : 255;
      try {
        const res = await api.customFields.create({
          destination_credentials: destCreds,
          salesforce_object: sfObject,
          field_label: label,
          field_api_name: apiName.endsWith('__c') ? apiName : apiName + '__c',
          field_type: fieldType,
          length: fieldLength,
        });
        results.push({ field: apiName, success: res.success, message: res.message });
        if (res.success) {
          // Update the compatibility report inline
          setCompatReport((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              objects: prev.objects.map((obj) => {
                if (obj.salesforce_object !== sfObject) return obj;
                return {
                  ...obj,
                  fields: obj.fields.map((of) => {
                    if (of.destination_field !== f.destination_field || of.status !== 'no_mapping') return of;
                    return { ...of, status: 'mapped' as const, suggestion: `Custom field '${res.field_api_name}' created.`, suggested_custom_field: '' };
                  }),
                  mapped_fields: obj.mapped_fields + 1,
                  missing_fields: obj.missing_fields - 1,
                  compatibility_pct: Math.round(((obj.mapped_fields + 1) / obj.total_fields) * 1000) / 10,
                };
              }),
            };
          });
        }
      } catch (e: unknown) {
        results.push({ field: apiName, success: false, message: e instanceof Error ? e.message : 'Failed' });
      }
    }
    setBulkResults(results);
    setBulkCreating(false);
  };

  const goStep = async (s: Step) => {
    setValidationMsg(null);
    setDestValidationMsg(null);
    setDiagnostics(null);
    setExpandDetails(false);
    if (s === 3 && sourceTables.length === 0) loadSourceTables();
    if (s === 4) {
      syncTableMappings(selectedTables);
      if (sfObjects.length === 0) loadSfObjects();
    }
    if (s === 5) {
      await saveMappingsToDb();
      setCompatReport(null);
      setCompatError(null);
    }
    setStep(s);
  };

  const submit = async () => {
    if (!selectedSource) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.migrations.create({
        source_type: selectedSource.id,
        destination_type: 'salesforce',
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
        selected_objects: mappingNames,
        options: {
          ...options,
          service_now_batch_size: options.source_batch_size,
        },
      });
      navigate(`/migrations/${res.session_id}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  // Mapping-heavy steps (field mapping, compatibility) need more horizontal
  // room on larger displays; form-like steps stay narrow for readability.
  const isWideStep = step === 4 || step === 5;
  return (
    <div style={{ ...pageStyle, maxWidth: isWideStep ? 1400 : 820 }}>
      {/* Stepper */}
      <div style={stepperStyle}>
        {(['Select Source', 'Destination', 'Select Tables', 'Map Fields', 'Compatibility', 'Review & Run'] as const).map(
          (label, i) => {
            const stepNum = (i + 1) as Step;
            return (
              <div key={label} style={stepStyle(step === stepNum, step > stepNum)}>
                <div style={stepCircle(step >= stepNum)}>{i + 1}</div>
                <span style={{ fontSize: 13 }}>{label}</span>
              </div>
            );
          },
        )}
      </div>

      <div style={cardStyle}>
        {/* Step 1 — Source */}
        {step === 1 && (
          <div>
            <h2 style={h2}>Select Migration Source</h2>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginBottom: 24 }}>
              {sources.map((s) => (
                <label
                  key={s.id}
                  style={{
                    ...sourceCard,
                    border: selectedSource?.id === s.id ? '2px solid #2563eb' : '1px solid #e5e7eb',
                    background: selectedSource?.id === s.id ? '#eff6ff' : '#fff',
                  }}
                >
                  <input
                    type="radio"
                    name="source"
                    value={s.id}
                    checked={selectedSource?.id === s.id}
                    onChange={() => { setSelectedSource(s); setSourceCreds({}); setSelectedSourceConn(''); setSourceValidated(false); setValidationMsg(null); setSourceConnSaved(false); }}
                  />
                  <div>
                    <div style={{ fontWeight: 600 }}>{s.name}</div>
                    <div style={{ fontSize: 13, color: '#6b7280' }}>{s.description}</div>
                  </div>
                </label>
              ))}
            </div>

            {selectedSource && (
              <div style={{ marginBottom: 20 }}>
                <h3 style={h3}>{selectedSource.name} Credentials</h3>

                {/* Saved connections selector */}
                {savedSourceConns.length > 0 && (
                  <div style={{ marginBottom: 16 }}>
                    <label style={labelStyle}>Use Saved Connection</label>
                    <select
                      value={selectedSourceConn}
                      onChange={(e) => onSelectSourceConn(e.target.value)}
                      style={{ ...inputStyle, marginBottom: 8 }}
                    >
                      <option value="">— Select a saved connection —</option>
                      {savedSourceConns.map((c) => (
                        <option key={c.id} value={c.id}>{c.name}</option>
                      ))}
                      <option value="new">+ Enter new credentials</option>
                    </select>
                  </div>
                )}

                {/* Show credential form for new or no-selection */}
                {(selectedSourceConn === 'new' || selectedSourceConn === '' || savedSourceConns.length === 0) && (
                  <CredentialForm
                    schema={selectedSource.credentials_schema}
                    values={sourceCreds}
                    onChange={(v) => { setSourceCreds(v); setSourceValidated(false); setValidationMsg(null); setSourceConnSaved(false); }}
                  />
                )}

                {/* Show badge for selected saved connection */}
                {selectedSourceConn && selectedSourceConn !== 'new' && (
                  <div style={{ padding: '12px 16px', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 8, fontSize: 13, color: '#166534' }}>
                    Using saved connection: <strong>{savedSourceConns.find((c) => c.id === selectedSourceConn)?.name}</strong>
                  </div>
                )}

                <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
                  <button onClick={validateSource} disabled={validating} style={secondaryBtn}>
                    {validating ? 'Testing…' : 'Test Connection'}
                  </button>
                  {validationMsg && <span style={{ fontSize: 13 }}>{validationMsg}</span>}
                </div>

                {/* Save connection prompt */}
                {sourceValidated && !sourceConnSaved && (!selectedSourceConn || selectedSourceConn === 'new') && (
                  <div style={{ marginTop: 16, padding: 16, background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 8 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: '#1e40af' }}>
                      💾 Save this connection for future use?
                    </div>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <input
                        type="text"
                        placeholder="Connection name (e.g. Dev ServiceNow)"
                        value={sourceConnName}
                        onChange={(e) => setSourceConnName(e.target.value)}
                        style={{ ...inputStyle, flex: 1 }}
                      />
                      <button
                        onClick={saveSourceConnection}
                        disabled={savingSourceConn || !sourceConnName.trim()}
                        style={primaryBtn}
                      >
                        {savingSourceConn ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                  </div>
                )}
                {sourceConnSaved && (
                  <div style={{ marginTop: 8, fontSize: 13, color: '#16a34a' }}>
                    ✅ Connection saved as "{sourceConnName}"
                  </div>
                )}
              </div>
            )}

            <div style={navRow}>
              <span />
              <button
                disabled={!selectedSource}
                onClick={() => goStep(2)}
                style={primaryBtn}
              >
                Next →
              </button>
            </div>
          </div>
        )}

        {/* Step 2 — Destination */}
        {step === 2 && (
          <div>
            <h2 style={h2}>Configure Destination (Salesforce)</h2>

            {/* Saved connections selector */}
            {savedDestConns.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <label style={labelStyle}>Use Saved Connection</label>
                <select
                  value={selectedDestConn}
                  onChange={(e) => onSelectDestConn(e.target.value)}
                  style={{ ...inputStyle, marginBottom: 8 }}
                >
                  <option value="">— Select a saved connection —</option>
                  {savedDestConns.map((c) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                  <option value="new">+ Enter new credentials</option>
                </select>
              </div>
            )}

            {/* Show credential form for new or no-selection */}
            {(selectedDestConn === 'new' || selectedDestConn === '' || savedDestConns.length === 0) && (
              <CredentialForm
                schema={SALESFORCE_SCHEMA as any}
                values={destCreds}
                onChange={(nextCreds) => {
                  setDestCreds(nextCreds);
                  setDestValidated(false);
                  setDestValidationMsg(null);
                  setDiagnostics(null);
                  setExpandDetails(false);
                  setDestConnSaved(false);
                }}
              />
            )}

            {/* Show badge for selected saved connection */}
            {selectedDestConn && selectedDestConn !== 'new' && (
              <div style={{ padding: '12px 16px', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 8, fontSize: 13, color: '#166534', marginBottom: 12 }}>
                Using saved connection: <strong>{savedDestConns.find((c) => c.id === selectedDestConn)?.name}</strong>
              </div>
            )}

            <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
              <button onClick={validateDestination} disabled={validatingDest} style={secondaryBtn}>
                {validatingDest ? 'Testing…' : 'Test Connection'}
              </button>
              {destValidationMsg && <span style={{ fontSize: 13 }}>{destValidationMsg}</span>}
            </div>

            {/* Save connection prompt */}
            {destValidated && !destConnSaved && (!selectedDestConn || selectedDestConn === 'new') && (
              <div style={{ marginTop: 16, padding: 16, background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 8 }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: '#1e40af' }}>
                  💾 Save this connection for future use?
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <input
                    type="text"
                    placeholder="Connection name (e.g. Prod Salesforce)"
                    value={destConnName}
                    onChange={(e) => setDestConnName(e.target.value)}
                    style={{ ...inputStyle, flex: 1 }}
                  />
                  <button
                    onClick={saveDestConnection}
                    disabled={savingDestConn || !destConnName.trim()}
                    style={primaryBtn}
                  >
                    {savingDestConn ? 'Saving…' : 'Save'}
                  </button>
                </div>
              </div>
            )}
            {destConnSaved && (
              <div style={{ marginTop: 8, fontSize: 13, color: '#16a34a' }}>
                ✅ Connection saved as "{destConnName}"
              </div>
            )}

            {/* Object Visibility Check */}
            <div style={{ marginTop: 16, marginBottom: 16 }}>
              <button
                onClick={checkObjectVisibility}
                disabled={checkingDiagnostics || !destValidated}
                style={{
                  ...secondaryBtn,
                  opacity: destValidated ? 1 : 0.6,
                  cursor: destValidated ? 'pointer' : 'not-allowed',
                }}
                title={destValidated ? 'Check which selected objects are accessible' : 'Test connection first to check object visibility'}
              >
                {checkingDiagnostics ? 'Checking…' : '🔍 Check Object Visibility'}
              </button>
            </div>

            {/* Diagnostics Results */}
            {diagnostics && (
              <div
                style={{
                  border: diagnostics.missing_objects && diagnostics.missing_objects.length > 0
                    ? '1px solid #f87171'
                    : '1px solid #86efac',
                  background: diagnostics.missing_objects && diagnostics.missing_objects.length > 0
                    ? '#fef2f2'
                    : '#f0fdf4',
                  borderRadius: 8,
                  padding: 14,
                  marginBottom: 16,
                }}
              >
                <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>
                  {diagnostics.missing_objects && diagnostics.missing_objects.length > 0
                    ? '⚠️ Some objects may not be accessible'
                    : '✅ All objects are accessible'}
                </div>
                <div style={{ fontSize: 12, color: '#374151', marginBottom: 6 }}>
                  <strong>Run-as User:</strong> {diagnostics.run_as_user_id || 'N/A'}
                </div>
                {diagnostics.missing_objects && diagnostics.missing_objects.length > 0 && (
                  <div style={{ fontSize: 12, color: '#ef4444', marginBottom: 6 }}>
                    <strong>Missing Objects:</strong> {diagnostics.missing_objects.join(', ')}
                  </div>
                )}
                {diagnostics.required_objects && diagnostics.required_objects.length > 0 && (
                  <button
                    onClick={() => setExpandDetails(!expandDetails)}
                    style={{
                      background: 'none',
                      border: 'none',
                      color: '#2563eb',
                      cursor: 'pointer',
                      fontSize: 12,
                      padding: '4px 0',
                      fontWeight: 500,
                    }}
                  >
                    {expandDetails ? '▼' : '▶'} Required Objects & Visibility ({diagnostics.required_objects.length})
                  </button>
                )}
                {expandDetails && diagnostics.required_objects && (
                  <div style={{ fontSize: 12, marginTop: 8, paddingTop: 8, borderTop: '1px solid rgba(0,0,0,0.1)' }}>
                    {diagnostics.required_objects.map((obj) => (
                      <div
                        key={obj}
                        style={{
                          padding: '4px 0',
                          color: diagnostics.visibility?.[obj] === false ? '#ef4444' : '#059669',
                        }}
                      >
                        {diagnostics.visibility?.[obj] === false ? '✗' : '✓'} {obj}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div style={navRow}>
              <button onClick={() => goStep(1)} style={secondaryBtn}>← Back</button>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
                <button
                  onClick={() => goStep(3)}
                  disabled={!destValidated || (diagnostics?.missing_objects && diagnostics.missing_objects.length > 0)}
                  style={{
                    ...primaryBtn,
                    opacity: !destValidated || (diagnostics?.missing_objects && diagnostics.missing_objects.length > 0) ? 0.6 : 1,
                    cursor: !destValidated || (diagnostics?.missing_objects && diagnostics.missing_objects.length > 0) ? 'not-allowed' : 'pointer',
                  }}
                  title={
                    !destValidated
                      ? 'Test Salesforce connection successfully before continuing'
                      : diagnostics?.missing_objects && diagnostics.missing_objects.length > 0
                        ? 'Some required objects are not accessible to the Salesforce user'
                        : undefined
                  }
                >
                  Next →
                </button>
                {!destValidated && (
                  <span style={{ fontSize: 12, color: '#b45309' }}>
                    Please test Salesforce connection successfully before continuing.
                  </span>
                )}
                {diagnostics?.missing_objects && diagnostics.missing_objects.length > 0 && (
                  <span style={{ fontSize: 12, color: '#dc2626' }}>
                    Required objects not accessible. Grant permissions and re-check.
                  </span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Step 3 — Select Source Tables (live) */}
        {step === 3 && (
          <div>
            <h2 style={h2}>Select Source Tables</h2>
            <p style={{ color: '#6b7280', fontSize: 13, marginBottom: 16 }}>
              Select the {selectedSource?.name ?? 'source'} tables you want to migrate.
              {!loadingTables && ` ${selectedTables.length} of ${sourceTables.length} selected.`}
            </p>
            {tableLoadError && (
              <div style={{
                padding: 12, marginBottom: 12, borderRadius: 8,
                background: '#fef2f2', border: '1px solid #fca5a5', fontSize: 13, color: '#991b1b',
              }}>
                {tableLoadError}
                <button
                  onClick={loadSourceTables}
                  disabled={loadingTables}
                  style={{ marginLeft: 12, padding: '4px 12px', borderRadius: 6, border: '1px solid #d1d5db', background: '#fff', cursor: 'pointer', fontSize: 12 }}
                >
                  Retry
                </button>
              </div>
            )}
            <SourceTableSelector
              tables={sourceTables}
              selected={selectedTables}
              onChange={setSelectedTables}
              loading={loadingTables}
            />
            <div style={navRow}>
              <button onClick={() => goStep(2)} style={secondaryBtn}>← Back</button>
              <button
                disabled={selectedTables.length === 0}
                onClick={() => goStep(4)}
                style={primaryBtn}
              >
                Next →
              </button>
            </div>
          </div>
        )}

        {/* Step 4 — Map to Salesforce (inline mapping) */}
        {step === 4 && (
          <div>
            <h2 style={h2}>Map to Salesforce</h2>
            <p style={{ color: '#6b7280', fontSize: 13, marginBottom: 16 }}>
              For each selected source table, choose a Salesforce object and configure field mappings.
              {loadingSfObjects && ' Loading Salesforce objects...'}
            </p>

            {savingMappings && (
              <div style={{ padding: 12, background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 8, marginBottom: 12, fontSize: 13 }}>
                Saving mappings...
              </div>
            )}

            {tableMappings.map((m, idx) => (
              <TableMappingCard
                key={m.id}
                state={m}
                index={idx}
                total={tableMappings.length}
                sfObjects={sfObjects}
                sourceType={selectedSource?.id ?? 'servicenow'}
                sourceCreds={sourceCreds}
                destCreds={destCreds}
                selectedTables={selectedTables}
                otherMappings={otherMappingsContext}
                onChange={(next) =>
                  setTableMappings((prev) => prev.map((p, i) => (i === idx ? next : p)).map((p, i) => ({ ...p, priority: i + 1 })))
                }
                onMoveUp={() => moveMapping(idx, idx - 1)}
                onMoveDown={() => moveMapping(idx, idx + 1)}
                onAddTables={(tables) => handleAddMissingTables(idx, tables)}
                onSplit={() => handleSplitMapping(idx)}
                onRemove={() => handleRemoveMapping(idx)}
                canRemove={(tableCardCounts.get(m.sourceTable) ?? 1) > 1}
              />
            ))}

            {tableMappings.length === 0 && (
              <p style={{ color: '#9ca3af', textAlign: 'center', padding: '32px 0' }}>
                No tables selected. Go back to Step 3 and select tables.
              </p>
            )}

            {duplicateMappingNames.length > 0 && (
              <div style={{
                padding: 12, marginBottom: 12, borderRadius: 8,
                background: '#fef2f2', border: '1px solid #fca5a5', fontSize: 13, color: '#991b1b',
              }}>
                <strong>Duplicate mapping names detected:</strong> {duplicateMappingNames.join(', ')}.
                Each mapping must have a unique name. Rename the duplicates before proceeding.
              </div>
            )}

            <div style={navRow}>
              <button onClick={() => goStep(3)} style={secondaryBtn}>← Back</button>
              <button
                disabled={mappingNames.length === 0 || savingMappings || duplicateMappingNames.length > 0}
                onClick={() => goStep(5)}
                style={primaryBtn}
              >
                {savingMappings ? 'Saving...' : 'Next →'}
              </button>
            </div>
          </div>
        )}

        {/* Step 5 — Compatibility Check */}
        {step === 5 && (
          <div>
            <h2 style={h2}>Compatibility Check</h2>
            <p style={{ color: '#6b7280', fontSize: 13, marginBottom: 16 }}>
              Analyse how well your mappings work with the Salesforce destination.
              This is a dry-run analysis — no data is moved.
            </p>

            <div style={{ marginBottom: 16, display: 'flex', gap: 12, alignItems: 'center' }}>
              <button
                onClick={runCompatibilityCheck}
                disabled={checkingCompat}
                style={primaryBtn}
              >
                {checkingCompat ? 'Checking...' : 'Run Compatibility Check'}
              </button>
            </div>

            {compatError && (
              <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 12 }}>
                {compatError}
              </div>
            )}

            {compatReport && (
              <div style={{ marginBottom: 20 }}>
                {/* Overall score banner */}
                <div style={{
                  padding: 14, borderRadius: 8, marginBottom: 16,
                  background: compatBg(compatReport.overall_pct),
                  border: `1px solid ${compatBorder(compatReport.overall_pct)}`,
                }}>
                  <div style={{ fontWeight: 700, fontSize: 16 }}>
                    Overall Compatibility: {compatReport.overall_pct}%
                  </div>
                  <div style={{ fontSize: 13, color: '#374151', marginTop: 4 }}>{compatReport.summary}</div>
                </div>

                {/* Per-object breakdown */}
                {compatReport.objects.map((obj) => (
                  <div key={obj.object_name} style={{
                    border: '1px solid #e5e7eb', borderRadius: 8, marginBottom: 10, overflow: 'hidden',
                  }}>
                    <div
                      onClick={() => setExpandedCompatObj(expandedCompatObj === obj.object_name ? null : obj.object_name)}
                      style={{
                        padding: '10px 14px', background: '#f9fafb', cursor: 'pointer',
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      }}
                    >
                      <div>
                        <strong>{obj.source_label}</strong>
                        <span style={{ fontSize: 12, color: '#6b7280', marginLeft: 8 }}>{'->'} {obj.salesforce_object}</span>
                      </div>
                      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                        <span style={{
                          fontSize: 13, fontWeight: 600,
                          color: compatColor(obj.compatibility_pct),
                        }}>
                          {obj.compatibility_pct}%
                        </span>
                        <span style={{ fontSize: 12, color: '#6b7280' }}>
                          {obj.mapped_fields}/{obj.total_fields} fields mapped
                        </span>
                        {obj.type_mismatches > 0 && (
                          <span style={{ fontSize: 11, color: '#92400e', background: '#fef9c3', padding: '1px 6px', borderRadius: 4 }}>
                            {obj.type_mismatches} type mismatch{obj.type_mismatches > 1 ? 'es' : ''}
                          </span>
                        )}
                        <span>{expandedCompatObj === obj.object_name ? '\u25BC' : '\u25B6'}</span>
                      </div>
                    </div>
                    {expandedCompatObj === obj.object_name && (
                      <div style={{ padding: 14 }}>
                        {obj.blockers.length > 0 && (
                          <div style={{ marginBottom: 10, padding: 10, background: '#fef2f2', borderRadius: 6, border: '1px solid #fca5a5' }}>
                            {obj.blockers.map((b, i) => (
                              <div key={i} style={{ fontSize: 12, color: '#dc2626' }}>{b}</div>
                            ))}
                          </div>
                        )}

                        {/* Bulk create all missing fields button */}
                        {obj.fields.some((f) => f.status === 'no_mapping' && f.suggested_custom_field) && (
                          <div style={{ marginBottom: 12, padding: 12, background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <div>
                              <div style={{ fontSize: 13, fontWeight: 600, color: '#1e40af' }}>
                                {obj.fields.filter((f) => f.status === 'no_mapping' && f.suggested_custom_field).length} missing field(s) detected
                              </div>
                              <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>
                                Create all missing fields in Salesforce with auto-detected types
                              </div>
                            </div>
                            <button
                              onClick={() => handleBulkCreateFields(
                                obj.salesforce_object,
                                obj.fields.filter((f) => f.status === 'no_mapping' && f.suggested_custom_field).map((f) => ({
                                  destination_field: f.destination_field,
                                  suggested_custom_field: f.suggested_custom_field,
                                  source_field: f.source_field,
                                  suggested_field_type: f.suggested_field_type,
                                }))
                              )}
                              disabled={bulkCreating}
                              style={{
                                padding: '8px 16px', fontSize: 12, background: '#2563eb', color: '#fff',
                                border: 'none', borderRadius: 6, cursor: 'pointer', fontWeight: 600, whiteSpace: 'nowrap',
                              }}
                            >
                              {bulkCreating ? 'Creating...' : 'Create All Missing Fields'}
                            </button>
                          </div>
                        )}

                        {/* Bulk creation results */}
                        {bulkResults.length > 0 && (
                          <div style={{ marginBottom: 12, padding: 10, background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 6, maxHeight: 150, overflowY: 'auto' }}>
                            <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 6 }}>Creation Results:</div>
                            {bulkResults.map((r, i) => (
                              <div key={i} style={{ fontSize: 11, color: r.success ? '#16a34a' : '#dc2626', marginBottom: 2 }}>
                                {r.field}: {r.message}
                              </div>
                            ))}
                          </div>
                        )}
                        <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
                          <thead>
                            <tr style={{ background: '#f3f4f6' }}>
                              <th style={thStyle}>Source Field</th>
                              <th style={thStyle}>Salesforce Field</th>
                              <th style={thStyle}>Status</th>
                              <th style={thStyle}>Suggestion</th>
                              <th style={thStyle}>Action</th>
                            </tr>
                          </thead>
                          <tbody>
                            {obj.fields.map((f, i) => {
                              const fieldKey = `${obj.salesforce_object}::${f.destination_field}`;
                              return (
                              <tr key={i} style={{ borderBottom: '1px solid #e5e7eb' }}>
                                <td style={tdStyle}>
                                  {f.source_field || '\u2014'}
                                  {f.source_type && f.status === 'type_mismatch' && (
                                    <span style={{ fontSize: 10, color: '#6b7280', marginLeft: 4 }}>({f.source_type})</span>
                                  )}
                                </td>
                                <td style={tdStyle}>
                                  {f.destination_field}
                                  {f.destination_type && f.status === 'type_mismatch' && (
                                    <span style={{ fontSize: 10, color: '#6b7280', marginLeft: 4 }}>({f.destination_type})</span>
                                  )}
                                </td>
                                <td style={tdStyle}>
                                  <span style={{
                                    padding: '2px 6px', borderRadius: 4, fontSize: 11,
                                    background: f.status === 'mapped' ? '#dcfce7' : f.status === 'type_mismatch' ? '#fef9c3' : '#fee2e2',
                                    color: f.status === 'mapped' ? '#166534' : f.status === 'type_mismatch' ? '#854d0e' : '#991b1b',
                                  }}>
                                    {f.status === 'type_mismatch' ? 'type mismatch' : f.status}
                                  </span>
                                </td>
                                <td style={{ ...tdStyle, color: '#6b7280' }}>{f.suggestion || '\u2014'}</td>
                                <td style={tdStyle}>
                                  {f.status === 'no_mapping' && f.suggested_custom_field && (
                                    <button
                                      onClick={() => openCreateFieldDialog(obj.salesforce_object, f)}
                                      style={{
                                        padding: '3px 8px', fontSize: 11, background: '#2563eb', color: '#fff',
                                        border: 'none', borderRadius: 4, cursor: 'pointer', whiteSpace: 'nowrap',
                                      }}
                                    >
                                      + Create Field
                                    </button>
                                  )}
                                  {customFieldMsg && customFieldMsg.key === fieldKey && (
                                    <div style={{ fontSize: 11, marginTop: 4, color: customFieldMsg.success ? '#16a34a' : '#dc2626' }}>
                                      {customFieldMsg.message}
                                    </div>
                                  )}
                                </td>
                              </tr>
                              );
                            })}
                          </tbody>
                        </table>

                        {/* Create Custom Field Dialog */}
                        {creatingField && creatingField.startsWith(`${obj.salesforce_object}::`) && (
                          <div style={{
                            marginTop: 12, padding: 16, background: '#f0f9ff', border: '1px solid #bae6fd',
                            borderRadius: 8,
                          }}>
                            <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 10 }}>
                              Create Custom Field on {obj.salesforce_object}
                            </div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
                              <div>
                                <label style={{ fontSize: 11, fontWeight: 500, display: 'block', marginBottom: 2 }}>Field Label</label>
                                <input
                                  type="text"
                                  value={customFieldLabel}
                                  onChange={(e) => setCustomFieldLabel(e.target.value)}
                                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #d1d5db', borderRadius: 4, fontSize: 12, boxSizing: 'border-box' }}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: 11, fontWeight: 500, display: 'block', marginBottom: 2 }}>API Name</label>
                                <input
                                  type="text"
                                  value={customFieldName}
                                  onChange={(e) => setCustomFieldName(e.target.value)}
                                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #d1d5db', borderRadius: 4, fontSize: 12, boxSizing: 'border-box' }}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: 11, fontWeight: 500, display: 'block', marginBottom: 2 }}>Field Type</label>
                                <select
                                  value={customFieldType}
                                  onChange={(e) => setCustomFieldType(e.target.value)}
                                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #d1d5db', borderRadius: 4, fontSize: 12, boxSizing: 'border-box' }}
                                >
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
                              </div>
                            </div>
                            <div style={{ display: 'flex', gap: 8 }}>
                              <button
                                onClick={() => handleCreateCustomField(obj.salesforce_object)}
                                style={{
                                  padding: '6px 14px', fontSize: 12, background: '#16a34a', color: '#fff',
                                  border: 'none', borderRadius: 4, cursor: 'pointer', fontWeight: 600,
                                }}
                              >
                                Create Field in Salesforce
                              </button>
                              <button
                                onClick={() => { setCreatingField(null); setCustomFieldMsg(null); }}
                                style={{
                                  padding: '6px 14px', fontSize: 12, background: '#fff', color: '#374151',
                                  border: '1px solid #d1d5db', borderRadius: 4, cursor: 'pointer',
                                }}
                              >
                                Cancel
                              </button>
                            </div>
                            {customFieldMsg && customFieldMsg.key === creatingField && (
                              <div style={{ marginTop: 8, fontSize: 12, color: customFieldMsg.success ? '#16a34a' : '#dc2626' }}>
                                {customFieldMsg.message}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}

                {/* Execution Order section */}
                {compatReport.dependency_order && compatReport.dependency_order.length > 0 && (
                  <div style={{ marginTop: 20 }}>
                    <h3 style={{ margin: '0 0 10px', fontSize: 15, fontWeight: 700 }}>Execution Order</h3>
                    <p style={{ fontSize: 12, color: '#6b7280', marginBottom: 10 }}>
                      Objects will be migrated in this order based on Salesforce reference field dependencies.
                      {compatReport.dependency_has_cycle && (
                        <span style={{ color: '#dc2626', fontWeight: 600 }}>
                          {' '}Circular dependency detected — using card order instead.
                        </span>
                      )}
                    </p>

                    {/* Missing parent warnings */}
                    {compatReport.dependency_missing_parents && compatReport.dependency_missing_parents.length > 0 && (
                      <div style={{
                        padding: 12, marginBottom: 12, borderRadius: 8,
                        background: '#fffbeb', border: '1px solid #fcd34d',
                      }}>
                        <div style={{ fontSize: 12, fontWeight: 600, color: '#92400e', marginBottom: 6 }}>
                          Missing Parent Object Warnings
                        </div>
                        {compatReport.dependency_missing_parents.map((mp, i) => (
                          <div key={i} style={{ fontSize: 12, color: '#92400e', marginBottom: 3 }}>
                            <strong>{mp.mapping}</strong> field <code style={{ background: '#fef3c7', padding: '1px 4px', borderRadius: 3 }}>{mp.field}</code> references <strong>{mp.references}</strong> — {mp.reason}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Self-reference / cycle warnings */}
                    {compatReport.dependency_warnings && compatReport.dependency_warnings.length > 0 && (
                      <div style={{
                        padding: 12, marginBottom: 12, borderRadius: 8,
                        background: '#fef2f2', border: '1px solid #fca5a5',
                      }}>
                        {compatReport.dependency_warnings.map((w, i) => (
                          <div key={i} style={{ fontSize: 12, color: '#991b1b', marginBottom: 3 }}>
                            {w.mapping ? <><strong>{w.mapping}</strong> — </> : null}{w.reason}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Ordered list with dependency arrows */}
                    <div style={{
                      border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden',
                    }}>
                      {compatReport.dependency_order.map((name, i) => {
                        const edges = (compatReport.dependency_edges ?? []).filter((e) => e.from_mapping === name);
                        const objInfo = compatReport.objects.find((o) => o.object_name === name);
                        return (
                          <div key={name} style={{
                            padding: '10px 14px',
                            borderBottom: i < compatReport.dependency_order!.length - 1 ? '1px solid #e5e7eb' : 'none',
                            background: i % 2 === 0 ? '#f9fafb' : '#fff',
                            display: 'flex', alignItems: 'center', gap: 12,
                          }}>
                            <div style={{
                              width: 28, height: 28, borderRadius: '50%',
                              background: '#2563eb', color: '#fff',
                              display: 'flex', alignItems: 'center', justifyContent: 'center',
                              fontSize: 12, fontWeight: 700, flexShrink: 0,
                            }}>
                              {i + 1}
                            </div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontWeight: 600, fontSize: 13 }}>
                                {name}
                                {objInfo && (
                                  <span style={{ fontWeight: 400, color: '#6b7280', marginLeft: 8, fontSize: 12 }}>
                                    → {objInfo.salesforce_object}
                                  </span>
                                )}
                              </div>
                              {edges.length > 0 && (
                                <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>
                                  depends on:{' '}
                                  {edges.map((e, ei) => (
                                    <span key={ei}>
                                      {ei > 0 && ', '}
                                      <span style={{
                                        padding: '1px 5px', borderRadius: 3, fontSize: 10,
                                        background: e.required ? '#fee2e2' : '#f0f9ff',
                                        color: e.required ? '#991b1b' : '#1e40af',
                                        border: e.required ? '1px solid #fca5a5' : '1px dashed #93c5fd',
                                      }}>
                                        {e.to_mapping}
                                        <span style={{ opacity: 0.7 }}> via {e.field}</span>
                                        {e.required && <span style={{ fontWeight: 600 }}> (required)</span>}
                                      </span>
                                    </span>
                                  ))}
                                </div>
                              )}
                            </div>
                            {i > 0 && (
                              <div style={{ fontSize: 16, color: '#d1d5db', flexShrink: 0 }}>↓</div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}

            <div style={navRow}>
              <button onClick={() => goStep(4)} style={secondaryBtn}>&larr; Back</button>
              <button onClick={() => goStep(6)} style={primaryBtn}>Next &rarr;</button>
            </div>
          </div>
        )}

        {/* Step 6 — Options & Review */}
        {step === 6 && (
          <div>
            <h2 style={h2}>Review & Run</h2>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
              <div>
                <label style={labelStyle}>Start Date (optional)</label>
                <input
                  type="date"
                  value={options.start_date}
                  onChange={(e) => setOptions((o) => ({ ...o, start_date: e.target.value }))}
                  style={inputStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>End Date (optional)</label>
                <input
                  type="date"
                  value={options.end_date}
                  onChange={(e) => setOptions((o) => ({ ...o, end_date: e.target.value }))}
                  style={inputStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>Source Batch Size</label>
                <input
                  type="number"
                  value={options.source_batch_size}
                  onChange={(e) => setOptions((o) => ({ ...o, source_batch_size: +e.target.value }))}
                  style={inputStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>Salesforce Batch Size</label>
                <input
                  type="number"
                  value={options.salesforce_batch_size}
                  onChange={(e) => setOptions((o) => ({ ...o, salesforce_batch_size: +e.target.value }))}
                  style={inputStyle}
                />
              </div>
            </div>

            <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 24, fontSize: 14 }}>
              <input
                type="checkbox"
                checked={options.disable_load}
                onChange={(e) => setOptions((o) => ({ ...o, disable_load: e.target.checked }))}
              />
              Dry Run (extract & transform only, skip Salesforce upload)
            </label>

            {/* Summary */}
            <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 8, padding: 16, marginBottom: 20 }}>
              <h3 style={{ margin: '0 0 12px', fontSize: 15 }}>Summary</h3>
              <div style={{ fontSize: 13, display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div><strong>Source:</strong> {selectedSource?.name}</div>
                <div><strong>Destination:</strong> Salesforce ({destCreds.base_url || 'not set'})</div>
                <div><strong>Mappings:</strong> {mappingNames.length} configured</div>
                {compatReport?.dependency_order && compatReport.dependency_order.length > 0 ? (
                  <div style={{ marginLeft: 16 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Execution Order:</div>
                    {compatReport.dependency_order.map((name, i) => {
                      const objInfo = compatReport.objects.find((o) => o.object_name === name);
                      return (
                        <div key={name} style={{ fontSize: 12, color: '#6b7280', display: 'flex', gap: 6, alignItems: 'center', marginBottom: 2 }}>
                          <span style={{
                            width: 18, height: 18, borderRadius: '50%', background: '#2563eb', color: '#fff',
                            display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, fontWeight: 700, flexShrink: 0,
                          }}>
                            {i + 1}
                          </span>
                          {name}{objInfo ? ` → ${objInfo.salesforce_object}` : ''}
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div style={{ fontSize: 12, color: '#6b7280', marginLeft: 16 }}>
                    {tableMappings
                      .filter((m) => m.sfObject && m.mappingName)
                      .map((m) => `${m.sourceTable} -> ${m.sfObject} (${m.mappingName})`)
                      .join(', ')}
                  </div>
                )}
                {compatReport && (
                  <div>
                    <strong>Compatibility:</strong>{' '}
                    <span style={{ color: compatColor(compatReport.overall_pct) }}>
                      {compatReport.overall_pct}%
                    </span>
                  </div>
                )}

                {/* Per-object field summary */}
                {compatReport && compatReport.objects.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    <strong>Field Mappings:</strong>
                    <div style={{ marginLeft: 16, marginTop: 4 }}>
                      {compatReport.objects.map((obj) => (
                        <div key={obj.object_name} style={{ fontSize: 12, color: '#6b7280', marginBottom: 2, display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span>{obj.object_name} → {obj.salesforce_object}:</span>
                          <span style={{ color: compatColor(obj.compatibility_pct), fontWeight: 600 }}>
                            {obj.mapped_fields}/{obj.total_fields} fields
                          </span>
                          {obj.type_mismatches > 0 && (
                            <span style={{
                              fontSize: 10, padding: '1px 5px', borderRadius: 4,
                              background: '#fef9c3', color: '#92400e',
                            }}>
                              {obj.type_mismatches} type mismatch{obj.type_mismatches > 1 ? 'es' : ''}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div><strong>Dry run:</strong> {options.disable_load ? 'Yes (extract & transform only)' : 'No'}</div>
              </div>
            </div>

            {/* Dependency warnings banner */}
            {compatReport && (
              (compatReport.dependency_warnings ?? []).length > 0 ||
              (compatReport.dependency_missing_parents ?? []).length > 0
            ) && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#fffbeb', border: '1px solid #fcd34d',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#92400e', marginBottom: 8 }}>
                  Dependency Warnings
                </div>
                {(compatReport.dependency_warnings ?? []).map((w, i) => (
                  <div key={`w-${i}`} style={{ fontSize: 12, color: '#78350f', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>⚠</span>
                    <span>{w.mapping ? `${w.mapping} — ` : ''}{w.reason}</span>
                  </div>
                ))}
                {(compatReport.dependency_missing_parents ?? []).map((mp, i) => (
                  <div key={`mp-${i}`} style={{ fontSize: 12, color: '#78350f', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>⚠</span>
                    <span>{mp.mapping} — {mp.reason}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Blocking required-field banner — hard gate before launch */}
            {compatReport && (compatReport.blocking_required_fields ?? []).length > 0 && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#fef2f2', border: '1px solid #fecaca',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#991b1b', marginBottom: 8 }}>
                  Resolve these before launching
                </div>
                {(compatReport.blocking_required_fields ?? []).map((b, i) => (
                  <div key={`b-${i}`} style={{ fontSize: 12, color: '#7f1d1d', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>✕</span>
                    <span><b>{b.mapping}.{b.field}</b> — {b.reason}</span>
                  </div>
                ))}
                <div style={{ fontSize: 11, color: '#991b1b', marginTop: 8 }}>
                  Fix these in Step 4 (set a Source field, Constant, or Lookup), then re-run the compatibility check.
                </div>
              </div>
            )}

            {/* Picklist value-mapping warnings banner (non-blocking) */}
            {compatReport && (compatReport.picklist_mapping_warnings ?? []).length > 0 && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#fef9c3', border: '1px solid #fde68a',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#854d0e', marginBottom: 8 }}>
                  Picklist values not fully mapped
                </div>
                {(compatReport.picklist_mapping_warnings ?? []).map((p, i) => (
                  <div key={`pk-${i}`} style={{ fontSize: 12, color: '#78350f', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>⚠</span>
                    <span><b>{p.mapping}.{p.field}</b> — {p.reason}</span>
                  </div>
                ))}
                <div style={{ fontSize: 11, color: '#854d0e', marginTop: 8 }}>
                  Unmapped values load as blank. This does not block the migration.
                </div>
              </div>
            )}

            {/* Type mismatch warnings banner */}
            {compatReport && compatReport.objects.some((o) => o.type_mismatches > 0) && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#fef9c3', border: '1px solid #fde68a',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#854d0e', marginBottom: 8 }}>
                  Type Mismatch Warnings
                </div>
                {compatReport.objects
                  .filter((o) => o.type_mismatches > 0)
                  .map((obj) => (
                    <div key={obj.object_name} style={{ marginBottom: 6 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: '#92400e' }}>
                        {obj.object_name} → {obj.salesforce_object} ({obj.type_mismatches} mismatch{obj.type_mismatches > 1 ? 'es' : ''})
                      </div>
                      {obj.fields
                        .filter((f) => f.status === 'type_mismatch')
                        .map((f, fi) => (
                          <div key={fi} style={{ fontSize: 11, color: '#78350f', marginLeft: 16, marginTop: 2 }}>
                            {f.source_field} ({f.source_type}) → {f.destination_field} ({f.destination_type})
                            {f.suggestion && <span style={{ color: '#92400e' }}> — {f.suggestion}</span>}
                          </div>
                        ))}
                    </div>
                  ))}
              </div>
            )}

            {error && <p style={{ color: '#dc2626', fontSize: 13 }}>Error: {error}</p>}

            <div style={navRow}>
              <button onClick={() => goStep(5)} style={secondaryBtn}>&larr; Back</button>
              <button
                onClick={submit}
                disabled={submitting || (compatReport?.blocking_required_fields ?? []).length > 0}
                style={primaryBtn}
                title={
                  (compatReport?.blocking_required_fields ?? []).length > 0
                    ? 'Resolve the required-field blockers above before launching'
                    : ''
                }
              >
                {submitting ? 'Creating...' : 'Create Migration'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────
const pageStyle: React.CSSProperties = { maxWidth: 820, margin: '0 auto', padding: '32px 24px', transition: 'max-width 0.2s ease' };
const h2: React.CSSProperties = { margin: '0 0 20px', fontSize: 20, fontWeight: 700 };
const h3: React.CSSProperties = { margin: '0 0 12px', fontSize: 15, fontWeight: 600 };
const cardStyle: React.CSSProperties = { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: 28 };
const navRow: React.CSSProperties = { display: 'flex', justifyContent: 'space-between', marginTop: 24 };
const primaryBtn: React.CSSProperties = {
  padding: '10px 22px', background: '#2563eb', color: '#fff', border: 'none',
  borderRadius: 8, cursor: 'pointer', fontWeight: 600, fontSize: 14,
};
const secondaryBtn: React.CSSProperties = {
  padding: '10px 22px', background: '#fff', color: '#374151', border: '1px solid #d1d5db',
  borderRadius: 8, cursor: 'pointer', fontWeight: 600, fontSize: 14,
};
// ── Compatibility colour helpers ───────────────────────────────────────────────
const COMPAT_HIGH = 80;  // pct threshold for "good" (green)
const COMPAT_MID  = 50;  // pct threshold for "warning" (amber)

function compatColor(pct: number): string {
  return pct >= COMPAT_HIGH ? '#16a34a' : pct >= COMPAT_MID ? '#b45309' : '#dc2626';
}
function compatBg(pct: number): string {
  return pct >= COMPAT_HIGH ? '#f0fdf4' : pct >= COMPAT_MID ? '#fffbeb' : '#fef2f2';
}
function compatBorder(pct: number): string {
  return pct >= COMPAT_HIGH ? '#86efac' : pct >= COMPAT_MID ? '#fcd34d' : '#f87171';
}
function compatIcon(pct: number): string {
  return pct >= COMPAT_HIGH ? '✅' : pct >= COMPAT_MID ? '⚠️' : '❌';
}

const thStyle: React.CSSProperties = {
  padding: '6px 10px', textAlign: 'left', fontWeight: 600,
  borderBottom: '1px solid #d1d5db',
};
const tdStyle: React.CSSProperties = { padding: '5px 10px', verticalAlign: 'top' };
const sourceCard: React.CSSProperties = {
  display: 'flex', gap: 12, alignItems: 'flex-start', padding: '14px 16px',
  borderRadius: 10, cursor: 'pointer',
};
const inputStyle: React.CSSProperties = {
  width: '100%', padding: '8px 10px', border: '1px solid #d1d5db',
  borderRadius: 6, fontSize: 14, boxSizing: 'border-box',
};
const labelStyle: React.CSSProperties = { fontSize: 13, fontWeight: 500, display: 'block', marginBottom: 4 };
const stepperStyle: React.CSSProperties = {
  display: 'flex', gap: 0, marginBottom: 28, background: '#fff',
  border: '1px solid #e5e7eb', borderRadius: 12, padding: '16px 24px',
  justifyContent: 'space-between',
};
const stepStyle = (active: boolean, done: boolean): React.CSSProperties => ({
  display: 'flex', alignItems: 'center', gap: 8,
  opacity: active || done ? 1 : 0.45, fontWeight: active ? 600 : 400,
});
const stepCircle = (active: boolean): React.CSSProperties => ({
  width: 24, height: 24, borderRadius: '50%', display: 'flex', alignItems: 'center',
  justifyContent: 'center', fontSize: 12, fontWeight: 700,
  background: active ? '#2563eb' : '#e5e7eb', color: active ? '#fff' : '#6b7280',
});
