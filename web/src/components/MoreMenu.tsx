import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

export interface MoreMenuItem {
  label: string;
  description?: string;
  onSelect: () => void;
  disabled?: boolean;
  /** Shown on hover, mostly to say why an item is disabled. */
  title?: string;
  danger?: boolean;
}

interface Props {
  items: MoreMenuItem[];
  /** The trigger's content; defaults to a "More" label. */
  label?: ReactNode;
  /** Narrow icon-only trigger, for use inside cards. */
  compact?: boolean;
  align?: 'left' | 'right';
}

/** A dropdown for actions that are occasionally useful but need not sit on the page. */
export function MoreMenu({ items, label, compact = false, align = 'right' }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  return (
    <div ref={root} style={{ position: 'relative', display: 'inline-block' }}>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        title="More actions"
        style={compact ? compactTrigger : trigger}
      >
        {label ?? (compact ? '\u22EF' : <>More <span style={{ fontSize: 10 }}>&#9662;</span></>)}
      </button>
      {open && (
        <div role="menu" style={{ ...menu, [align]: 0 }}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              title={item.title}
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                item.onSelect();
              }}
              style={menuItem(Boolean(item.disabled), Boolean(item.danger))}
            >
              <div style={{ fontWeight: 600 }}>{item.label}</div>
              {item.description && <div style={itemDescription}>{item.description}</div>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const trigger: React.CSSProperties = {
  padding: '8px 14px',
  background: '#fff',
  border: '1px solid #d4d4d4',
  borderRadius: 8,
  fontSize: 13,
  fontWeight: 600,
  color: '#404040',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const compactTrigger: React.CSSProperties = {
  width: 28,
  height: 26,
  background: '#fff',
  border: '1px solid #e5e5e5',
  borderRadius: 6,
  fontSize: 15,
  lineHeight: 1,
  color: '#525252',
  cursor: 'pointer',
};

const menu: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 4px)',
  zIndex: 40,
  minWidth: 220,
  maxWidth: 300,
  background: '#fff',
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  boxShadow: '0 8px 24px rgba(0,0,0,0.10)',
  padding: 4,
};

const menuItem = (disabled: boolean, danger: boolean): React.CSSProperties => ({
  display: 'block',
  width: '100%',
  textAlign: 'left',
  background: 'none',
  border: 'none',
  borderRadius: 6,
  padding: '7px 10px',
  fontSize: 13,
  color: disabled ? '#a3a3a3' : danger ? '#0a0a0a' : '#262626',
  cursor: disabled ? 'not-allowed' : 'pointer',
});

const itemDescription: React.CSSProperties = {
  fontSize: 11.5,
  color: '#737373',
  fontWeight: 400,
  marginTop: 1,
  lineHeight: 1.4,
};
