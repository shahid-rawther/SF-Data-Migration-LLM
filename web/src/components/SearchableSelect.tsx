import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';

export interface SelectOption {
  value: string;
  label: string;
}

interface SearchableSelectProps {
  value: string;
  options: SelectOption[];
  placeholder?: string;
  onChange: (value: string) => void;
  style?: React.CSSProperties;
}

export function SearchableSelect({
  value,
  options,
  placeholder = '-- select --',
  onChange,
  style,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [highlighted, setHighlighted] = useState(0);
  const triggerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [panelPos, setPanelPos] = useState({ top: 0, left: 0, width: 0 });

  const selectedLabel = useMemo(
    () => options.find((o) => o.value === value)?.label ?? '',
    [options, value],
  );

  const filtered = useMemo(
    () => search
      ? options.filter((o) => o.label.toLowerCase().includes(search.toLowerCase()))
      : options,
    [options, search],
  );

  const select = useCallback((val: string) => {
    onChange(val);
    setOpen(false);
    setSearch('');
    setHighlighted(0);
  }, [onChange]);

  const openDropdown = useCallback(() => {
    if (triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      setPanelPos({
        top: rect.bottom + 2,
        left: rect.left,
        width: Math.max(rect.width, 280),
      });
    }
    setOpen(true);
  }, []);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        triggerRef.current?.contains(target) ||
        panelRef.current?.contains(target)
      ) return;
      setOpen(false);
      setSearch('');
      setHighlighted(0);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  useEffect(() => {
    if (open && inputRef.current) {
      inputRef.current.focus();
    }
  }, [open]);

  useEffect(() => {
    setHighlighted(0);
  }, [search]);

  useEffect(() => {
    if (!open || !listRef.current) return;
    const active = listRef.current.children[highlighted] as HTMLElement | undefined;
    active?.scrollIntoView({ block: 'nearest' });
  }, [highlighted, open]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlighted((h) => Math.min(h + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlighted((h) => Math.max(h - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (filtered[highlighted]) {
        select(filtered[highlighted].value);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
      setSearch('');
      setHighlighted(0);
    }
  };

  const dropdown = open
    ? createPortal(
        <div
          ref={panelRef}
          style={{
            position: 'fixed',
            top: panelPos.top,
            left: panelPos.left,
            width: panelPos.width,
            background: '#fff',
            border: '1px solid #d1d5db',
            borderRadius: 8,
            boxShadow: '0 8px 24px rgba(0,0,0,0.15)',
            zIndex: 9999,
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <div style={{ padding: '8px 8px 6px', borderBottom: '1px solid #e5e7eb' }}>
            <input
              ref={inputRef}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type to search..."
              style={{
                width: '100%',
                padding: '6px 10px',
                border: '1px solid #d1d5db',
                borderRadius: 6,
                fontSize: 13,
                boxSizing: 'border-box',
                outline: 'none',
              }}
            />
          </div>

          <div ref={listRef} style={{ maxHeight: 240, overflowY: 'auto' }}>
            {filtered.length === 0 && (
              <div style={{ padding: '10px 12px', color: '#9ca3af', fontSize: 12 }}>
                No matches found
              </div>
            )}
            {filtered.map((opt, i) => (
              <div
                key={opt.value}
                onMouseDown={(e) => {
                  e.preventDefault();
                  select(opt.value);
                }}
                onMouseEnter={() => setHighlighted(i)}
                style={{
                  padding: '7px 12px',
                  fontSize: 13,
                  cursor: 'pointer',
                  background: i === highlighted ? '#eff6ff' : opt.value === value ? '#f0f9ff' : '#fff',
                  color: opt.value === value ? '#2563eb' : '#1f2937',
                  fontWeight: opt.value === value ? 600 : 400,
                  borderLeft: opt.value === value ? '3px solid #2563eb' : '3px solid transparent',
                  borderBottom: '1px solid #f3f4f6',
                }}
              >
                {opt.label}
              </div>
            ))}
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <div style={{ position: 'relative', ...style }}>
      <div
        ref={triggerRef}
        onClick={() => open ? setOpen(false) : openDropdown()}
        style={{
          ...triggerStyle,
          borderColor: open ? '#2563eb' : '#d1d5db',
          color: value ? '#111827' : '#9ca3af',
        }}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            open ? setOpen(false) : openDropdown();
          }
        }}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
          {selectedLabel || placeholder}
        </span>
        <span style={{ fontSize: 10, color: '#9ca3af', flexShrink: 0, marginLeft: 4 }}>
          {open ? '\u25B4' : '\u25BE'}
        </span>
      </div>
      {dropdown}
    </div>
  );
}

const triggerStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 8px',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 13,
  boxSizing: 'border-box',
  background: '#fff',
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  minHeight: 32,
};
