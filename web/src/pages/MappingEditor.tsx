import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type {
  MappingMeta,
  MappingConfig,
  MappingFieldDef,
  SourceFieldInfo,
  DestinationFieldInfo,
  SavedConnection,
  Source,
  MappingValidationResult,
} from '../api/client';
import { FieldMapper } from '../components/FieldMapper';

// ── Mappings list ───────────────────────────────────────────────────────────────

export function MappingsList() {
  const [mappings, setMappings] = useState<MappingMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api.mappings
      .list()
      .then(setMappings)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const remove = async (name: string) => {
    if (!window.confirm(`Delete mapping "${name}"? This cannot be undone.`)) return;
    try {
      await api.mappings.remove(name);
      load();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <div style={pageStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <div>
          <h1 style={h1}>Manage Mappings</h1>
          <p style={{ color: '#6b7280', margin: 0 }}>
            Define how source records map onto Salesforce objects
          </p>
        </div>
        <Link to="/mappings/new" style={primaryBtn}>+ New Mapping</Link>
      </div>

      {loading && <p style={{ color: '#6b7280' }}>Loading…</p>}
      {error && <p style={{ color: '#dc2626' }}>Error: {error}</p>}

      {!loading && (
        <div style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 10, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
            <thead>
              <tr style={{ background: '#f8fafc', textAlign: 'left' }}>
                <th style={lth}>Name</th>
                <th style={lth}>Source</th>
                <th style={lth}>Priority</th>
                <th style={lth}>Type</th>
                <th style={lth}></th>
              </tr>
            </thead>
            <tbody>
              {mappings.map((m) => (
                <tr key={m.name} style={{ borderTop: '1px solid #f1f5f9' }}>
                  <td style={ltd}><strong>{m.name}</strong></td>
                  <td style={ltd}>{m.source_type}</td>
                  <td style={ltd}>{m.priority}</td>
                  <td style={ltd}>
                    <span style={m.is_system ? systemBadge : customBadge}>
                      {m.is_system ? 'System' : 'Custom'}
                    </span>
                  </td>
                  <td style={{ ...ltd, textAlign: 'right' }}>
                    <Link to={`/mappings/edit/${encodeURIComponent(m.name)}`} style={linkBtn}>Edit</Link>
                    {!m.is_system && (
                      <button onClick={() => remove(m.name)} style={dangerLink}>Delete</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Mapping editor ──────────────────────────────────────────────────────────────

const EMPTY_CONFIG: MappingConfig = {
  destination_object: '',
  source_tables: [],
  operation: 'insert',
  priority: 9999,
  fields: [],
  joins: [],
  filters: [],
};

export function MappingEditor() {
  const { name: editName } = useParams<{ name: string }>();
  const isEdit = Boolean(editName);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  // URL query params passed from the wizard (Step 4 "Customize Mappings")
  const qpSource = searchParams.get('source') ?? '';
  const qpSrcConn = searchParams.get('srcConn') ?? '';
  const qpDestConn = searchParams.get('destConn') ?? '';
  const hasWizardContext = Boolean(qpSource && qpSrcConn && qpDestConn);

  const [availableSources, setAvailableSources] = useState<Source[]>([]);
  const [selectedSourceType, setSelectedSourceType] = useState(qpSource || 'servicenow');

  const [name, setName] = useState(editName ?? '');
  const [config, setConfig] = useState<MappingConfig>({ ...EMPTY_CONFIG, source_type: qpSource || 'servicenow' });
  const [sourceTable, setSourceTable] = useState('');

  const [sourceConns, setSourceConns] = useState<SavedConnection[]>([]);
  const [sfConns, setSfConns] = useState<SavedConnection[]>([]);
  const [sourceConnId, setSourceConnId] = useState(qpSrcConn);
  const [sfConnId, setSfConnId] = useState(qpDestConn);

  const [sourceTables, setSourceTables] = useState<string[]>([]);
  const [sourceFields, setSourceFields] = useState<SourceFieldInfo[]>([]);
  const [destFields, setDestFields] = useState<DestinationFieldInfo[]>([]);

  const [busy, setBusy] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [validation, setValidation] = useState<MappingValidationResult | null>(null);

  // Load available sources, connections, and existing mapping.
  useEffect(() => {
    api.sources.list().then(setAvailableSources).catch(() => {});
    const srcType = qpSource || selectedSourceType || 'servicenow';
    api.connections.list(srcType).then(setSourceConns).catch(() => {});
    api.connections.list('salesforce').then(setSfConns).catch(() => {});

    if (isEdit && editName) {
      api.mappings
        .get(editName)
        .then(({ config: cfg }) => {
          setConfig({ ...EMPTY_CONFIG, ...cfg });
          setSourceTable(cfg.source_tables?.[0] ?? cfg.servicenow_tables?.[0] ?? '');
          if (cfg.source_type) setSelectedSourceType(cfg.source_type);
        })
        .catch((e: Error) => setError(e.message));
    }
  }, [isEdit, editName]);

  // Reload source connections when source type changes
  useEffect(() => {
    if (!hasWizardContext) {
      api.connections.list(selectedSourceType).then(setSourceConns).catch(() => {});
      setSourceConnId('');
    }
  }, [selectedSourceType]);

  const sourceCreds = () => sourceConns.find((c) => c.id === sourceConnId)?.credentials ?? {};
  const sfCreds = () => sfConns.find((c) => c.id === sfConnId)?.credentials ?? {};

  const loadSourceTables = async () => {
    setError(null);
    setBusy('tables');
    try {
      const res = await api.schema.sourceTables(selectedSourceType, sourceCreds());
      setSourceTables(res.tables);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const loadSourceFields = async (table: string) => {
    if (!table) return;
    try {
      const res = await api.schema.sourceFields(selectedSourceType, sourceCreds(), { table });
      setSourceFields(res.fields);
    } catch {
      setSourceFields([]);
    }
  };

  const loadDestFields = async (sfObject: string) => {
    if (!sfObject) return;
    try {
      const res = await api.schema.destinationFields(sfObject, sfCreds());
      setDestFields(res.fields);
    } catch {
      setDestFields([]);
    }
  };

  const destObject = config.destination_object || config.salesforce_object || '';

  const autoSuggest = async () => {
    if (!sourceTable || !destObject) {
      setError('Select a source table and a destination object first.');
      return;
    }
    setError(null);
    setBusy('suggest');
    try {
      const draft = await api.mappings.autoSuggest({
        source_type: selectedSourceType,
        source_table: sourceTable,
        salesforce_object: destObject,
        source_credentials: sourceCreds(),
        destination_credentials: sfCreds(),
      });
      setConfig((c) => ({
        ...c,
        source_tables: draft.source_tables?.length ? draft.source_tables : [sourceTable],
        fields: draft.fields,
      }));
      loadSourceFields(sourceTable);
      loadDestFields(destObject);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const setFields = (fields: MappingFieldDef[]) => setConfig((c) => ({ ...c, fields }));

  const save = async () => {
    setError(null);
    if (!name.trim()) {
      setError('A mapping name is required.');
      return;
    }
    const payload: MappingConfig = {
      ...config,
      source_type: selectedSourceType,
      destination_object: destObject,
      source_tables: sourceTable ? [sourceTable] : (config.source_tables?.length ? config.source_tables : config.servicenow_tables ?? []),
    };
    setBusy('save');
    try {
      if (isEdit) {
        await api.mappings.update(name, payload, selectedSourceType);
      } else {
        await api.mappings.create(name.trim(), payload, selectedSourceType);
      }
      navigate('/mappings');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const validate = async () => {
    if (!isEdit) {
      setError('Save the mapping before validating.');
      return;
    }
    setBusy('validate');
    try {
      const res = await api.mappings.validate(name, sfCreds());
      setValidation(res);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div style={pageStyle}>
      <div style={{ marginBottom: 16 }}>
        <Link to="/mappings" style={{ color: '#2563eb', textDecoration: 'none', fontSize: 14 }}>
          &larr; Back to mappings
        </Link>
      </div>
      <h1 style={h1}>{isEdit ? `Edit mapping: ${editName}` : 'New mapping'}</h1>

      {error && (
        <div style={{ background: '#fef2f2', border: '1px solid #fecaca', color: '#dc2626', padding: 12, borderRadius: 8, marginBottom: 16 }}>
          {error}
        </div>
      )}

      {/* Step 1: Source type & connections — hidden when context is inherited from wizard */}
      {!hasWizardContext && (
        <section style={card}>
          <h3 style={h3}>1. Source type &amp; connections</h3>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <label style={fieldLabel}>
              Source type
              <select
                value={selectedSourceType}
                onChange={(e) => setSelectedSourceType(e.target.value)}
                style={selectStyle}
              >
                {availableSources.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
                {availableSources.length === 0 && (
                  <option value={selectedSourceType}>{selectedSourceType}</option>
                )}
              </select>
            </label>
            <label style={fieldLabel}>
              Source connection
              <select value={sourceConnId} onChange={(e) => setSourceConnId(e.target.value)} style={selectStyle}>
                <option value="">— select —</option>
                {sourceConns.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </label>
            <label style={fieldLabel}>
              Destination connection
              <select value={sfConnId} onChange={(e) => setSfConnId(e.target.value)} style={selectStyle}>
                <option value="">— select —</option>
                {sfConns.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </label>
          </div>
          <p style={hint}>
            Connections supply credentials used for schema discovery and auto-suggest. Manage them on the
            {' '}<Link to="/new" style={{ color: '#2563eb' }}>migration wizard</Link>.
          </p>
        </section>
      )}

      {hasWizardContext && (
        <section style={card}>
          <p style={{ margin: 0, color: '#475569', fontSize: 13 }}>
            Using <strong>{qpSource}</strong> source connection from the migration wizard.
          </p>
        </section>
      )}

      {/* Step 2: tables + object */}
      <section style={card}>
        <h3 style={h3}>{hasWizardContext ? '1' : '2'}. Source &amp; destination</h3>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <label style={fieldLabel}>
            Mapping name
            <input value={name} disabled={isEdit} onChange={(e) => setName(e.target.value)} style={selectStyle} placeholder="e.g. CustomIncident" />
          </label>
          <label style={fieldLabel}>
            Source table
            <input
              list="all-source-tables"
              value={sourceTable}
              onChange={(e) => {
                setSourceTable(e.target.value);
                loadSourceFields(e.target.value);
              }}
              style={selectStyle}
              placeholder="e.g. incident"
            />
            <datalist id="all-source-tables">
              {sourceTables.map((t) => <option key={t} value={t} />)}
            </datalist>
          </label>
          <button type="button" onClick={loadSourceTables} disabled={!sourceConnId || busy === 'tables'} style={secondaryBtn}>
            {busy === 'tables' ? 'Loading\u2026' : 'List tables'}
          </button>
          <label style={fieldLabel}>
            Destination object
            <input
              value={destObject}
              onChange={(e) => setConfig((c) => ({ ...c, destination_object: e.target.value }))}
              onBlur={(e) => loadDestFields(e.target.value)}
              style={selectStyle}
              placeholder="e.g. Case"
            />
          </label>
        </div>
      </section>

      {/* Step 3: auto-suggest + field mapper */}
      <section style={card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h3 style={{ ...h3, margin: 0 }}>{hasWizardContext ? '2' : '3'}. Field mappings</h3>
          <button type="button" onClick={autoSuggest} disabled={busy === 'suggest'} style={primaryBtnSm}>
            {busy === 'suggest' ? 'Analyzing\u2026' : 'Auto-suggest'}
          </button>
        </div>
        {config._meta && (
          <p style={hint}>
            Matched {config._meta.suggested_count} of {config._meta.destination_field_count} writable destination fields
            {' '}from {config._meta.source_field_count} source fields.
          </p>
        )}
        <FieldMapper
          fields={config.fields}
          sourceFields={sourceFields}
          destFields={destFields}
          onChange={setFields}
        />
      </section>

      {/* Step 4: options */}
      <section style={card}>
        <h3 style={h3}>{hasWizardContext ? '3' : '4'}. Operation &amp; priority</h3>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <label style={fieldLabel}>
            Operation
            <select
              value={config.operation}
              onChange={(e) => setConfig((c) => ({ ...c, operation: e.target.value as MappingConfig['operation'] }))}
              style={selectStyle}
            >
              <option value="insert">insert</option>
              <option value="update">update</option>
              <option value="upsert">upsert</option>
            </select>
          </label>
          <label style={fieldLabel}>
            Priority
            <input
              type="number"
              value={config.priority}
              onChange={(e) => setConfig((c) => ({ ...c, priority: Number(e.target.value) }))}
              style={selectStyle}
            />
          </label>
        </div>
      </section>

      {/* Validation results */}
      {validation && (
        <section style={{ ...card, borderColor: validation.valid ? '#bbf7d0' : '#fecaca' }}>
          <h3 style={h3}>Validation: {validation.valid ? 'Valid' : 'Issues found'}</h3>
          {validation.errors.map((e, i) => (
            <div key={`e${i}`} style={{ color: '#dc2626', fontSize: 13 }}>&bull; {e}</div>
          ))}
          {validation.warnings.map((w, i) => (
            <div key={`w${i}`} style={{ color: '#d97706', fontSize: 13 }}>&bull; {w}</div>
          ))}
        </section>
      )}

      {/* Actions */}
      <div style={{ display: 'flex', gap: 12, marginTop: 8 }}>
        <button type="button" onClick={save} disabled={busy === 'save'} style={primaryBtn}>
          {busy === 'save' ? 'Saving\u2026' : 'Save mapping'}
        </button>
        <button type="button" onClick={validate} disabled={busy === 'validate'} style={secondaryBtn}>
          {busy === 'validate' ? 'Validating\u2026' : 'Validate'}
        </button>
      </div>
    </div>
  );
}

// ── Styles ───────────────────────────────────────────────────────────────────────

const pageStyle: React.CSSProperties = { maxWidth: 1000, margin: '0 auto', padding: '32px 24px' };
const h1: React.CSSProperties = { margin: '0 0 16px', fontSize: 24, fontWeight: 700 };
const h3: React.CSSProperties = { margin: '0 0 12px', fontSize: 16, fontWeight: 600 };
const card: React.CSSProperties = {
  background: '#fff',
  border: '1px solid #e5e7eb',
  borderRadius: 10,
  padding: 20,
  marginBottom: 16,
  boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
};
const fieldLabel: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 600, color: '#374151' };
const selectStyle: React.CSSProperties = {
  padding: '8px 10px',
  border: '1px solid #d1d5db',
  borderRadius: 8,
  fontSize: 14,
  minWidth: 200,
};
const hint: React.CSSProperties = { color: '#6b7280', fontSize: 12, marginTop: 10, marginBottom: 0 };
const primaryBtn: React.CSSProperties = {
  padding: '10px 20px',
  background: '#2563eb',
  color: '#fff',
  borderRadius: 8,
  border: 'none',
  textDecoration: 'none',
  fontWeight: 600,
  fontSize: 14,
  cursor: 'pointer',
};
const primaryBtnSm: React.CSSProperties = { ...primaryBtn, padding: '8px 14px' };
const secondaryBtn: React.CSSProperties = {
  padding: '8px 16px',
  background: '#f1f5f9',
  color: '#334155',
  borderRadius: 8,
  border: '1px solid #cbd5e1',
  fontWeight: 600,
  fontSize: 14,
  cursor: 'pointer',
  height: 'fit-content',
};
const lth: React.CSSProperties = { padding: '12px 16px', fontWeight: 600, color: '#475569', fontSize: 13 };
const ltd: React.CSSProperties = { padding: '12px 16px' };
const linkBtn: React.CSSProperties = { color: '#2563eb', textDecoration: 'none', fontWeight: 600, fontSize: 13, marginRight: 12 };
const dangerLink: React.CSSProperties = {
  color: '#dc2626',
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontWeight: 600,
  fontSize: 13,
};
const systemBadge: React.CSSProperties = {
  background: '#f1f5f9',
  color: '#475569',
  padding: '2px 8px',
  borderRadius: 12,
  fontSize: 12,
  fontWeight: 600,
};
const customBadge: React.CSSProperties = {
  background: '#dbeafe',
  color: '#1d4ed8',
  padding: '2px 8px',
  borderRadius: 12,
  fontSize: 12,
  fontWeight: 600,
};
