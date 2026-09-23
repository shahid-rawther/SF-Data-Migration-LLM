import { useState, useMemo } from 'react';

interface DestinationObject {
  name: string;
  label: string;
}

interface DestinationObjectSelectorProps {
  objects: DestinationObject[];
  selected: string[];
  onChange: (selected: string[]) => void;
  loading?: boolean;
}

/** Multi-select over the Salesforce objects the migration will write into.
 *
 *  This is the destination side of the selection, and what the dependency
 *  hierarchy is computed from. An org describes over a thousand objects, so the
 *  list is searchable and there is deliberately no select-all.
 */
export function DestinationObjectSelector({
  objects,
  selected,
  onChange,
  loading,
}: DestinationObjectSelectorProps) {
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return objects.slice(0, 200);
    return objects
      .filter((o) => o.name.toLowerCase().includes(term) || o.label.toLowerCase().includes(term))
      .slice(0, 200);
  }, [objects, search]);

  const toggle = (name: string) =>
    onChange(selected.includes(name) ? selected.filter((n) => n !== name) : [...selected, name]);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '48px 0', color: '#6b7280' }}>
        Loading Salesforce objects...
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          type="text"
          placeholder="Search Salesforce objects..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ flex: 1, padding: '6px 10px', borderRadius: 6, border: '1px solid #d1d5db' }}
        />
        {selected.length > 0 && (
          <button type="button" onClick={() => onChange([])} style={btnStyle}>
            Clear
          </button>
        )}
        <span style={{ fontSize: 12, color: '#6b7280', whiteSpace: 'nowrap' }}>
          {selected.length} selected
        </span>
      </div>

      {selected.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
          {selected.map((name) => (
            <button key={name} type="button" onClick={() => toggle(name)} style={chipStyle}>
              {name}
              <span style={{ color: '#6b7280' }}>×</span>
            </button>
          ))}
        </div>
      )}

      <div
        style={{
          maxHeight: 400,
          overflowY: 'auto',
          border: '1px solid #e5e7eb',
          borderRadius: 8,
          padding: 8,
        }}
      >
        {filtered.length === 0 ? (
          <p style={{ color: '#6b7280', textAlign: 'center', padding: '32px 0' }}>
            {objects.length === 0
              ? 'No objects found. Check the Salesforce connection.'
              : 'No objects match your search.'}
          </p>
        ) : (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
              gap: 4,
            }}
          >
            {filtered.map((obj) => (
              <label
                key={obj.name}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '5px 8px',
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: selected.includes(obj.name) ? '#eff6ff' : 'transparent',
                  border: selected.includes(obj.name)
                    ? '1px solid #bfdbfe'
                    : '1px solid transparent',
                  fontSize: 13,
                  minWidth: 0,
                }}
              >
                <input
                  type="checkbox"
                  checked={selected.includes(obj.name)}
                  onChange={() => toggle(obj.name)}
                />
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={`${obj.label} (${obj.name})`}
                >
                  {obj.label}
                  {obj.label !== obj.name && (
                    <span style={{ color: '#9ca3af', fontFamily: 'monospace', marginLeft: 6 }}>
                      {obj.name}
                    </span>
                  )}
                </span>
              </label>
            ))}
          </div>
        )}
      </div>

      {objects.length > filtered.length && !search.trim() && (
        <p style={{ fontSize: 12, color: '#6b7280', marginTop: 8 }}>
          Showing the first {filtered.length} of {objects.length} objects — search to narrow it down.
        </p>
      )}
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  padding: '6px 12px',
  borderRadius: 6,
  border: '1px solid #d1d5db',
  background: '#fff',
  cursor: 'pointer',
  fontSize: 13,
};

const chipStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '3px 8px',
  borderRadius: 999,
  border: '1px solid #bfdbfe',
  background: '#eff6ff',
  color: '#1e40af',
  fontSize: 12,
  fontFamily: 'monospace',
  cursor: 'pointer',
};
