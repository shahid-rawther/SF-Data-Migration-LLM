import { useEffect, useState, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Source, SalesforceDiagnosticsResponse, SavedConnection, CompatibilityReport, CreateCustomFieldRequest, CredentialsSchema, DeferredLookupNotice, PreflightResult, WorkflowReport } from '../api/client';
import { WorkflowCheckPanel } from '../components/WorkflowCheckPanel';
import { ConnectionPicker } from '../components/ConnectionPicker';
import { DataReadinessPanel } from '../components/DataReadinessPanel';
import { Disclosure } from '../components/Disclosure';
import { DestinationHierarchy } from '../components/DestinationHierarchy';
import { DestinationObjectSelector } from '../components/DestinationObjectSelector';
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
  // The destination side of the selection. It drives the dependency hierarchy
  // and the order advice; the per-mapping target is still chosen in step 4.
  const [selectedSfObjects, setSelectedSfObjects] = useState<string[]>([]);
  const [sfObjects, setSfObjects] = useState<{ name: string; label: string }[]>([]);
  const [loadingSfObjects, setLoadingSfObjects] = useState(false);
  const [tableMappings, setTableMappings] = useState<TableMappingState[]>([]);
  const [savingMappings, setSavingMappings] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Every mapping already saved, so a new card never reuses one of their names.
  const [savedMappingNames, setSavedMappingNames] = useState<Set<string>>(new Set());
  const [options, setOptions] = useState({
    start_date: '',
    end_date: '',
    service_now_batch_size: 5000,
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
  // Lookups this run will load empty because the objects reference each other,
  // read before launch so the deferral is expected rather than discovered later.
  const [deferredLookups, setDeferredLookups] = useState<DeferredLookupNotice[]>([]);
  // What the launch gate found. Shown here because the gate also runs when the
  // migration starts, and a blocker discovered then is a refusal to launch with
  // no opportunity to fix it first.
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const [checkingWorkflow, setCheckingWorkflow] = useState(false);
  const [workflowReport, setWorkflowReport] = useState<WorkflowReport | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [expandedCompatObj, setExpandedCompatObj] = useState<string | null>(null);
  const [showAllCompatFields, setShowAllCompatFields] = useState(false);

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
    api.mappings
      .list()
      .then((ms) => setSavedMappingNames(new Set(ms.map((m) => m.name))))
      .catch(() => {});
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
        setValidationMsg('Connected');
        setSourceValidated(true);
      } else {
        setValidationMsg('Connection failed');
        setSourceValidated(false);
      }
    } catch {
      setValidationMsg('Could not reach the server');
      setSourceValidated(false);
    } finally {
      setValidating(false);
    }
  };

  const saveSourceConnection = async () => {
    if (!sourceConnName.trim() || !selectedSource) return;
    setSavingSourceConn(true);
    try {
      const created = await api.connections.create(
        sourceConnName.trim(),
        selectedSource.id,
        sourceCreds,
      );
      setSourceConnSaved(true);
      // Adopt the connection just saved, so the session records it and can
      // reach this source again without the credentials it will never see.
      setSelectedSourceConn(created.id);
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
      const created = await api.connections.create(destConnName.trim(), 'salesforce', destCreds);
      setDestConnSaved(true);
      setSelectedDestConn(created.id);
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
        // Reaching here now means the call succeeded and the answer really was
        // nothing, so the cause is access rather than reachability — anything
        // that stopped the request reports its own reason in the catch below.
        setTableLoadError(
          'The source answered, but listed no tables. This usually means the account cannot read the table catalogue.',
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

  const syncTableMappings = useCallback((tables: string[], sfSelection: string[], taken: Set<string>) => {
    setTableMappings((prev) => {
      const names = new Set([...taken, ...prev.map((m) => m.mappingName)]);
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
          const card = newCard(t, names, guessSfObject(t, sfSelection));
          names.add(card.mappingName);
          result.push(card);
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
      const names = new Set([...savedMappingNames, ...prev.map((m) => m.mappingName)]);
      const newCards = newTables.map((t) => {
        const card = newCard(t, names);
        names.add(card.mappingName);
        return card;
      });
      const next = [...prev];
      next.splice(requestingIdx, 0, ...newCards);
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, [selectedTables, savedMappingNames]);

  const handleSplitMapping = useCallback((idx: number) => {
    setTableMappings((prev) => {
      const source = prev[idx];
      const names = new Set([...savedMappingNames, ...prev.map((m) => m.mappingName)]);
      const split: TableMappingState = {
        ...newCard(source.sourceTable, names),
        sourceFields: source.sourceFields,
        joinTables: [...source.joinTables],
        joins: source.joins.map((j) => ({ ...j })),
        filters: source.filters.map((f) => ({ ...f })),
      };
      const next = [...prev];
      next.splice(idx + 1, 0, split);
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
  }, [savedMappingNames]);

  /** Replace a card's contents with the saved mapping of the same name. */
  const loadSavedMapping = async (cardId: string, name: string) => {
    try {
      const { config } = await api.mappings.get(name);
      const [driving, ...joined] = config.source_tables ?? [];
      setTableMappings((prev) =>
        prev.map((m) =>
          m.id !== cardId
            ? m
            : {
                ...m,
                sourceTable: driving || m.sourceTable,
                sfObject: config.destination_object,
                operation: config.operation ?? 'insert',
                fields: config.fields ?? [],
                fieldsLoaded: false,
                sourceFields: [],
                destFields: [],
                joinTables: joined,
                joins: (config.joins ?? []) as TableMappingState['joins'],
                filters: (config.filters ?? []) as TableMappingState['filters'],
                savedAs: name,
              },
        ),
      );
      setSaveError(null);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : `Could not read the saved mapping ${name}`);
    }
  };

  const renameCard = (cardId: string) => {
    setTableMappings((prev) => {
      const names = new Set([...savedMappingNames, ...prev.map((m) => m.mappingName)]);
      return prev.map((m) =>
        m.id === cardId ? { ...m, mappingName: uniqueName(m.mappingName, names) } : m,
      );
    });
    setSaveError(null);
  };

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

  /**
   * Distinct tables extraction will visit — driving tables and join tables
   * together, deduplicated the same way the extraction controller does it.
   * The row limit is applied to each of these independently, so this is the
   * multiplier that turns the number in the box into a total.
   */
  const sourceTableCount = useMemo(() => {
    const tables = new Set<string>();
    tableMappings.forEach((m) => {
      if (m.sourceTable) tables.add(m.sourceTable);
      m.joinTables.forEach((t) => t && tables.add(t));
    });
    return Math.max(tables.size, 1);
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

  /** Cards whose name belongs to a saved mapping they were not loaded from. */
  const nameConflicts = useMemo(
    () =>
      tableMappings.filter(
        (m) => m.sfObject && m.mappingName && savedMappingNames.has(m.mappingName) && m.savedAs !== m.mappingName,
      ),
    [tableMappings, savedMappingNames],
  );

  const saveMappingsToDb = async (): Promise<boolean> => {
    if (nameConflicts.length > 0) return false;
    setSavingMappings(true);
    setSaveError(null);
    const written = new Map<string, string>();
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
        if (m.savedAs === m.mappingName) {
          await api.mappings.update(m.mappingName, config, sourceType);
        } else {
          await api.mappings.create(m.mappingName, config, sourceType);
        }
        written.set(m.id, m.mappingName);
      }
      return true;
    } catch (err) {
      setSaveError(err instanceof Error ? `Could not save the mappings: ${err.message}` : 'Could not save the mappings');
      return false;
    } finally {
      if (written.size) {
        setTableMappings((prev) =>
          prev.map((m) => (written.has(m.id) ? { ...m, savedAs: written.get(m.id) } : m)),
        );
        setSavedMappingNames((prev) => new Set([...prev, ...written.values()]));
      }
      setSavingMappings(false);
    }
  };

  const validateDestination = async () => {
    setValidatingDest(true);
    setDestValidationMsg(null);
    try {
      const res = await api.destinations.validateSalesforceCredentials(destCreds);
      if (res.valid) {
        setDestValidationMsg('Connected');
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

  // A saved connection has worked before; typed credentials have to be tested
  // first, or step 3 opens on an empty table list with no reason given.
  const sourceReady =
    !!selectedSource && (sourceValidated || (!!selectedSourceConn && selectedSourceConn !== 'new'));

  const launchBlockers = [
    ...(compatReport?.blocking_required_fields ?? []).map((b) => ({
      where: `${b.mapping}.${b.field}`,
      detail: b.reason,
    })),
    ...(preflight?.blockers ?? []).map((f) => ({
      where: `${f.mapping}${f.field ? `.${f.field}` : ''}`,
      detail: f.detail,
    })),
  ];

  const warningCount =
    (compatReport?.dependency_warnings ?? []).length +
    (compatReport?.dependency_missing_parents ?? []).length +
    (compatReport?.picklist_mapping_warnings ?? []).length +
    deferredLookups.length +
    (preflight?.warnings ?? []).length +
    (compatReport?.objects ?? []).reduce((n, o) => n + o.type_mismatches, 0);

  /**
   * Pull mappings a readiness suggestion changed back into the wizard.
   *
   * Step 4's cards are the wizard's copy of each mapping, and every entry into
   * step 5 saves them again — so a fix applied to the saved config would be
   * overwritten the moment the person went back and forward. A mapping the
   * suggestion added to the run becomes a card, with its table selected so
   * step 4 does not drop it again.
   */
  const refreshMappingsFromDb = async (touched: string[], included: string[]) => {
    const names = [...new Set([...touched, ...included])];
    const loaded = await Promise.all(
      names.map(async (name) => {
        try {
          return { name, config: (await api.mappings.get(name)).config };
        } catch {
          return null;
        }
      }),
    );
    const configs = new Map(loaded.filter((l) => l !== null).map((l) => [l.name, l.config]));
    const existing = new Set(tableMappings.map((m) => m.mappingName));
    const added: TableMappingState[] = [];
    for (const name of included) {
      const config = configs.get(name);
      const [driving, ...joined] = config?.source_tables ?? [];
      if (!config || !driving || existing.has(name)) continue;
      added.push({
        id: crypto.randomUUID(),
        sourceTable: driving,
        sfObject: config.destination_object,
        mappingName: name,
        operation: config.operation ?? 'insert',
        priority: 0,
        fields: config.fields ?? [],
        sourceFields: [],
        destFields: [],
        fieldsLoaded: false,
        joinTables: joined,
        joins: (config.joins ?? []) as TableMappingState['joins'],
        filters: (config.filters ?? []) as TableMappingState['filters'],
        savedAs: name,
      });
    }
    setTableMappings((prev) => {
      // Parents load first, so an added mapping goes ahead of what needs it.
      const next = [
        ...added,
        ...prev.map((m) => {
          const config = configs.get(m.mappingName);
          return config ? { ...m, fields: config.fields ?? m.fields, operation: config.operation ?? m.operation } : m;
        }),
      ];
      return next.map((m, i) => ({ ...m, priority: i + 1 }));
    });
    if (added.length) {
      setSelectedTables((prev) => [...new Set([...added.map((m) => m.sourceTable), ...prev])]);
      setSelectedSfObjects((prev) => [...new Set([...prev, ...added.map((m) => m.sfObject)])]);
    }
  };

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

    // Separate from the compatibility check and deliberately not fatal: not
    // knowing what will be deferred is no reason to stop someone launching.
    try {
      const gate = await api.agents.preflight({
        source_type: selectedSource.id,
        selected_objects: mappingNames,
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
      });
      setDeferredLookups(gate.deferred ?? []);
      setPreflight(gate);
    } catch {
      setDeferredLookups([]);
      setPreflight(null);
    }
  };

  const runWorkflowCheck = async () => {
    setCheckingWorkflow(true);
    setWorkflowError(null);
    setWorkflowReport(null);
    try {
      const report = await api.workflow.check(mappingNames, destCreds);
      setWorkflowReport(report);
    } catch (e: unknown) {
      setWorkflowError(e instanceof Error ? e.message : 'Workflow check failed');
    } finally {
      setCheckingWorkflow(false);
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
    if (s === 3) {
      if (sourceTables.length === 0) loadSourceTables();
      // Both sides are chosen on this step now, so the object list has to be
      // there before step 4 rather than on entry to it.
      if (sfObjects.length === 0) loadSfObjects();
    }
    if (s === 4) {
      syncTableMappings(selectedTables, selectedSfObjects, savedMappingNames);
      if (sfObjects.length === 0) loadSfObjects();
    }
    if (s === 5) {
      if (!(await saveMappingsToDb())) return;
      setCompatReport(null);
      setCompatError(null);
      setDeferredLookups([]);
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
        // Only a real id: 'new' is the marker for the inline credential form.
        source_connection_id: selectedSourceConn === 'new' ? '' : selectedSourceConn,
        destination_connection_id: selectedDestConn === 'new' ? '' : selectedDestConn,
        selected_objects: mappingNames,
        options,
      });
      navigate(`/migrations/${res.session_id}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={pageStyle}>
      {/* Stepper */}
      <div style={stepperStyle}>
        {(['Select Source', 'Destination', 'Select Objects', 'Map Fields', 'Compatibility', 'Review & Run'] as const).map(
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
            <h2 style={h2}>Where is the data coming from?</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10, marginBottom: 24 }}>
              {sources.map((s) => (
                <label
                  key={s.id}
                  style={{
                    ...sourceCard,
                    border: selectedSource?.id === s.id ? '1px solid #171717' : '1px solid #e5e5e5',
                    boxShadow: selectedSource?.id === s.id ? '0 0 0 1px #171717' : 'none',
                    background: selectedSource?.id === s.id ? '#fafafa' : '#fff',
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
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{s.name}</div>
                    <div style={{ fontSize: 12, color: '#737373', marginTop: 2, lineHeight: 1.45 }}>{s.description}</div>
                  </div>
                </label>
              ))}
            </div>

            {selectedSource && (
              <div style={{ marginBottom: 8 }}>
                <h3 style={h3}>{selectedSource.name} connection</h3>
                <ConnectionPicker
                  schema={selectedSource.credentials_schema}
                  savedConnections={savedSourceConns}
                  selected={selectedSourceConn}
                  onSelect={onSelectSourceConn}
                  credentials={sourceCreds}
                  onCredentialsChange={(v) => { setSourceCreds(v); setSourceValidated(false); setValidationMsg(null); setSourceConnSaved(false); }}
                  testing={validating}
                  testResult={validationMsg ? { ok: sourceValidated && validationMsg === 'Connected', message: validationMsg } : null}
                  onTest={validateSource}
                  saveName={sourceConnName}
                  onSaveNameChange={setSourceConnName}
                  saving={savingSourceConn}
                  savedAsName={sourceConnSaved ? sourceConnName : null}
                  onSave={saveSourceConnection}
                  namePlaceholder="Name, e.g. Dev ServiceNow"
                />
              </div>
            )}

            <div style={navRow}>
              <span />
              <button
                disabled={!sourceReady}
                onClick={() => goStep(2)}
                style={{ ...primaryBtn, opacity: sourceReady ? 1 : 0.5 }}
                title={sourceReady ? undefined : 'Choose a saved connection or test the new one first'}
              >
                Next →
              </button>
            </div>
          </div>
        )}

        {/* Step 2 — Destination */}
        {step === 2 && (
          <div>
            <h2 style={h2}>Where is it going?</h2>
            <p style={stepIntro}>Salesforce</p>

            <ConnectionPicker
              schema={SALESFORCE_SCHEMA as CredentialsSchema}
              savedConnections={savedDestConns}
              selected={selectedDestConn}
              onSelect={onSelectDestConn}
              credentials={destCreds}
              onCredentialsChange={(nextCreds) => {
                setDestCreds(nextCreds);
                setDestValidated(false);
                setDestValidationMsg(null);
                setDiagnostics(null);
                setExpandDetails(false);
                setDestConnSaved(false);
              }}
              testing={validatingDest}
              testResult={destValidationMsg ? { ok: destValidated && destValidationMsg === 'Connected', message: destValidationMsg } : null}
              onTest={validateDestination}
              saveName={destConnName}
              onSaveNameChange={setDestConnName}
              saving={savingDestConn}
              savedAsName={destConnSaved ? destConnName : null}
              onSave={saveDestConnection}
              namePlaceholder="Name, e.g. Prod Salesforce"
            />

            {destValidated && (
              <Disclosure
                label="Check object access"
                hint="optional: confirm the integration user can see the objects you need"
                style={{ marginTop: 18 }}
              >
                <button
                  onClick={checkObjectVisibility}
                  disabled={checkingDiagnostics}
                  style={smallBtn}
                >
                  {checkingDiagnostics ? 'Checking…' : 'Run check'}
                </button>
              </Disclosure>
            )}

            {/* Diagnostics Results */}
            {diagnostics && (
              <div
                style={{
                  border: diagnostics.missing_objects && diagnostics.missing_objects.length > 0
                    ? '1px solid #a3a3a3'
                    : '1px solid #d4d4d4',
                  background: diagnostics.missing_objects && diagnostics.missing_objects.length > 0
                    ? '#fafafa'
                    : '#fafafa',
                  borderRadius: 8,
                  padding: 14,
                  marginBottom: 16,
                }}
              >
                <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>
                  {diagnostics.missing_objects && diagnostics.missing_objects.length > 0
                    ? 'Some objects may not be accessible'
                    : 'All objects are accessible'}
                </div>
                <div style={{ fontSize: 12, color: '#404040', marginBottom: 6 }}>
                  <strong>Run-as User:</strong> {diagnostics.run_as_user_id || 'N/A'}
                </div>
                {diagnostics.missing_objects && diagnostics.missing_objects.length > 0 && (
                  <div style={{ fontSize: 12, color: '#404040', marginBottom: 6 }}>
                    <strong>Missing Objects:</strong> {diagnostics.missing_objects.join(', ')}
                  </div>
                )}
                {diagnostics.required_objects && diagnostics.required_objects.length > 0 && (
                  <button
                    onClick={() => setExpandDetails(!expandDetails)}
                    style={{
                      background: 'none',
                      border: 'none',
                      color: '#171717',
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
                          color: diagnostics.visibility?.[obj] === false ? '#404040' : '#404040',
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
                  <span style={{ fontSize: 12, color: '#737373' }}>
                    Test the connection to continue.
                  </span>
                )}
                {diagnostics?.missing_objects && diagnostics.missing_objects.length > 0 && (
                  <span style={{ fontSize: 12, color: '#0a0a0a' }}>
                    Required objects not accessible. Grant permissions and re-check.
                  </span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Step 3 — Select what to migrate, on both sides */}
        {step === 3 && (
          <div>
            <h2 style={h2}>What should move?</h2>
            <p style={stepIntro}>
              Pick the {selectedSource?.name ?? 'source'} tables to read and the Salesforce objects they go into.
            </p>

            <div style={selectionPanel}>
              <div style={selectionHeader}>
                <span style={{ fontWeight: 700, fontSize: 14, color: '#171717' }}>
                  Source — {selectedSource?.name ?? 'source'} tables
                </span>
                <span style={{ fontSize: 12, color: '#737373' }}>
                  {!loadingTables && `${selectedTables.length} of ${sourceTables.length} selected`}
                </span>
              </div>
              {tableLoadError && (
                <div style={{
                  padding: 12, marginBottom: 12, borderRadius: 8,
                  background: '#fafafa', border: '1px solid #d4d4d4', fontSize: 13, color: '#171717',
                }}>
                  {tableLoadError}
                  <button
                    onClick={loadSourceTables}
                    disabled={loadingTables}
                    style={{ marginLeft: 12, padding: '4px 12px', borderRadius: 6, border: '1px solid #d4d4d4', background: '#fff', cursor: 'pointer', fontSize: 12 }}
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
            </div>

            <div style={{ ...selectionPanel, marginTop: 16 }}>
              <div style={selectionHeader}>
                <span style={{ fontWeight: 700, fontSize: 14, color: '#171717' }}>
                  Destination — Salesforce objects
                </span>
                <span style={{ fontSize: 12, color: '#737373' }}>
                  {selectedSfObjects.length} selected
                </span>
              </div>
              <DestinationObjectSelector
                objects={sfObjects}
                selected={selectedSfObjects}
                onChange={setSelectedSfObjects}
                loading={loadingSfObjects}
              />
            </div>

            <DestinationHierarchy
              selectedObjects={selectedSfObjects}
              destCreds={destCreds}
              sourceType={selectedSource?.id}
              defaultCollapsed
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
            <h2 style={h2}>Map the fields</h2>
            <p style={stepIntro}>
              For each table, pick its Salesforce object, then load and match the fields.
              {loadingSfObjects && ' Loading Salesforce objects…'}
            </p>

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
              <p style={{ color: '#a3a3a3', textAlign: 'center', padding: '32px 0' }}>
                No tables selected. Go back to Step 3 and select tables.
              </p>
            )}

            {duplicateMappingNames.length > 0 && (
              <div style={noticeBox}>
                <strong>Two cards share a mapping name:</strong> {duplicateMappingNames.join(', ')}.
                Rename one of them to continue.
              </div>
            )}

            {nameConflicts.map((m) => (
              <div key={m.id} style={{ ...noticeBox, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <span style={{ flex: 1, minWidth: 260 }}>
                  A saved mapping is already called <strong>{m.mappingName}</strong>. Continuing would
                  replace it, so choose whether to edit that mapping or save this card under a new name.
                </span>
                <button type="button" style={smallBtn} onClick={() => loadSavedMapping(m.id, m.mappingName)}>
                  Edit the saved one
                </button>
                <button type="button" style={smallBtn} onClick={() => renameCard(m.id)}>
                  Use a new name
                </button>
              </div>
            ))}

            {saveError && <div style={noticeBox}>{saveError}</div>}

            <div style={navRow}>
              <button onClick={() => goStep(3)} style={secondaryBtn}>← Back</button>
              <button
                disabled={
                  mappingNames.length === 0 || savingMappings || duplicateMappingNames.length > 0 || nameConflicts.length > 0
                }
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
            <h2 style={h2}>Check before you run</h2>
            <p style={stepIntro}>
              Nothing is moved here. The checks read both systems and report what would go wrong.
            </p>

            <div style={{ marginBottom: 16, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                onClick={runCompatibilityCheck}
                disabled={checkingCompat}
                style={primaryBtn}
              >
                {checkingCompat ? 'Checking…' : compatReport ? 'Re-run compatibility check' : 'Run compatibility check'}
              </button>
              <button
                onClick={runWorkflowCheck}
                disabled={checkingWorkflow || mappingNames.length === 0}
                style={secondaryBtn}
              >
                {checkingWorkflow ? 'Checking workflow…' : workflowReport ? 'Re-check Salesforce workflow' : 'Check Salesforce workflow'}
              </button>
            </div>
            <p style={{ ...stepIntro, marginTop: -8 }}>
              The workflow check walks Object, Trigger, Entry Conditions, Decisions, Actions, Approvals, Notifications, and Error Handling. It uses the template in data/workflow_templates, then checks transformed rows again before load.
            </p>

            {workflowError && (
              <div style={{ color: '#0a0a0a', fontSize: 13, marginBottom: 12 }}>
                {workflowError}
              </div>
            )}
            {workflowReport && <WorkflowCheckPanel report={workflowReport} />}

            {compatError && (
              <div style={{ color: '#0a0a0a', fontSize: 13, marginBottom: 12 }}>
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
                  <div style={{ fontSize: 13, color: '#404040', marginTop: 4 }}>{compatReport.summary}</div>
                </div>

                {/* Per-object breakdown */}
                {compatReport.objects.map((obj) => (
                  <div key={obj.object_name} style={{
                    border: '1px solid #e5e5e5', borderRadius: 8, marginBottom: 10, overflow: 'hidden',
                  }}>
                    <div
                      onClick={() => setExpandedCompatObj(expandedCompatObj === obj.object_name ? null : obj.object_name)}
                      style={{
                        padding: '10px 14px', background: '#fafafa', cursor: 'pointer',
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      }}
                    >
                      <div>
                        <strong>{obj.source_label}</strong>
                        <span style={{ fontSize: 12, color: '#737373', marginLeft: 8 }}>&rarr; {obj.salesforce_object}</span>
                      </div>
                      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                        <span style={{
                          fontSize: 13, fontWeight: 600,
                          color: compatColor(obj.compatibility_pct),
                        }}>
                          {obj.compatibility_pct}%
                        </span>
                        <span style={{ fontSize: 12, color: '#737373' }}>
                          {obj.mapped_fields}/{obj.total_fields} fields mapped
                        </span>
                        {obj.type_mismatches > 0 && (
                          <span style={{ fontSize: 11, color: '#404040', background: '#f5f5f5', padding: '1px 6px', borderRadius: 4 }}>
                            {obj.type_mismatches} type mismatch{obj.type_mismatches > 1 ? 'es' : ''}
                          </span>
                        )}
                        <span>{expandedCompatObj === obj.object_name ? '\u25BC' : '\u25B6'}</span>
                      </div>
                    </div>
                    {expandedCompatObj === obj.object_name && (
                      <div style={{ padding: 14 }}>
                        {obj.blockers.length > 0 && (
                          <div style={{ marginBottom: 10, padding: 10, background: '#fafafa', borderRadius: 6, border: '1px solid #d4d4d4' }}>
                            {obj.blockers.map((b, i) => (
                              <div key={i} style={{ fontSize: 12, color: '#0a0a0a' }}>{b}</div>
                            ))}
                          </div>
                        )}

                        {/* Bulk create all missing fields button */}
                        {obj.fields.some((f) => f.status === 'no_mapping' && f.suggested_custom_field) && (
                          <div style={{ marginBottom: 12, padding: 12, background: '#f5f5f5', border: '1px solid #d4d4d4', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <div>
                              <div style={{ fontSize: 13, fontWeight: 600, color: '#262626' }}>
                                {obj.fields.filter((f) => f.status === 'no_mapping' && f.suggested_custom_field).length} missing field(s) detected
                              </div>
                              <div style={{ fontSize: 11, color: '#737373', marginTop: 2 }}>
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
                                padding: '8px 16px', fontSize: 12, background: '#171717', color: '#fff',
                                border: 'none', borderRadius: 6, cursor: 'pointer', fontWeight: 600, whiteSpace: 'nowrap',
                              }}
                            >
                              {bulkCreating ? 'Creating...' : 'Create All Missing Fields'}
                            </button>
                          </div>
                        )}

                        {/* Bulk creation results */}
                        {bulkResults.length > 0 && (
                          <div style={{ marginBottom: 12, padding: 10, background: '#fafafa', border: '1px solid #e5e5e5', borderRadius: 6, maxHeight: 150, overflowY: 'auto' }}>
                            <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 6 }}>Creation Results:</div>
                            {bulkResults.map((r, i) => (
                              <div key={i} style={{ fontSize: 11, color: r.success ? '#171717' : '#0a0a0a', marginBottom: 2 }}>
                                {r.field}: {r.message}
                              </div>
                            ))}
                          </div>
                        )}
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                          <span style={{ fontSize: 12, color: '#737373' }}>
                            {showAllCompatFields
                              ? `All ${obj.fields.length} fields`
                              : `${obj.fields.filter((f) => f.status !== 'mapped').length} field(s) need attention`}
                          </span>
                          <button type="button" onClick={() => setShowAllCompatFields((v) => !v)} style={linkBtn}>
                            {showAllCompatFields ? 'Show only problems' : `Show all ${obj.fields.length} fields`}
                          </button>
                        </div>
                        <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
                          <thead>
                            <tr style={{ background: '#f5f5f5' }}>
                              <th style={thStyle}>Source Field</th>
                              <th style={thStyle}>Salesforce Field</th>
                              <th style={thStyle}>Status</th>
                              <th style={thStyle}>Suggestion</th>
                              <th style={thStyle}>Action</th>
                            </tr>
                          </thead>
                          <tbody>
                            {obj.fields.filter((f) => showAllCompatFields || f.status !== 'mapped').map((f, i) => {
                              const fieldKey = `${obj.salesforce_object}::${f.destination_field}`;
                              return (
                              <tr key={i} style={{ borderBottom: '1px solid #e5e5e5' }}>
                                <td style={tdStyle}>
                                  {f.source_field || '\u2014'}
                                  {f.source_type && f.status === 'type_mismatch' && (
                                    <span style={{ fontSize: 10, color: '#737373', marginLeft: 4 }}>({f.source_type})</span>
                                  )}
                                </td>
                                <td style={tdStyle}>
                                  {f.destination_field}
                                  {f.destination_type && f.status === 'type_mismatch' && (
                                    <span style={{ fontSize: 10, color: '#737373', marginLeft: 4 }}>({f.destination_type})</span>
                                  )}
                                </td>
                                <td style={tdStyle}>
                                  <span style={{
                                    padding: '2px 6px', borderRadius: 4, fontSize: 11,
                                    background: f.status === 'mapped' ? '#f5f5f5' : f.status === 'type_mismatch' ? '#f5f5f5' : '#f5f5f5',
                                    color: f.status === 'mapped' ? '#171717' : f.status === 'type_mismatch' ? '#404040' : '#171717',
                                  }}>
                                    {f.status === 'type_mismatch' ? 'type mismatch' : f.status}
                                  </span>
                                </td>
                                <td style={{ ...tdStyle, color: '#737373' }}>{f.suggestion || '\u2014'}</td>
                                <td style={tdStyle}>
                                  {f.status === 'no_mapping' && f.suggested_custom_field && (
                                    <button
                                      onClick={() => openCreateFieldDialog(obj.salesforce_object, f)}
                                      style={{
                                        padding: '3px 8px', fontSize: 11, background: '#171717', color: '#fff',
                                        border: 'none', borderRadius: 4, cursor: 'pointer', whiteSpace: 'nowrap',
                                      }}
                                    >
                                      + Create Field
                                    </button>
                                  )}
                                  {customFieldMsg && customFieldMsg.key === fieldKey && (
                                    <div style={{ fontSize: 11, marginTop: 4, color: customFieldMsg.success ? '#171717' : '#0a0a0a' }}>
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
                            marginTop: 12, padding: 16, background: '#fafafa', border: '1px solid #d4d4d4',
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
                                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #d4d4d4', borderRadius: 4, fontSize: 12, boxSizing: 'border-box' }}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: 11, fontWeight: 500, display: 'block', marginBottom: 2 }}>API Name</label>
                                <input
                                  type="text"
                                  value={customFieldName}
                                  onChange={(e) => setCustomFieldName(e.target.value)}
                                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #d4d4d4', borderRadius: 4, fontSize: 12, boxSizing: 'border-box' }}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: 11, fontWeight: 500, display: 'block', marginBottom: 2 }}>Field Type</label>
                                <select
                                  value={customFieldType}
                                  onChange={(e) => setCustomFieldType(e.target.value)}
                                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #d4d4d4', borderRadius: 4, fontSize: 12, boxSizing: 'border-box' }}
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
                                  padding: '6px 14px', fontSize: 12, background: '#171717', color: '#fff',
                                  border: 'none', borderRadius: 4, cursor: 'pointer', fontWeight: 600,
                                }}
                              >
                                Create Field in Salesforce
                              </button>
                              <button
                                onClick={() => { setCreatingField(null); setCustomFieldMsg(null); }}
                                style={{
                                  padding: '6px 14px', fontSize: 12, background: '#fff', color: '#404040',
                                  border: '1px solid #d4d4d4', borderRadius: 4, cursor: 'pointer',
                                }}
                              >
                                Cancel
                              </button>
                            </div>
                            {customFieldMsg && customFieldMsg.key === creatingField && (
                              <div style={{ marginTop: 8, fontSize: 12, color: customFieldMsg.success ? '#171717' : '#0a0a0a' }}>
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
                  <Disclosure
                    label="Load order"
                    count={compatReport.dependency_order.length}
                    hint={compatReport.dependency_order.join(' \u2192 ')}
                    variant="boxed"
                    style={{ marginTop: 16 }}
                  >
                    <p style={{ fontSize: 12, color: '#737373', marginBottom: 10 }}>
                      Objects will be migrated in this order based on Salesforce reference field dependencies.
                      {compatReport.dependency_has_cycle && (
                        <span style={{ color: '#0a0a0a', fontWeight: 600 }}>
                          {' '}Circular dependency detected — using card order instead.
                        </span>
                      )}
                    </p>

                    {/* Missing parent warnings */}
                    {compatReport.dependency_missing_parents && compatReport.dependency_missing_parents.length > 0 && (
                      <div style={{
                        padding: 12, marginBottom: 12, borderRadius: 8,
                        background: '#fafafa', border: '1px solid #d4d4d4',
                      }}>
                        <div style={{ fontSize: 12, fontWeight: 600, color: '#404040', marginBottom: 6 }}>
                          Missing Parent Object Warnings
                        </div>
                        {compatReport.dependency_missing_parents.map((mp, i) => (
                          <div key={i} style={{ fontSize: 12, color: '#404040', marginBottom: 3 }}>
                            <strong>{mp.mapping}</strong> field <code style={{ background: '#f5f5f5', padding: '1px 4px', borderRadius: 3 }}>{mp.field}</code> references <strong>{mp.references}</strong> — {mp.reason}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Self-reference / cycle warnings */}
                    {compatReport.dependency_warnings && compatReport.dependency_warnings.length > 0 && (
                      <div style={{
                        padding: 12, marginBottom: 12, borderRadius: 8,
                        background: '#fafafa', border: '1px solid #d4d4d4',
                      }}>
                        {compatReport.dependency_warnings.map((w, i) => (
                          <div key={i} style={{ fontSize: 12, color: '#171717', marginBottom: 3 }}>
                            {w.mapping ? <><strong>{w.mapping}</strong> — </> : null}{w.reason}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Ordered list with dependency arrows */}
                    <div style={{
                      border: '1px solid #e5e5e5', borderRadius: 8, overflow: 'hidden',
                    }}>
                      {compatReport.dependency_order.map((name, i) => {
                        const edges = (compatReport.dependency_edges ?? []).filter((e) => e.from_mapping === name);
                        const objInfo = compatReport.objects.find((o) => o.object_name === name);
                        return (
                          <div key={name} style={{
                            padding: '10px 14px',
                            borderBottom: i < compatReport.dependency_order!.length - 1 ? '1px solid #e5e5e5' : 'none',
                            background: i % 2 === 0 ? '#fafafa' : '#fff',
                            display: 'flex', alignItems: 'center', gap: 12,
                          }}>
                            <div style={{
                              width: 28, height: 28, borderRadius: '50%',
                              background: '#171717', color: '#fff',
                              display: 'flex', alignItems: 'center', justifyContent: 'center',
                              fontSize: 12, fontWeight: 700, flexShrink: 0,
                            }}>
                              {i + 1}
                            </div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontWeight: 600, fontSize: 13 }}>
                                {name}
                                {objInfo && (
                                  <span style={{ fontWeight: 400, color: '#737373', marginLeft: 8, fontSize: 12 }}>
                                    → {objInfo.salesforce_object}
                                  </span>
                                )}
                              </div>
                              {edges.length > 0 && (
                                <div style={{ fontSize: 11, color: '#737373', marginTop: 2 }}>
                                  depends on:{' '}
                                  {edges.map((e, ei) => (
                                    <span key={ei}>
                                      {ei > 0 && ', '}
                                      <span style={{
                                        padding: '1px 5px', borderRadius: 3, fontSize: 10,
                                        background: e.required ? '#f5f5f5' : '#fafafa',
                                        color: e.required ? '#171717' : '#262626',
                                        border: e.required ? '1px solid #d4d4d4' : '1px dashed #d4d4d4',
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
                              <div style={{ fontSize: 16, color: '#d4d4d4', flexShrink: 0 }}>↓</div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </Disclosure>
                )}
              </div>
            )}

            {selectedSource && (
              <DataReadinessPanel
                sourceType={selectedSource.id}
                mappingNames={mappingNames}
                sourceCreds={sourceCreds}
                destCreds={destCreds}
                onMappingsChanged={refreshMappingsFromDb}
              />
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
            <h2 style={h2}>Review &amp; run</h2>
            <p style={stepIntro}>
              {selectedSource?.name} &rarr; Salesforce{destCreds.base_url ? ` (${destCreds.base_url.replace(/^https?:\/\//, '')})` : ''}
              {' · '}{mappingNames.length} mapping{mappingNames.length === 1 ? '' : 's'}
              {compatReport && <> · {compatReport.overall_pct}% compatible</>}
            </p>

            {launchBlockers.length > 0 && (
              <div style={{ ...noticeBox, border: '1px solid #a3a3a3', marginBottom: 18 }}>
                <div style={{ fontWeight: 700, marginBottom: 6 }}>
                  {launchBlockers.length} issue{launchBlockers.length === 1 ? '' : 's'} must be fixed before launching
                </div>
                {launchBlockers.map((b, i) => (
                  <div key={`lb-${i}`} style={{ fontSize: 12.5, marginBottom: 3 }}>
                    <b>{b.where}</b> — {b.detail}
                  </div>
                ))}
                <div style={{ fontSize: 12, color: '#525252', marginTop: 6 }}>
                  Fix these in step 4 (set a source field, constant or lookup), then re-run the check in step 5.
                </div>
              </div>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(220px, 320px) 1fr', gap: 16, alignItems: 'start', marginBottom: 12 }}>
              <div>
                <label style={labelStyle}>Rows per source table</label>
                <input
                  type="number"
                  min={1}
                  value={options.service_now_batch_size}
                  onChange={(e) =>
                    setOptions((o) => ({ ...o, service_now_batch_size: +e.target.value }))
                  }
                  style={inputStyle}
                />
              </div>
              <p style={{ fontSize: 12, color: '#737373', margin: '26px 0 0', lineHeight: 1.5 }}>
                Up to{' '}
                <strong>{((options.service_now_batch_size || 0) * sourceTableCount).toLocaleString()} rows</strong>{' '}
                across {sourceTableCount === 1 ? '1 table' : `${sourceTableCount} tables`}. A later run picks up
                after the rows already migrated.
              </p>
            </div>

            <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 14, fontSize: 14 }}>
              <input
                type="checkbox"
                checked={options.disable_load}
                onChange={(e) => setOptions((o) => ({ ...o, disable_load: e.target.checked }))}
              />
              Dry run <span style={{ color: '#737373', fontSize: 13 }}>— read and transform only, write nothing to Salesforce</span>
            </label>

            <Disclosure
              label="More options"
              hint={options.start_date || options.end_date
                ? `Date range ${options.start_date || '…'} to ${options.end_date || '…'}`
                : 'Date range'}
              defaultOpen={Boolean(options.start_date || options.end_date)}
              style={{ marginBottom: 20 }}
            >
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, maxWidth: 520 }}>
                <div>
                  <label style={labelStyle}>Created on or after</label>
                  <input
                    type="date"
                    value={options.start_date}
                    onChange={(e) => setOptions((o) => ({ ...o, start_date: e.target.value }))}
                    style={inputStyle}
                  />
                </div>
                <div>
                  <label style={labelStyle}>Created on or before</label>
                  <input
                    type="date"
                    value={options.end_date}
                    onChange={(e) => setOptions((o) => ({ ...o, end_date: e.target.value }))}
                    style={inputStyle}
                  />
                </div>
              </div>
            </Disclosure>

            {/* Summary */}
            <Disclosure
              label="What will run"
              hint="load order and fields per object"
              variant="boxed"
              style={{ marginBottom: 10 }}
            >
              <div style={{ fontSize: 13, display: 'flex', flexDirection: 'column', gap: 6 }}>
                {compatReport?.dependency_order && compatReport.dependency_order.length > 0 ? (
                  <div style={{ marginLeft: 16 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: '#404040', marginBottom: 4 }}>Execution Order:</div>
                    {compatReport.dependency_order.map((name, i) => {
                      const objInfo = compatReport.objects.find((o) => o.object_name === name);
                      return (
                        <div key={name} style={{ fontSize: 12, color: '#737373', display: 'flex', gap: 6, alignItems: 'center', marginBottom: 2 }}>
                          <span style={{
                            width: 18, height: 18, borderRadius: '50%', background: '#171717', color: '#fff',
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
                  <div style={{ fontSize: 12, color: '#737373', marginLeft: 16 }}>
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
                        <div key={obj.object_name} style={{ fontSize: 12, color: '#737373', marginBottom: 2, display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span>{obj.object_name} → {obj.salesforce_object}:</span>
                          <span style={{ color: compatColor(obj.compatibility_pct), fontWeight: 600 }}>
                            {obj.mapped_fields}/{obj.total_fields} fields
                          </span>
                          {obj.type_mismatches > 0 && (
                            <span style={{
                              fontSize: 10, padding: '1px 5px', borderRadius: 4,
                              background: '#f5f5f5', color: '#404040',
                            }}>
                              {obj.type_mismatches} type mismatch{obj.type_mismatches > 1 ? 'es' : ''}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

              </div>
            </Disclosure>

            {warningCount > 0 && (
            <Disclosure
              label="Warnings"
              count={warningCount}
              hint="these will not stop the run"
              variant="boxed"
              style={{ marginBottom: 10 }}
            >
            {/* Dependency warnings banner */}
            {compatReport && (
              (compatReport.dependency_warnings ?? []).length > 0 ||
              (compatReport.dependency_missing_parents ?? []).length > 0
            ) && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#fafafa', border: '1px solid #d4d4d4',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#404040', marginBottom: 8 }}>
                  Dependency Warnings
                </div>
                {(compatReport.dependency_warnings ?? []).map((w, i) => (
                  <div key={`w-${i}`} style={{ fontSize: 12, color: '#404040', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>⚠</span>
                    <span>{w.mapping ? `${w.mapping} — ` : ''}{w.reason}</span>
                  </div>
                ))}
                {(compatReport.dependency_missing_parents ?? []).map((mp, i) => (
                  <div key={`mp-${i}`} style={{ fontSize: 12, color: '#404040', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>⚠</span>
                    <span>{mp.mapping} — {mp.reason}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Picklist value-mapping warnings banner (non-blocking) */}
            {compatReport && (compatReport.picklist_mapping_warnings ?? []).length > 0 && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#f5f5f5', border: '1px solid #e5e5e5',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#404040', marginBottom: 8 }}>
                  Picklist values not fully mapped
                </div>
                {(compatReport.picklist_mapping_warnings ?? []).map((p, i) => (
                  <div key={`pk-${i}`} style={{ fontSize: 12, color: '#404040', marginBottom: 4, display: 'flex', gap: 6 }}>
                    <span>⚠</span>
                    <span><b>{p.mapping}.{p.field}</b> — {p.reason}</span>
                  </div>
                ))}
                <div style={{ fontSize: 11, color: '#404040', marginTop: 8 }}>
                  Unmapped values load as blank. This does not block the migration.
                </div>
              </div>
            )}

            {/* References this run cannot fill while creating the records */}
            {deferredLookups.length > 0 && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#f5f5f5', border: '1px solid #d4d4d4',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#262626', marginBottom: 8 }}>
                  {deferredLookups.length} reference{deferredLookups.length === 1 ? '' : 's'} will be
                  attached after the load, not during it
                </div>
                <div style={{ fontSize: 12, color: '#262626', marginBottom: 8, lineHeight: 1.55 }}>
                  These lookups have nothing to point at while the records are being created —
                  either the objects reference each other, so whichever goes first has no
                  counterpart yet, or the object being pointed at has not been migrated at all.
                  Those records will load with the field empty and the missing link recorded.
                  You will be told how many are affected when this run finishes, and again when
                  a later run migrates what they are waiting for. Filling them is always
                  offered, never automatic.
                </div>
                {deferredLookups.map((d, i) => (
                  <div key={`df-${i}`} style={{ fontSize: 12, color: '#262626', marginBottom: 3 }}>
                    <b>{d.destination_object}.{d.field}</b>
                    <span style={{ color: '#737373' }}>
                      {' '}— {d.reason === 'self_reference'
                        ? 'points at its own object'
                        : d.reason === 'parent_not_migrated'
                          ? d.buildable
                            ? `waiting on ${d.awaiting_object || 'another object'}${
                                d.awaiting?.length ? ` — run ${d.awaiting.join(', ')} to fill them` : ''
                              }`
                            : `waiting on ${d.awaiting_object || 'another object'}, which nothing migrates yet`
                          : 'circular reference'}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* What the launch gate found, before it can refuse the launch */}
            {preflight && preflight.warnings.length > 0 && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#fafafa', border: '1px solid #d4d4d4',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: '#404040' }}>
                  {`${preflight.warnings.length} thing${preflight.warnings.length === 1 ? '' : 's'} worth checking before you launch`}
                </div>
                {preflight.warnings.map((f, i) => (
                  <div
                    key={`pf-${i}`}
                    style={{ fontSize: 12, marginBottom: 6, lineHeight: 1.55, color: '#404040' }}
                  >
                    <b>
                      {f.mapping}
                      {f.field ? `.${f.field}` : ''}
                    </b>{' '}
                    — {f.detail}
                  </div>
                ))}
              </div>
            )}

            {/* Type mismatch warnings banner */}
            {compatReport && compatReport.objects.some((o) => o.type_mismatches > 0) && (
              <div style={{
                padding: 14, marginBottom: 16, borderRadius: 8,
                background: '#f5f5f5', border: '1px solid #e5e5e5',
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#404040', marginBottom: 8 }}>
                  Type Mismatch Warnings
                </div>
                {compatReport.objects
                  .filter((o) => o.type_mismatches > 0)
                  .map((obj) => (
                    <div key={obj.object_name} style={{ marginBottom: 6 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: '#404040' }}>
                        {obj.object_name} → {obj.salesforce_object} ({obj.type_mismatches} mismatch{obj.type_mismatches > 1 ? 'es' : ''})
                      </div>
                      {obj.fields
                        .filter((f) => f.status === 'type_mismatch')
                        .map((f, fi) => (
                          <div key={fi} style={{ fontSize: 11, color: '#404040', marginLeft: 16, marginTop: 2 }}>
                            {f.source_field} ({f.source_type}) → {f.destination_field} ({f.destination_type})
                            {f.suggestion && <span style={{ color: '#404040' }}> — {f.suggestion}</span>}
                          </div>
                        ))}
                    </div>
                  ))}
              </div>
            )}
            </Disclosure>
            )}

            {error && <p style={{ color: '#0a0a0a', fontSize: 13 }}>Error: {error}</p>}

            <div style={navRow}>
              <button onClick={() => goStep(5)} style={secondaryBtn}>&larr; Back</button>
              <button
                onClick={submit}
                disabled={submitting || (compatReport?.blocking_required_fields ?? []).length > 0}
                style={{ ...primaryBtn, opacity: (compatReport?.blocking_required_fields ?? []).length > 0 ? 0.5 : 1 }}
                title={
                  (compatReport?.blocking_required_fields ?? []).length > 0
                    ? 'Resolve the required-field blockers above before launching'
                    : ''
                }
              >
                {submitting ? 'Creating…' : options.disable_load ? 'Create dry run' : 'Create migration'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const defaultMappingName = (table: string) =>
  table.replace(/^(sc_|sys_)/, '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\s/g, '');

function uniqueName(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base} ${n}`)) n++;
  return `${base} ${n}`;
}

/** The Salesforce object chosen in step 3 whose name matches the table, if exactly that. */
function guessSfObject(table: string, selection: string[]): string {
  const wanted = defaultMappingName(table).toLowerCase();
  return selection.find((o) => o.toLowerCase() === wanted) ?? '';
}

function newCard(table: string, taken: Set<string>, sfObject = ''): TableMappingState {
  return {
    id: crypto.randomUUID(),
    sourceTable: table,
    sfObject,
    mappingName: uniqueName(defaultMappingName(table), taken),
    operation: 'insert',
    priority: 0,
    fields: [],
    sourceFields: [],
    destFields: [],
    fieldsLoaded: false,
    joinTables: [],
    joins: [],
    filters: [],
  };
}

// ── Styles ────────────────────────────────────────────────────────────────────
const pageStyle: React.CSSProperties = {
  padding: '32px',
  width: '100%',
  maxWidth: 1600,
  margin: '0 auto',
  transition: 'max-width 0.2s ease',
};
const h2: React.CSSProperties = { margin: '0 0 20px', fontSize: 20, fontWeight: 700 };
const h3: React.CSSProperties = { margin: '0 0 12px', fontSize: 15, fontWeight: 600 };
const cardStyle: React.CSSProperties = { background: '#fff', border: '1px solid #e5e5e5', borderRadius: 12, padding: 28 };
const navRow: React.CSSProperties = {
  display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginTop: 24,
  paddingTop: 18, borderTop: '1px solid #f0f0f0',
};
const selectionPanel: React.CSSProperties = {
  border: '1px solid #e5e5e5', borderRadius: 10, padding: 16, background: '#fff',
};
const selectionHeader: React.CSSProperties = {
  display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12,
};
const primaryBtn: React.CSSProperties = {
  padding: '10px 22px', background: '#171717', color: '#fff', border: 'none',
  borderRadius: 8, cursor: 'pointer', fontWeight: 600, fontSize: 14,
};
const secondaryBtn: React.CSSProperties = {
  padding: '10px 22px', background: '#fff', color: '#404040', border: '1px solid #d4d4d4',
  borderRadius: 8, cursor: 'pointer', fontWeight: 600, fontSize: 14,
};
const linkBtn: React.CSSProperties = {
  background: 'none', border: 'none', padding: 0, color: '#404040', textDecoration: 'underline',
  cursor: 'pointer', fontSize: 12,
};
const smallBtn: React.CSSProperties = {
  padding: '6px 12px', background: '#fff', color: '#262626', border: '1px solid #d4d4d4',
  borderRadius: 6, cursor: 'pointer', fontWeight: 600, fontSize: 12.5, whiteSpace: 'nowrap',
};
const noticeBox: React.CSSProperties = {
  padding: '10px 12px', marginBottom: 10, borderRadius: 8,
  background: '#fafafa', border: '1px solid #d4d4d4', fontSize: 13, color: '#171717', lineHeight: 1.5,
};
const stepIntro: React.CSSProperties = { color: '#737373', fontSize: 13, margin: '-12px 0 18px' };
// ── Compatibility colour helpers ───────────────────────────────────────────────
const COMPAT_HIGH = 80;  // pct threshold for "good" (green)
const COMPAT_MID  = 50;  // pct threshold for "warning" (amber)

function compatColor(pct: number): string {
  return pct >= COMPAT_HIGH ? '#171717' : pct >= COMPAT_MID ? '#404040' : '#0a0a0a';
}
function compatBg(pct: number): string {
  return pct >= COMPAT_HIGH ? '#fafafa' : pct >= COMPAT_MID ? '#fafafa' : '#fafafa';
}
function compatBorder(pct: number): string {
  return pct >= COMPAT_HIGH ? '#d4d4d4' : pct >= COMPAT_MID ? '#d4d4d4' : '#a3a3a3';
}
const thStyle: React.CSSProperties = {
  padding: '6px 10px', textAlign: 'left', fontWeight: 600,
  borderBottom: '1px solid #d4d4d4',
};
const tdStyle: React.CSSProperties = { padding: '5px 10px', verticalAlign: 'top' };
const sourceCard: React.CSSProperties = {
  display: 'flex', gap: 12, alignItems: 'flex-start', padding: '14px 16px',
  borderRadius: 10, cursor: 'pointer',
};
const inputStyle: React.CSSProperties = {
  width: '100%', padding: '8px 10px', border: '1px solid #d4d4d4',
  borderRadius: 6, fontSize: 14, boxSizing: 'border-box',
};
const labelStyle: React.CSSProperties = { fontSize: 13, fontWeight: 500, display: 'block', marginBottom: 4 };
const stepperStyle: React.CSSProperties = {
  display: 'flex', gap: 0, marginBottom: 28, background: '#fff',
  border: '1px solid #e5e5e5', borderRadius: 12, padding: '16px 24px',
  justifyContent: 'space-between',
};
const stepStyle = (active: boolean, done: boolean): React.CSSProperties => ({
  display: 'flex', alignItems: 'center', gap: 8,
  opacity: active || done ? 1 : 0.45, fontWeight: active ? 600 : 400,
});
const stepCircle = (active: boolean): React.CSSProperties => ({
  width: 24, height: 24, borderRadius: '50%', display: 'flex', alignItems: 'center',
  justifyContent: 'center', fontSize: 12, fontWeight: 700,
  background: active ? '#171717' : '#e5e5e5', color: active ? '#fff' : '#737373',
});
