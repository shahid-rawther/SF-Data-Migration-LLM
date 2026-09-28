import { useState, useMemo } from 'react';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { Input } from './ui/input';

interface SourceTableSelectorProps {
  tables: string[];
  selected: string[];
  onChange: (selected: string[]) => void;
  loading?: boolean;
}

/** Rows drawn at once. A source can list thousands of tables, and painting all
 *  of them is what made each keystroke wait for a full re-render. */
const VISIBLE_LIMIT = 300;

export function SourceTableSelector({ tables, selected, onChange, loading }: SourceTableSelectorProps) {
  const [search, setSearch] = useState('');
  // The box keeps the typed value so the character lands immediately; the list
  // below re-filters once the typing pauses.
  const [debouncedSearch, settling] = useDebouncedValue(search);

  // Lowercasing every table name on each keystroke is the other half of the
  // cost, and it only changes when the table list itself does.
  const haystack = useMemo(
    () => tables.map((name) => ({ name, term: name.toLowerCase() })),
    [tables],
  );

  const filtered = useMemo(() => {
    const term = debouncedSearch.trim().toLowerCase();
    if (!term) return tables;
    return haystack.filter((entry) => entry.term.includes(term)).map((entry) => entry.name);
  }, [haystack, tables, debouncedSearch]);

  const visible = filtered.length > VISIBLE_LIMIT ? filtered.slice(0, VISIBLE_LIMIT) : filtered;
  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const toggle = (name: string) => {
    if (selectedSet.has(name)) {
      onChange(selected.filter((n) => n !== name));
    } else {
      onChange([...selected, name]);
    }
  };

  // Every match, not just the drawn ones, so capping the rows above does not
  // quietly narrow what this selects.
  const selectAll = () => onChange([...new Set([...selected, ...filtered])]);

  const deselectAll = () => {
    const filteredSet = new Set(filtered);
    onChange(selected.filter((s) => !filteredSet.has(s)));
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '48px 0', color: '#737373' }}>
        Loading source tables...
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <Input
          type="search"
          placeholder="Search tables..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1"
        />
        <button type="button" onClick={selectAll} style={btnStyle}>
          Select All
        </button>
        <button type="button" onClick={deselectAll} style={btnStyle}>
          Deselect All
        </button>
        <span style={{ fontSize: 12, color: '#737373', whiteSpace: 'nowrap' }}>
          {selected.length} selected
        </span>
      </div>

      {filtered.length === 0 && (
        <p style={{ color: '#737373', textAlign: 'center', padding: '32px 0' }}>
          {tables.length === 0 ? 'No tables found. Check your source credentials.' : 'No tables match your search.'}
        </p>
      )}

      <div
        style={{
          maxHeight: 400,
          overflowY: 'auto',
          border: '1px solid #e5e5e5',
          borderRadius: 8,
          padding: 8,
          opacity: settling ? 0.6 : 1,
          transition: 'opacity 0.12s ease',
        }}
      >
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 4 }}>
          {visible.map((table) => (
            <label
              key={table}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '5px 8px',
                borderRadius: 6,
                cursor: 'pointer',
                background: selectedSet.has(table) ? '#f5f5f5' : 'transparent',
                border: selectedSet.has(table) ? '1px solid #d4d4d4' : '1px solid transparent',
                fontSize: 13,
                contentVisibility: 'auto',
                containIntrinsicSize: 'auto 28px',
              }}
            >
              <input
                type="checkbox"
                checked={selectedSet.has(table)}
                onChange={() => toggle(table)}
              />
              <span style={{ fontFamily: 'monospace' }}>{table}</span>
            </label>
          ))}
        </div>
      </div>

      {filtered.length > visible.length && (
        <p style={{ fontSize: 12, color: '#737373', marginTop: 8 }}>
          Showing {visible.length} of {filtered.length} matching tables — keep typing to narrow it
          down. Select All still takes every match.
        </p>
      )}
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  padding: '6px 12px',
  borderRadius: 6,
  border: '1px solid #d4d4d4',
  background: '#fff',
  cursor: 'pointer',
  fontSize: 13,
};
