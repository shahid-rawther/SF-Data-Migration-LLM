import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type { DestinationFieldInfo } from '../api/client';

interface ObjectFieldsDrawerProps {
  /** The Salesforce object to describe. Null closes the drawer. */
  objectName: string | null;
  destCreds: Record<string, string>;
  /** A saved connection to describe through, when the credentials are masked. */
  destConnectionId?: string;
  onClose: () => void;
}

type Filter = 'all' | 'required' | 'lookups' | 'picklists';

/** The fields of one Salesforce object, in a panel over the right-hand side.
 *
 *  Opened from the hierarchy to answer the question the graph raises but cannot
 *  itself answer: the graph says an object is in the way, and this says what is
 *  actually in it. So the columns are the ones that decide a mapping — the type,
 *  whether a record can be created without the field, what a lookup points at —
 *  rather than everything a describe call returns.
 *
 *  Objects here run to several hundred fields, which is past reading top to
 *  bottom, so the search box is the primary way in and the filters cover the
 *  three questions that come up repeatedly.
 */
export function ObjectFieldsDrawer({
  objectName,
  destCreds,
  destConnectionId,
  onClose,
}: ObjectFieldsDrawerProps) {
  const [fields, setFields] = useState<DestinationFieldInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');

  useEffect(() => {
    if (!objectName) return;
    let live = true;
    setLoading(true);
    setError('');
    setFields([]);
    setQuery('');
    setFilter('all');
    api.schema
      .destinationFields(objectName, destCreds, { connection_id: destConnectionId })
      .then((result) => {
        if (live) setFields(result.fields);
      })
      .catch((e) => {
        if (live) setError(e instanceof Error ? e.message : `Could not describe ${objectName}`);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    // A slow describe for an object the user has already navigated away from
    // must not overwrite the one they are now looking at.
    return () => {
      live = false;
    };
  }, [objectName, destCreds, destConnectionId]);

  useEffect(() => {
    if (!objectName) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [objectName, onClose]);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return fields.filter((f) => {
      if (filter === 'required' && !f.required) return false;
      if (filter === 'lookups' && !f.reference_to.length) return false;
      if (filter === 'picklists' && !f.picklist_values.length) return false;
      if (!needle) return true;
      return (
        f.name.toLowerCase().includes(needle) ||
        f.label.toLowerCase().includes(needle) ||
        f.type.toLowerCase().includes(needle)
      );
    });
  }, [fields, query, filter]);

  const counts = useMemo(
    () => ({
      all: fields.length,
      required: fields.filter((f) => f.required).length,
      lookups: fields.filter((f) => f.reference_to.length).length,
      picklists: fields.filter((f) => f.picklist_values.length).length,
    }),
    [fields],
  );

  if (!objectName) return null;

  return (
    <>
      <div style={backdrop} onClick={onClose} />
      <aside style={drawer} role="dialog" aria-label={`${objectName} fields`}>
        <header style={head}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontFamily: 'monospace', fontSize: 15, fontWeight: 700, color: '#111827' }}>
              {objectName}
            </div>
            <div style={{ fontSize: 12, color: '#6b7280', marginTop: 2 }}>
              {loading
                ? 'Describing the object…'
                : `${counts.all} field${counts.all === 1 ? '' : 's'} · ${counts.required} required · ${counts.lookups} lookup${counts.lookups === 1 ? '' : 's'}`}
            </div>
          </div>
          <button type="button" onClick={onClose} style={closeBtn} aria-label="Close">
            ✕
          </button>
        </header>

        {error && <div style={errorBox}>{error}</div>}

        {!loading && !error && fields.length > 0 && (
          <div style={controls}>
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by name, label or type"
              style={search}
            />
            <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
              {(['all', 'required', 'lookups', 'picklists'] as Filter[]).map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setFilter(f)}
                  disabled={counts[f] === 0}
                  style={chip(filter === f, counts[f] === 0)}
                >
                  {f === 'all' ? 'All' : f[0].toUpperCase() + f.slice(1)} ({counts[f]})
                </button>
              ))}
            </div>
          </div>
        )}

        <div style={body}>
          {loading && <p style={muted}>Reading the schema from Salesforce…</p>}

          {!loading && !error && !fields.length && (
            <p style={muted}>
              No fields came back. The connected user may not have read access to {objectName}.
            </p>
          )}

          {!loading && fields.length > 0 && !shown.length && (
            <p style={muted}>Nothing matches “{query}”.</p>
          )}

          {shown.map((field) => (
            <FieldRow key={field.name} field={field} />
          ))}
        </div>
      </aside>
    </>
  );
}

function FieldRow({ field }: { field: DestinationFieldInfo }) {
  // A field the API will not let us set cannot take a value from the source, so
  // it is dimmed rather than hidden: it still explains what the object holds.
  const settable = field.createable;

  return (
    <div style={{ ...row, opacity: settable ? 1 : 0.55 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' }}>
        <code style={{ fontSize: 12.5, fontWeight: 700, color: '#111827' }}>{field.name}</code>
        <span style={typeBadge}>
          {field.type}
          {field.length > 0 && (field.type === 'string' || field.type === 'textarea')
            ? `(${field.length})`
            : ''}
        </span>
        {field.required && <span style={pill('#b45309', '#fef3c7')}>required</span>}
        {field.id_lookup && <span style={pill('#3730a3', '#e0e7ff')}>upsert key</span>}
        {!settable && <span style={pill('#6b7280', '#f3f4f6')}>read-only</span>}
      </div>

      {field.label && field.label !== field.name && (
        <div style={{ fontSize: 11.5, color: '#6b7280', marginTop: 2 }}>{field.label}</div>
      )}

      {field.reference_to.length > 0 && (
        <div style={{ fontSize: 11.5, color: '#4b5563', marginTop: 3 }}>
          → points at <strong>{field.reference_to.slice(0, 4).join(', ')}</strong>
          {field.reference_to.length > 4 && ` and ${field.reference_to.length - 4} more`}
        </div>
      )}

      {field.picklist_values.length > 0 && (
        <div style={{ fontSize: 11.5, color: '#4b5563', marginTop: 3 }}>
          {field.picklist_values.slice(0, 6).join(' · ')}
          {field.picklist_values.length > 6 && ` … +${field.picklist_values.length - 6}`}
        </div>
      )}
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
  width: 'min(460px, 92vw)',
  background: '#fff',
  borderLeft: '1px solid #e5e7eb',
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
  borderBottom: '1px solid #e5e7eb',
};

const closeBtn: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontSize: 15,
  color: '#6b7280',
  padding: 4,
  lineHeight: 1,
};

const controls: React.CSSProperties = {
  padding: '10px 16px',
  borderBottom: '1px solid #f3f4f6',
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const search: React.CSSProperties = {
  width: '100%',
  padding: '7px 10px',
  border: '1px solid #d1d5db',
  borderRadius: 7,
  fontSize: 12.5,
  boxSizing: 'border-box',
};

const chip = (active: boolean, disabled: boolean): React.CSSProperties => ({
  padding: '3px 9px',
  background: active ? '#111827' : '#fff',
  color: active ? '#fff' : disabled ? '#9ca3af' : '#374151',
  border: `1px solid ${active ? '#111827' : '#e5e7eb'}`,
  borderRadius: 20,
  fontSize: 11.5,
  fontWeight: 600,
  cursor: disabled ? 'default' : 'pointer',
});

const body: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '4px 16px 20px',
};

const row: React.CSSProperties = {
  padding: '9px 0',
  borderBottom: '1px solid #f3f4f6',
};

const typeBadge: React.CSSProperties = {
  fontFamily: 'monospace',
  fontSize: 11,
  color: '#0369a1',
  background: '#f0f9ff',
  border: '1px solid #bae6fd',
  padding: '0 5px',
  borderRadius: 4,
};

const pill = (color: string, background: string): React.CSSProperties => ({
  fontSize: 9.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
  color,
  background,
  padding: '1px 5px',
  borderRadius: 4,
});

const muted: React.CSSProperties = {
  fontSize: 12.5,
  color: '#6b7280',
  margin: '14px 0',
};

const errorBox: React.CSSProperties = {
  margin: '12px 16px',
  padding: '9px 12px',
  background: '#fef2f2',
  border: '1px solid #fecaca',
  borderRadius: 8,
  color: '#991b1b',
  fontSize: 12.5,
};
