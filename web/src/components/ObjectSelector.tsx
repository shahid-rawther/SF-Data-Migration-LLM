import { useState } from 'react';
import type { MigratableObject } from '../api/client';

interface ObjectSelectorProps {
  objects: MigratableObject[];
  selected: string[];
  onChange: (selected: string[]) => void;
}

export function ObjectSelector({ objects, selected, onChange }: ObjectSelectorProps) {
  const [search, setSearch] = useState('');

  const categories = [...new Set(objects.map((o) => o.category))].sort();

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

  const filtered = (cat: string) =>
    objects
      .filter((o) => o.category === cat)
      .filter((o) => o.name.toLowerCase().includes(search.toLowerCase()));

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8 }}>
        <input
          type="text"
          placeholder="Search objects…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ flex: 1, padding: '6px 10px', borderRadius: 6, border: '1px solid #d1d5db' }}
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
        const items = filtered(cat);
        if (!items.length) return null;
        const inCat = objects.filter((o) => o.category === cat).map((o) => o.name);
        const allSel = inCat.every((n) => selected.includes(n));
        const someSel = inCat.some((n) => selected.includes(n));
        return (
          <div key={cat} style={{ marginBottom: 20 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '6px 0',
                borderBottom: '1px solid #e5e7eb',
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
              <span style={{ fontSize: 12, color: '#6b7280' }}>
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
                    background: selected.includes(obj.name) ? '#eff6ff' : 'transparent',
                    border: selected.includes(obj.name) ? '1px solid #bfdbfe' : '1px solid transparent',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(obj.name)}
                    onChange={() => toggle(obj.name)}
                  />
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 500 }}>{obj.name}</div>
                    <div style={{ fontSize: 11, color: '#6b7280' }}>{obj.salesforce_object}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>
        );
      })}

      {objects.length === 0 && (
        <p style={{ color: '#6b7280', textAlign: 'center', padding: '32px 0' }}>
          No objects available. Select a source first.
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
