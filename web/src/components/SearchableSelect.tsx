import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronsUpDown, Search } from 'lucide-react';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { Input } from './ui/input';

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
  const [debouncedSearch, settling] = useDebouncedValue(search);
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

  const haystack = useMemo(
    () => options.map((option) => ({ option, term: option.label.toLowerCase() })),
    [options],
  );

  const matchesFor = useCallback(
    (text: string) => {
      const term = text.trim().toLowerCase();
      if (!term) return options;
      return haystack.filter((entry) => entry.term.includes(term)).map((entry) => entry.option);
    },
    [haystack, options],
  );

  const filtered = useMemo(() => matchesFor(debouncedSearch), [matchesFor, debouncedSearch]);

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
      // Enter can arrive before the debounce has caught up, so the choice is
      // made against what the typed text matches now, not the list on screen.
      const list = settling ? matchesFor(search) : filtered;
      if (list[highlighted]) {
        select(list[highlighted].value);
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
          className="fixed z-[9999] flex flex-col overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md"
          style={{ top: panelPos.top, left: panelPos.left, width: panelPos.width }}
        >
          <div className="relative border-b p-2">
            <Search className="pointer-events-none absolute left-5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              ref={inputRef}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setHighlighted(0);
              }}
              onKeyDown={handleKeyDown}
              placeholder="Type to search..."
              className="h-9 pl-9"
            />
          </div>

          <div
            ref={listRef}
            style={{
              maxHeight: 240,
              overflowY: 'auto',
              opacity: settling ? 0.6 : 1,
              transition: 'opacity 0.12s ease',
            }}
          >
            {filtered.length === 0 && (
              <div className="py-6 text-center text-sm text-muted-foreground">
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
                className={`relative flex cursor-default select-none items-center rounded-sm py-2 pl-8 pr-2 text-sm outline-none ${
                  i === highlighted ? 'bg-accent text-accent-foreground' : ''
                }`}
              >
                {opt.value === value && <Check className="absolute left-2 size-4" />}
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
        className={`flex min-h-9 w-full cursor-pointer items-center rounded-md border bg-background px-3 py-2 text-sm shadow-sm outline-none ${
          open ? 'ring-2 ring-ring' : ''
        } ${value ? 'text-foreground' : 'text-muted-foreground'}`}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            if (open) setOpen(false);
            else openDropdown();
          }
        }}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
          {selectedLabel || placeholder}
        </span>
        <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
      </div>
      {dropdown}
    </div>
  );
}
