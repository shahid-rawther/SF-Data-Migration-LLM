import type { CredentialsSchema, SavedConnection } from '../api/client';
import { CredentialForm } from './CredentialForm';

interface Props {
  schema: CredentialsSchema;
  savedConnections: SavedConnection[];
  /** A saved connection id, 'new' for typed credentials, or '' before a choice. */
  selected: string;
  onSelect: (id: string) => void;
  credentials: Record<string, string>;
  onCredentialsChange: (creds: Record<string, string>) => void;
  testing: boolean;
  /** null before a test; otherwise whether the last test passed and what it said. */
  testResult: { ok: boolean; message: string } | null;
  onTest: () => void;
  saveName: string;
  onSaveNameChange: (name: string) => void;
  saving: boolean;
  savedAsName: string | null;
  onSave: () => void;
  namePlaceholder: string;
}

/**
 * Pick a saved connection or type new credentials, test it, and optionally keep it.
 *
 * With saved connections the form stays hidden until "New connection" is chosen,
 * so the common case is one dropdown and a test button.
 */
export function ConnectionPicker({
  schema, savedConnections, selected, onSelect, credentials, onCredentialsChange,
  testing, testResult, onTest, saveName, onSaveNameChange, saving, savedAsName, onSave,
  namePlaceholder,
}: Props) {
  const hasSaved = savedConnections.length > 0;
  const typing = selected === 'new' || !hasSaved;
  const canSave = typing && testResult?.ok && !savedAsName;

  return (
    <div>
      {hasSaved && (
        <div style={{ marginBottom: typing ? 14 : 0 }}>
          <label style={label}>Connection</label>
          <select value={selected} onChange={(e) => onSelect(e.target.value)} style={select}>
            <option value="">Choose a saved connection…</option>
            {savedConnections.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
            <option value="new">+ New connection</option>
          </select>
        </div>
      )}

      {typing && (
        <CredentialForm schema={schema} values={credentials} onChange={onCredentialsChange} />
      )}

      {(typing || selected) && (
        <div style={{ marginTop: 14, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <button type="button" onClick={onTest} disabled={testing} style={button}>
            {testing ? 'Testing…' : 'Test connection'}
          </button>
          {testResult && (
            <span style={{ fontSize: 13, color: testResult.ok ? '#171717' : '#0a0a0a' }}>
              {testResult.ok ? '\u2713 ' : '\u2717 '}
              {testResult.message}
            </span>
          )}
        </div>
      )}

      {canSave && (
        <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center', maxWidth: 520 }}>
          <input
            type="text"
            placeholder={namePlaceholder}
            value={saveName}
            onChange={(e) => onSaveNameChange(e.target.value)}
            style={{ ...select, flex: 1 }}
          />
          <button type="button" onClick={onSave} disabled={saving || !saveName.trim()} style={button}>
            {saving ? 'Saving…' : 'Save for next time'}
          </button>
        </div>
      )}
      {savedAsName && (
        <div style={{ marginTop: 8, fontSize: 12.5, color: '#525252' }}>
          Saved as &ldquo;{savedAsName}&rdquo;
        </div>
      )}
    </div>
  );
}

const label: React.CSSProperties = { fontSize: 13, fontWeight: 500, display: 'block', marginBottom: 4 };

const select: React.CSSProperties = {
  width: '100%',
  maxWidth: 520,
  padding: '8px 10px',
  border: '1px solid #d4d4d4',
  borderRadius: 6,
  fontSize: 14,
  boxSizing: 'border-box',
  background: '#fff',
};

const button: React.CSSProperties = {
  padding: '8px 16px',
  background: '#fff',
  color: '#262626',
  border: '1px solid #d4d4d4',
  borderRadius: 8,
  cursor: 'pointer',
  fontWeight: 600,
  fontSize: 13,
  whiteSpace: 'nowrap',
};
