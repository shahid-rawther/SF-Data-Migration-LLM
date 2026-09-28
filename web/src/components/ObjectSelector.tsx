import { useMemo, useState } from 'react';
import type { MigratableObject } from '../api/client';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { Input } from './ui/input';

interface ObjectSelectorProps {
  objects: MigratableObject[];
  selected: string[];
  onChange: (selected: string[]) => void;
}

export function ObjectSelector({ objects, selected, onChange }: ObjectSelectorProps) {
  const [search, setSearch] = useState('');
  const [debouncedSearch, settling] = useDebouncedValue(search);

  const categories = useMemo(
    () => [...new Set(objects.map((o) => o.category))].sort(),
    [objects],
  );

  const toggle = (name: string) => {
    if (selected.includes(name)) {
      onChange(selected.filter((n) => n !== name));
    } else {
      onChange([...selected, name]);
    }
  };

  const toggleCategory = (category: string) => {
    const inCat = objects.filter((o) => o.category === category).map((o) => o.name);
    const allSelected = inCat.every((n) => selected.includes(n));
    if (allSelected) {
      onChange(selected.filter((n) => !inCat.includes(n)));
    } else {
      const newSel = [...new Set([...selected, ...inCat])];
      onChange(newSel);
    }
  };

  // Grouped once per search rather than re-filtered for each category on every
  // render, so a keystroke costs a single pass over the objects.
  const byCategory = useMemo(() => {
    const term = debouncedSearch.trim().toLowerCase();
    const groups = new Map<string, MigratableObject[]>();
    for (const object of objects) {
      if (term && !object.name.toLowerCase().includes(term)) continue;
      const group = groups.get(object.category);
      if (group) group.push(object);
      else groups.set(object.category, [object]);
    }
    return groups;
  }, [objects, debouncedSearch]);

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8 }}>
        <Input
          type="search"
          placeholder="Search objects…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1"
        />
        <button
          type="button"
          onClick={() => onChange(objects.map((o) => o.name))}
          style={btnStyle}
        >
          Select All
        </button>
        <button type="button" onClick={() => onChange([])} style={btnStyle}>
          Deselect All
        </button>
      </div>

      {categories.map((cat) => {
        const items = byCategory.get(cat);
        if (!items?.length) return null;
        const inCat = objects.filter((o) => o.category === cat).map((o) => o.name);
        const allSel = inCat.every((n) => selected.includes(n));
        const someSel = inCat.some((n) => selected.includes(n));
        return (
          <div
            key={cat}
            style={{ marginBottom: 20, opacity: settling ? 0.6 : 1, transition: 'opacity 0.12s ease' }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '6px 0',
                borderBottom: '1px solid #e5e5e5',
                marginBottom: 8,
              }}
            >
              <input
                type="checkbox"
                checked={allSel}
                ref={(el) => {
                  if (el) el.indeterminate = someSel && !allSel;
                }}
                onChange={() => toggleCategory(cat)}
              />
              <strong style={{ fontSize: 14 }}>{cat}</strong>
              <span style={{ fontSize: 12, color: '#737373' }}>
                ({inCat.filter((n) => selected.includes(n)).length}/{inCat.length})
              </span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 6 }}>
              {items.map((obj) => (
                <label
                  key={obj.name}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '4px 8px',
                    borderRadius: 6,
                    cursor: 'pointer',
                    background: selected.includes(obj.name) ? '#f5f5f5' : 'transparent',
                    border: selected.includes(obj.name) ? '1px solid #d4d4d4' : '1px solid transparent',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(obj.name)}
                    onChange={() => toggle(obj.name)}
                  />
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 500 }}>{obj.name}</div>
                    <div style={{ fontSize: 11, color: '#737373' }}>{obj.salesforce_object}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>
        );
      })}

      {objects.length === 0 && (
        <p style={{ color: '#737373', textAlign: 'center', padding: '32px 0' }}>
          No objects available. Select a source first.
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
