import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Source, MigratableObject, SalesforceDiagnosticsResponse, SavedConnection, CompatibilityReport } from '../api/client';
import { CredentialForm } from '../components/CredentialForm';
import { ObjectSelector } from '../components/ObjectSelector';

type Step = 1 | 2 | 3 | 4 | 5;

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
  const [objects, setObjects] = useState<MigratableObject[]>([]);
  const [selectedObjects, setSelectedObjects] = useState<string[]>([]);
  const [options, setOptions] = useState({
    start_date: '',
    end_date: '',
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

  const loadObjects = async () => {
    if (!selectedSource) return;
    const objs = await api.sources.listObjects(selectedSource.id);
    setObjects(objs);
    setSelectedObjects(objs.map((o) => o.name)); // default: all selected
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

  const checkObjectVisibility = async () => {
    setCheckingDiagnostics(true);
    setDiagnostics(null);
    setExpandDetails(false);
    try {
      const res = await api.destinations.checkSalesforceDiagnostics(destCreds, selectedObjects);
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
        selectedObjects,
      );
      setCompatReport(report);
    } catch (e: unknown) {
      setCompatError(e instanceof Error ? e.message : 'Compatibility check failed');
    } finally {
      setCheckingCompat(false);
    }
  };

  const goStep = (s: Step) => {
    setValidationMsg(null);
    setDestValidationMsg(null);
    setDiagnostics(null);
    setExpandDetails(false);
    if (s === 3 && objects.length === 0) loadObjects();
    if (s === 4) {
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
        source_credentials: sourceCreds,
        destination_credentials: destCreds,
        selected_objects: selectedObjects,
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
        {(['Select Source', 'Destination', 'Select Objects', 'Compatibility', 'Options & Review'] as const).map(
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

        {/* Step 3 — Objects */}
        {step === 3 && (
          <div>
            <h2 style={h2}>Select Objects to Migrate</h2>
            <p style={{ color: '#6b7280', fontSize: 13, marginBottom: 16 }}>
              {selectedObjects.length} of {objects.length} objects selected
            </p>
            <ObjectSelector objects={objects} selected={selectedObjects} onChange={setSelectedObjects} />
            <div style={navRow}>
              <button onClick={() => goStep(2)} style={secondaryBtn}>← Back</button>
              <button
                disabled={selectedObjects.length === 0}
                onClick={() => goStep(4)}
                style={primaryBtn}
              >
                Next →
              </button>
            </div>
          </div>
        )}

        {/* Step 4 — Compatibility Check */}
        {step === 4 && (
          <div>
            <h2 style={h2}>Compatibility Check</h2>
            <p style={{ color: '#6b7280', fontSize: 13, marginBottom: 16 }}>
              Analyse how well your selected {selectedSource?.name} objects map onto Salesforce.
              This is a dry-run analysis — no data is moved.
            </p>

            <div style={{ marginBottom: 16 }}>
              <button
                onClick={runCompatibilityCheck}
                disabled={checkingCompat}
                style={primaryBtn}
              >
                {checkingCompat ? '⏳ Checking…' : '🔍 Run Compatibility Check'}
              </button>
            </div>

            {compatError && (
              <div style={{ color: '#dc2626', fontSize: 13, marginBottom: 12 }}>
                ⚠️ {compatError}
              </div>
            )}

            {compatReport && (
              <div style={{ marginBottom: 20 }}>
                {/* Overall score banner */}
                <div style={{
                  padding: 14, borderRadius: 8, marginBottom: 16,
                  background: compatReport.overall_pct >= 80 ? '#f0fdf4' : compatReport.overall_pct >= 50 ? '#fffbeb' : '#fef2f2',
                  border: `1px solid ${compatReport.overall_pct >= 80 ? '#86efac' : compatReport.overall_pct >= 50 ? '#fcd34d' : '#f87171'}`,
                }}>
                  <div style={{ fontWeight: 700, fontSize: 16 }}>
                    {compatReport.overall_pct >= 80 ? '✅' : compatReport.overall_pct >= 50 ? '⚠️' : '❌'}{' '}
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
                        <span style={{ fontSize: 12, color: '#6b7280', marginLeft: 8 }}>→ {obj.salesforce_object}</span>
                      </div>
                      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                        <span style={{
                          fontSize: 13, fontWeight: 600,
                          color: obj.compatibility_pct >= 80 ? '#16a34a' : obj.compatibility_pct >= 50 ? '#b45309' : '#dc2626',
                        }}>
                          {obj.compatibility_pct}%
                        </span>
                        <span style={{ fontSize: 12, color: '#6b7280' }}>
                          {obj.mapped_fields}/{obj.total_fields} fields mapped
                        </span>
                        <span>{expandedCompatObj === obj.object_name ? '▼' : '▶'}</span>
                      </div>
                    </div>
                    {expandedCompatObj === obj.object_name && (
                      <div style={{ padding: 14 }}>
                        {obj.blockers.length > 0 && (
                          <div style={{ marginBottom: 10, padding: 10, background: '#fef2f2', borderRadius: 6, border: '1px solid #fca5a5' }}>
                            {obj.blockers.map((b, i) => (
                              <div key={i} style={{ fontSize: 12, color: '#dc2626' }}>🚫 {b}</div>
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
                            </tr>
                          </thead>
                          <tbody>
                            {obj.fields.map((f, i) => (
                              <tr key={i} style={{ borderBottom: '1px solid #e5e7eb' }}>
                                <td style={tdStyle}>{f.source_field || '—'}</td>
                                <td style={tdStyle}>{f.destination_field}</td>
                                <td style={tdStyle}>
                                  <span style={{
                                    padding: '2px 6px', borderRadius: 4, fontSize: 11,
                                    background: f.status === 'mapped' ? '#dcfce7' : f.status === 'type_mismatch' ? '#fef9c3' : '#fee2e2',
                                    color: f.status === 'mapped' ? '#166534' : f.status === 'type_mismatch' ? '#854d0e' : '#991b1b',
                                  }}>
                                    {f.status}
                                  </span>
                                </td>
                                <td style={{ ...tdStyle, color: '#6b7280' }}>{f.suggestion || '—'}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            <div style={navRow}>
              <button onClick={() => goStep(3)} style={secondaryBtn}>← Back</button>
              <button onClick={() => goStep(5)} style={primaryBtn}>Next →</button>
            </div>
          </div>
        )}

        {/* Step 5 — Options & Review */}
        {step === 5 && (
          <div>
            <h2 style={h2}>Options & Review</h2>

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
                  value={options.service_now_batch_size}
                  onChange={(e) => setOptions((o) => ({ ...o, service_now_batch_size: +e.target.value }))}
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
                <div><strong>Objects:</strong> {selectedObjects.length} selected</div>
                {compatReport && (
                  <div>
                    <strong>Compatibility:</strong>{' '}
                    <span style={{ color: compatReport.overall_pct >= 80 ? '#16a34a' : compatReport.overall_pct >= 50 ? '#b45309' : '#dc2626' }}>
                      {compatReport.overall_pct}%
                    </span>
                  </div>
                )}
                <div><strong>Dry run:</strong> {options.disable_load ? 'Yes (extract & transform only)' : 'No'}</div>
              </div>
            </div>

            {error && <p style={{ color: '#dc2626', fontSize: 13 }}>Error: {error}</p>}

            <div style={navRow}>
              <button onClick={() => goStep(4)} style={secondaryBtn}>← Back</button>
              <button onClick={submit} disabled={submitting} style={primaryBtn}>
                {submitting ? 'Creating…' : '🚀 Create Migration'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────
const pageStyle: React.CSSProperties = { maxWidth: 820, margin: '0 auto', padding: '32px 24px' };
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
