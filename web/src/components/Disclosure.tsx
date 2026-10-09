import { useState } from 'react';
import type { ReactNode } from 'react';

interface Props {
  label: ReactNode;
  /** Shown as a small pill after the label, so a closed section still says what it holds. */
  count?: number;
  /** One line shown next to the label while closed. */
  hint?: ReactNode;
  defaultOpen?: boolean;
  /** Controlled mode; when set, `defaultOpen` is ignored. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** `boxed` draws a bordered section; `plain` is just the toggle and the content. */
  variant?: 'boxed' | 'plain';
  style?: React.CSSProperties;
  children: ReactNode;
}

/** A secondary section that stays out of the way until someone asks for it. */
export function Disclosure({
  label, count, hint, defaultOpen = false, open: controlled, onOpenChange,
  variant = 'plain', style, children,
}: Props) {
  const [internal, setInternal] = useState(defaultOpen);
  const open = controlled ?? internal;
  const toggle = () => {
    const next = !open;
    if (controlled === undefined) setInternal(next);
    onOpenChange?.(next);
  };

  return (
    <div style={{ ...(variant === 'boxed' ? boxed : {}), ...style }}>
      <button type="button" onClick={toggle} aria-expanded={open} style={toggleStyle}>
        <span style={{ ...chevron, transform: open ? 'rotate(90deg)' : 'none' }}>&#9656;</span>
        <span style={{ fontWeight: 600, color: '#262626' }}>{label}</span>
        {count !== undefined && count > 0 && <span style={pill}>{count}</span>}
        {hint && !open && <span style={hintStyle}>{hint}</span>}
      </button>
      {open && <div style={{ marginTop: 8 }}>{children}</div>}
    </div>
  );
}

const boxed: React.CSSProperties = {
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  padding: '8px 12px',
  background: '#fff',
};

const toggleStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  width: '100%',
  background: 'none',
  border: 'none',
  padding: '2px 0',
  fontSize: 12.5,
  cursor: 'pointer',
  textAlign: 'left',
};

const chevron: React.CSSProperties = {
  display: 'inline-block',
  width: 10,
  color: '#737373',
  fontSize: 11,
  transition: 'transform 0.12s',
};

const pill: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  color: '#404040',
  background: '#f5f5f5',
  border: '1px solid #e5e5e5',
  borderRadius: 999,
  padding: '0 7px',
};

const hintStyle: React.CSSProperties = {
  fontSize: 12,
  color: '#a3a3a3',
  fontWeight: 400,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  minWidth: 0,
};
