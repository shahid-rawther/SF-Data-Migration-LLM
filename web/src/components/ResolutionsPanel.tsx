import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Maximize2, Minimize2 } from 'lucide-react';
import { api } from '../api/client';
import type { AgentChange, AgentResolutionGroup } from '../api/client';
import { FieldEntry } from './AgentFieldEntry';
import { AgentInline } from './AgentMarkdown';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from './ui/sheet';

interface ResolutionsPanelProps {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}

type Filter = 'all' | 'auto_repair' | 'proposal' | 'repair' | 'relationship';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'auto_repair', label: 'Auto-repaired' },
  { id: 'proposal', label: 'Approved proposal' },
  { id: 'repair', label: 'Repair agent' },
  { id: 'relationship', label: 'Relationship agent' },
];

const MIN_WIDTH = 420;
const DEFAULT_WIDTH = 640;
const WIDTH_KEY = 'resolutionsPanelWidth';
/** Groups past this many start collapsed, so a long history opens readable. */
const EXPANDED_BY_DEFAULT = 3;

function clampWidth(width: number): number {
  return Math.round(Math.min(Math.max(width, MIN_WIDTH), window.innerWidth));
}

function storedWidth(): number {
  const saved = Number(localStorage.getItem(WIDTH_KEY));
  return clampWidth(Number.isFinite(saved) && saved > 0 ? saved : DEFAULT_WIDTH);
}

/**
 * Every mapping change an agent applied in this session, whichever agent made
 * it: a deterministic auto-repair rule, the model's repair pass, the pre-run
 * relationship agent, or a proposal a person approved. Each change can be
 * reverted from here, one field at a time.
 */
export function ResolutionsPanel({ sessionId, open, onClose }: ResolutionsPanelProps) {
  const [groups, setGroups] = useState<AgentResolutionGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [reverting, setReverting] = useState<number | null>(null);
  const [width, setWidth] = useState(storedWidth);
  const [dragging, setDragging] = useState(false);
  const widthBeforeFull = useRef(DEFAULT_WIDTH);

  const load = useCallback(
    (showSpinner: boolean) => {
      if (showSpinner) setLoading(true);
      api.agents
        .resolutions(sessionId)
        .then((next) => {
          setGroups(next);
          setError('');
        })
        .catch((e: Error) => setError(e.message))
        .finally(() => setLoading(false));
    },
    [sessionId],
  );

  useEffect(() => {
    if (open) load(true);
  }, [open, load]);

  useEffect(() => {
    const onResize = () => setWidth((current) => clampWidth(current));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    localStorage.setItem(WIDTH_KEY, String(width));
  }, [width]);

  const isFull = width >= window.innerWidth;
  const toggleFull = () => {
    if (isFull) {
      setWidth(clampWidth(widthBeforeFull.current));
    } else {
      widthBeforeFull.current = width;
      setWidth(window.innerWidth);
    }
  };

  const startDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  };
  const onDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    setWidth(clampWidth(window.innerWidth - event.clientX));
  };
  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    event.currentTarget.releasePointerCapture(event.pointerId);
    setDragging(false);
  };

  const counts = useMemo(() => {
    const byFilter: Record<Filter, number> = {
      all: groups.length,
      auto_repair: 0,
      proposal: 0,
      repair: 0,
      relationship: 0,
    };
    for (const group of groups) {
      if (group.source in byFilter) byFilter[group.source as Filter] += 1;
    }
    return byFilter;
  }, [groups]);

  const visible = filter === 'all' ? groups : groups.filter((g) => g.source === filter);
  const totalChanges = groups.reduce((sum, g) => sum + g.changes.length, 0);

  const revert = async (change: AgentChange) => {
    setReverting(change.id);
    setError('');
    try {
      await api.agents.revertChange(change.id);
      load(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not revert the change');
    } finally {
      setReverting(null);
    }
  };

  const groupKey = (group: AgentResolutionGroup) => `${group.run_id}:${group.source}`;
  const isCollapsed = (group: AgentResolutionGroup, index: number) =>
    collapsed[groupKey(group)] ?? index >= EXPANDED_BY_DEFAULT;

  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent
        aria-label="Agent resolutions"
        style={{ width, maxWidth: 'none', userSelect: dragging ? 'none' : undefined }}
      >
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Drag to resize"
          title="Drag to resize · double-click for full width"
          onPointerDown={startDrag}
          onPointerMove={onDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onDoubleClick={toggleFull}
          style={{ ...dragHandle, background: dragging ? '#d4d4d4' : 'transparent' }}
        >
          <span style={dragGrip} />
        </div>

        <button
          type="button"
          onClick={toggleFull}
          style={iconButton}
          title={isFull ? 'Exit full width' : 'Full width'}
          aria-label={isFull ? 'Exit full width' : 'Full width'}
        >
          {isFull ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
        </button>

        <SheetHeader style={{ paddingRight: 80 }}>
          <SheetTitle>Agent resolutions</SheetTitle>
          <SheetDescription>
            Every mapping change an agent applied in this session
            {groups.length ? ` · ${totalChanges} change(s) across ${groups.length} run(s)` : ''}
          </SheetDescription>
        </SheetHeader>

        <div style={filterBar}>
          {FILTERS.map((option) => (
            <button
              key={option.id}
              type="button"
              onClick={() => setFilter(option.id)}
              style={chip(filter === option.id)}
            >
              {option.label} <span style={{ opacity: 0.6 }}>{counts[option.id]}</span>
            </button>
          ))}
        </div>

        <div style={body}>
          {error && <div style={errorBox}>{error}</div>}
          {loading && !groups.length && (
            <>
              {[0, 1, 2].map((i) => (
                <div key={i} style={{ ...skeleton, height: 84, marginBottom: 10 }} />
              ))}
            </>
          )}
          {!loading && !visible.length && !error && (
            <p style={muted}>
              {groups.length
                ? 'No changes of this kind in this session.'
                : 'No agent has changed a mapping in this session yet.'}
            </p>
          )}

          {visible.map((group, index) => {
            const folded = isCollapsed(group, index);
            const revertedCount = group.changes.filter((c) => c.reverted).length;
            return (
              <div key={groupKey(group)} style={groupCard}>
                <button
                  type="button"
                  onClick={() =>
                    setCollapsed((prev) => ({ ...prev, [groupKey(group)]: !folded }))
                  }
                  style={groupHeader}
                  aria-expanded={!folded}
                >
                  <span style={{ fontSize: 11, color: '#a3a3a3', width: 10 }}>{folded ? '▸' : '▾'}</span>
                  <SourceTag source={group.source} />
                  {group.source === 'proposal' && group.origin && (
                    <span style={originTag}>
                      {group.origin === 'diagnosis' ? 'from diagnosis' : 'from chat'}
                    </span>
                  )}
                  <code style={{ fontSize: 12, fontWeight: 700, color: '#171717' }}>
                    {group.mappings.join(', ')}
                  </code>
                  <span style={{ fontSize: 12, color: '#525252' }}>
                    {group.changes.length} field{group.changes.length === 1 ? '' : 's'}
                  </span>
                  {revertedCount > 0 && (
                    <span style={revertedTag}>
                      {revertedCount === group.changes.length ? 'Reverted' : `${revertedCount} reverted`}
                    </span>
                  )}
                  <span style={{ marginLeft: 'auto', fontSize: 11.5, color: '#a3a3a3', whiteSpace: 'nowrap' }}>
                    {group.applied_at ? new Date(group.applied_at).toLocaleString() : ''}
                  </span>
                </button>

                {group.proposal && (
                  <div style={proposalLine}>
                    <strong style={{ color: '#171717' }}>{group.proposal.summary || 'Approved proposal'}</strong>
                    {group.proposal.rerun.length > 0 && (
                      <span style={{ color: '#737373' }}> · re-ran {group.proposal.rerun.join(', ')}</span>
                    )}
                  </div>
                )}

                {!folded && (
                  <div style={{ marginTop: 8 }}>
                    {group.changes.map((change) => (
                      <ChangeRow
                        key={change.id}
                        change={change}
                        wide={width >= 760}
                        busy={reverting === change.id}
                        onRevert={() => revert(change)}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function SourceTag({ source }: { source: string }) {
  const style = SOURCE_STYLES[source] ?? SOURCE_STYLES.other;
  return <span style={{ ...tagBase, ...style.style }}>{style.label}</span>;
}

const SOURCE_STYLES: Record<string, { label: string; style: React.CSSProperties }> = {
  auto_repair: {
    label: 'Auto-repaired',
    style: { background: '#171717', color: '#fff', border: '1px solid #171717' },
  },
  proposal: {
    label: 'Approved proposal',
    style: { background: '#fff', color: '#171717', border: '1px solid #171717' },
  },
  repair: {
    label: 'Repair agent',
    style: { background: '#e5e5e5', color: '#262626', border: '1px solid #e5e5e5' },
  },
  relationship: {
    label: 'Relationship agent',
    style: { background: '#fff', color: '#525252', border: '1px dashed #a3a3a3' },
  },
  other: {
    label: 'Agent',
    style: { background: '#f5f5f5', color: '#525252', border: '1px solid #e5e5e5' },
  },
};

function ChangeRow({
  change,
  wide,
  busy,
  onRevert,
}: {
  change: AgentChange;
  wide: boolean;
  busy: boolean;
  onRevert: () => void;
}) {
  return (
    <div style={{ ...changeRow, opacity: change.reverted ? 0.6 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <code style={{ fontSize: 12, fontWeight: 700, color: '#171717' }}>
          {change.field || change.mapping_name}
        </code>
        <span style={{ fontSize: 11, color: '#737373' }}>{change.kind.replace(/_/g, ' ')}</span>
        {change.reverted && <span style={revertedTag}>Reverted</span>}
        <span style={{ marginLeft: 'auto' }}>
          {!change.reverted && (
            <button type="button" onClick={onRevert} disabled={busy} style={ghostButton}>
              {busy ? 'Reverting…' : 'Revert'}
            </button>
          )}
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: wide ? '1fr 1fr' : '1fr', gap: 8, marginTop: 6 }}>
        <div style={valueBox}>
          <div style={valueLabel}>Before</div>
          <FieldEntry value={change.previous_value} maxChars={wide ? 1200 : 300} />
        </div>
        <div style={valueBox}>
          <div style={valueLabel}>After</div>
          <FieldEntry value={change.new_value} maxChars={wide ? 1200 : 300} />
        </div>
      </div>
      {change.rationale && (
        <p style={{ margin: '6px 0 0', fontSize: 12, color: '#525252', lineHeight: 1.5 }}>
          <AgentInline text={change.rationale} />
        </p>
      )}
    </div>
  );
}

const dragHandle: React.CSSProperties = {
  position: 'absolute',
  left: -3,
  top: 0,
  bottom: 0,
  width: 6,
  cursor: 'col-resize',
  zIndex: 1,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  touchAction: 'none',
};
const dragGrip: React.CSSProperties = {
  width: 3,
  height: 36,
  borderRadius: 2,
  background: '#d4d4d4',
};
const filterBar: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  flexWrap: 'wrap',
  padding: '10px 20px',
  borderBottom: '1px solid #f5f5f5',
};
const body: React.CSSProperties = {
  padding: '14px 20px 32px',
  overflowY: 'auto',
  flex: 1,
};
const muted: React.CSSProperties = { color: '#737373', fontSize: 13 };
const errorBox: React.CSSProperties = {
  padding: '8px 10px',
  marginBottom: 10,
  borderRadius: 7,
  background: '#fafafa',
  border: '1px solid #d4d4d4',
  fontSize: 12.5,
  color: '#171717',
};
const skeleton: React.CSSProperties = {
  borderRadius: 8,
  background: 'linear-gradient(90deg, #f5f5f5 25%, #ebebeb 50%, #f5f5f5 75%)',
  backgroundSize: '200% 100%',
  animation: 'shimmer 1.5s ease-in-out infinite',
};
const groupCard: React.CSSProperties = {
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  padding: '10px 12px',
  marginBottom: 10,
  background: '#fff',
};
const groupHeader: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  flexWrap: 'wrap',
  width: '100%',
  padding: 0,
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  textAlign: 'left',
};
const proposalLine: React.CSSProperties = {
  marginTop: 6,
  fontSize: 12.5,
  lineHeight: 1.5,
};
const tagBase: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  padding: '2px 8px',
  borderRadius: 999,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
  whiteSpace: 'nowrap',
};
const originTag: React.CSSProperties = {
  ...tagBase,
  background: '#f5f5f5',
  color: '#404040',
  border: '1px solid #e5e5e5',
  textTransform: 'none',
  letterSpacing: 0,
};
const revertedTag: React.CSSProperties = {
  ...tagBase,
  background: '#fff',
  color: '#a3a3a3',
  border: '1px solid #d4d4d4',
};
const changeRow: React.CSSProperties = {
  padding: '8px 10px',
  marginBottom: 6,
  borderRadius: 7,
  background: '#fafafa',
  border: '1px solid #f0f0f0',
};
const valueBox: React.CSSProperties = {
  padding: '6px 8px',
  borderRadius: 6,
  background: '#fff',
  border: '1px solid #e5e5e5',
  minWidth: 0,
};
const valueLabel: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: '#737373',
  marginBottom: 3,
};
const ghostButton: React.CSSProperties = {
  padding: '4px 10px',
  background: '#fff',
  border: '1px solid #e5e5e5',
  borderRadius: 6,
  fontSize: 11.5,
  fontWeight: 600,
  color: '#525252',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

/** Sits just left of the Sheet's close button, centred on the same line. */
const iconButton: React.CSSProperties = {
  position: 'absolute',
  top: 10,
  right: 40,
  zIndex: 1,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 28,
  height: 28,
  padding: 0,
  background: 'transparent',
  border: 'none',
  borderRadius: 6,
  color: '#525252',
  cursor: 'pointer',
};

function chip(active: boolean): React.CSSProperties {
  return {
    padding: '4px 10px',
    borderRadius: 999,
    fontSize: 12,
    fontWeight: 600,
    cursor: 'pointer',
    border: `1px solid ${active ? '#171717' : '#e5e5e5'}`,
    background: active ? '#171717' : '#fff',
    color: active ? '#fff' : '#404040',
  };
}
