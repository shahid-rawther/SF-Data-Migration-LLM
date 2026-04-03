import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Source, MigratableObject } from '../api/client';
import { CredentialForm } from '../components/CredentialForm';
import { ObjectSelector } from '../components/ObjectSelector';

type Step = 1 | 2 | 3 | 4;

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
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.sources.list().then(setSources);
  }, []);

  const validateSource = async () => {
    if (!selectedSource) return;
    setValidating(true);
    setValidationMsg(null);
    try {
      const res = await api.sources.validateCredentials(selectedSource.id, sourceCreds);
      setValidationMsg(res.valid ? '✅ Connection successful' : '❌ Connection failed');
    } catch {
      setValidationMsg('❌ Could not reach server');
    } finally {
      setValidating(false);
    }
  };

  const loadObjects = async () => {
    if (!selectedSource) return;
    const objs = await api.sources.listObjects(selectedSource.id);
    setObjects(objs);
    setSelectedObjects(objs.map((o) => o.name)); // default: all selected
  };

  const goStep = (s: Step) => {
    setValidationMsg(null);
    if (s === 3 && objects.length === 0) loadObjects();
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
        {(['Select Source', 'Destination', 'Select Objects', 'Options & Review'] as const).map(
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
                    onChange={() => { setSelectedSource(s); setSourceCreds({}); }}
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
                <CredentialForm
                  schema={selectedSource.credentials_schema}
                  values={sourceCreds}
                  onChange={setSourceCreds}
                />
                <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
                  <button onClick={validateSource} disabled={validating} style={secondaryBtn}>
                    {validating ? 'Testing…' : 'Test Connection'}
                  </button>
                  {validationMsg && <span style={{ fontSize: 13 }}>{validationMsg}</span>}
                </div>
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
            <CredentialForm
              schema={SALESFORCE_SCHEMA as any}
              values={destCreds}
              onChange={setDestCreds}
            />
            <div style={navRow}>
              <button onClick={() => goStep(1)} style={secondaryBtn}>← Back</button>
              <button onClick={() => goStep(3)} style={primaryBtn}>Next →</button>
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

        {/* Step 4 — Options & Review */}
        {step === 4 && (
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
                <label style={labelStyle}>ServiceNow Batch Size</label>
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
                <div><strong>Dry run:</strong> {options.disable_load ? 'Yes' : 'No'}</div>
              </div>
            </div>

            {error && <p style={{ color: '#dc2626', fontSize: 13 }}>Error: {error}</p>}

            <div style={navRow}>
              <button onClick={() => goStep(3)} style={secondaryBtn}>← Back</button>
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
