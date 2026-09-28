import { useState, useMemo } from 'react';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { Input } from './ui/input';

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
  // As in the source list: the box owns the typed value so it never waits on
  // the filter, which runs once the typing pauses.
  const [debouncedSearch, settling] = useDebouncedValue(search);

  // An org describes over a thousand objects, so their searchable forms are
  // built once rather than on every keystroke.
  const haystack = useMemo(
    () =>
      objects.map((object) => ({
        object,
        term: `${object.name} ${object.label}`.toLowerCase(),
      })),
    [objects],
  );

  const filtered = useMemo(() => {
    const term = debouncedSearch.trim().toLowerCase();
    if (!term) return objects.slice(0, 200);
    const matches: DestinationObject[] = [];
    for (const entry of haystack) {
      if (!entry.term.includes(term)) continue;
      matches.push(entry.object);
      if (matches.length === 200) break;
    }
    return matches;
  }, [objects, haystack, debouncedSearch]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const toggle = (name: string) =>
    onChange(selectedSet.has(name) ? selected.filter((n) => n !== name) : [...selected, name]);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '48px 0', color: '#737373' }}>
        Loading Salesforce objects...
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <Input
          type="search"
          placeholder="Search Salesforce objects..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1"
        />
        {selected.length > 0 && (
          <button type="button" onClick={() => onChange([])} style={btnStyle}>
            Clear
          </button>
        )}
        <span style={{ fontSize: 12, color: '#737373', whiteSpace: 'nowrap' }}>
          {selected.length} selected
        </span>
      </div>

      {selected.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
          {selected.map((name) => (
            <button key={name} type="button" onClick={() => toggle(name)} style={chipStyle}>
              {name}
              <span style={{ color: '#737373' }}>×</span>
            </button>
          ))}
        </div>
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
        {filtered.length === 0 ? (
          <p style={{ color: '#737373', textAlign: 'center', padding: '32px 0' }}>
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
                  background: selectedSet.has(obj.name) ? '#f5f5f5' : 'transparent',
                  border: selectedSet.has(obj.name)
                    ? '1px solid #d4d4d4'
                    : '1px solid transparent',
                  fontSize: 13,
                  minWidth: 0,
                  contentVisibility: 'auto',
                  containIntrinsicSize: 'auto 28px',
                }}
              >
                <input
                  type="checkbox"
                  checked={selectedSet.has(obj.name)}
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
                    <span style={{ color: '#a3a3a3', fontFamily: 'monospace', marginLeft: 6 }}>
                      {obj.name}
                    </span>
                  )}
                </span>
              </label>
            ))}
          </div>
        )}
      </div>

      {objects.length > filtered.length && (
        <p style={{ fontSize: 12, color: '#737373', marginTop: 8 }}>
          {debouncedSearch.trim()
            ? `Showing the first ${filtered.length} matches — keep typing to narrow it down.`
            : `Showing the first ${filtered.length} of ${objects.length} objects — search to narrow it down.`}
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

const chipStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '3px 8px',
  borderRadius: 999,
  border: '1px solid #d4d4d4',
  background: '#f5f5f5',
  color: '#262626',
  fontSize: 12,
  fontFamily: 'monospace',
  cursor: 'pointer',
};
