import { useState, useMemo } from 'react';

interface SourceTableSelectorProps {
  tables: string[];
  selected: string[];
  onChange: (selected: string[]) => void;
  loading?: boolean;
}

export function SourceTableSelector({ tables, selected, onChange, loading }: SourceTableSelectorProps) {
  const [search, setSearch] = useState('');

  const filtered = useMemo(
    () => tables.filter((t) => t.toLowerCase().includes(search.toLowerCase())),
    [tables, search],
  );

  const toggle = (name: string) => {
    if (selected.includes(name)) {
      onChange(selected.filter((n) => n !== name));
    } else {
      onChange([...selected, name]);
    }
  };

  const selectAll = () => onChange([...new Set([...selected, ...filtered])]);

  const deselectAll = () => {
    const filteredSet = new Set(filtered);
    onChange(selected.filter((s) => !filteredSet.has(s)));
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '48px 0', color: '#6b7280' }}>
        Loading source tables...
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          type="text"
          placeholder="Search tables..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ flex: 1, padding: '6px 10px', borderRadius: 6, border: '1px solid #d1d5db' }}
        />
        <button type="button" onClick={selectAll} style={btnStyle}>
          Select All
        </button>
        <button type="button" onClick={deselectAll} style={btnStyle}>
          Deselect All
        </button>
        <span style={{ fontSize: 12, color: '#6b7280', whiteSpace: 'nowrap' }}>
          {selected.length} selected
        </span>
      </div>

      {filtered.length === 0 && (
        <p style={{ color: '#6b7280', textAlign: 'center', padding: '32px 0' }}>
          {tables.length === 0 ? 'No tables found. Check your source credentials.' : 'No tables match your search.'}
        </p>
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
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 4 }}>
          {filtered.map((table) => (
            <label
              key={table}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '5px 8px',
                borderRadius: 6,
                cursor: 'pointer',
                background: selected.includes(table) ? '#eff6ff' : 'transparent',
                border: selected.includes(table) ? '1px solid #bfdbfe' : '1px solid transparent',
                fontSize: 13,
              }}
            >
              <input
                type="checkbox"
                checked={selected.includes(table)}
                onChange={() => toggle(table)}
              />
              <span style={{ fontFamily: 'monospace' }}>{table}</span>
            </label>
          ))}
        </div>
      </div>
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
