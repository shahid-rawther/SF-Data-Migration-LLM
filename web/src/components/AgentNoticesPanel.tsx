import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { AgentChange } from '../api/client';
import { AgentInline } from './AgentMarkdown';

interface Props {
  sessionId: string;
  /** Re-read when the run reaches a point that could have changed the answer. */
  refreshKey?: number;
}

/**
 * What the agent had to say about this migration, and what it changed.
 *
 * The deterministic checks only catch the edge cases someone wrote a rule for.
 * The agent is asked to review beyond them and to raise a notice for anything
 * else that could lose or corrupt data, so this panel is where an edge case
 * nobody anticipated shows up. A notice changes nothing on its own — it is a
 * concern to act on, and it is listed here rather than left in a log.
 *
 * Only the current attempt is shown. A notice from an earlier attempt was
 * written against a mapping that has since been edited and nothing
 * re-evaluates it, so leaving it in the list presents a finding already fixed
 * as though it were still true.
 */
export function AgentNoticesPanel({ sessionId, refreshKey = 0 }: Props) {
  const [entries, setEntries] = useState<AgentChange[]>([]);
  const [earlierCount, setEarlierCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const [latest, all] = await Promise.all([
        api.agents.changes({ session_id: sessionId, scope: 'latest' }),
        api.agents.changes({ session_id: sessionId }),
      ]);
      setEntries(latest);
      const latestIds = new Set(latest.map((entry) => entry.id));
      setEarlierCount(
        all.filter((entry) => entry.kind === 'notice' && !latestIds.has(entry.id)).length,
      );
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read what the agent reported');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const clearEarlier = async () => {
    setClearing(true);
    try {
      await api.agents.discardEarlierNotices(sessionId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not clear the earlier notices');
    } finally {
      setClearing(false);
    }
  };

  if (loading) return null;

  const notices = entries.filter((entry) => entry.kind === 'notice');
  const applied = entries.filter((entry) => entry.kind !== 'notice' && !entry.reverted);
  if (!notices.length && !applied.length && !earlierCount && !error) return null;

  const concerning = notices.filter((n) => n.severity !== 'info');
  // Housekeeping, not something to announce: the order is always recomputed
  // from the schema, never a judgement the agent made.
  const wired = applied.filter((c) => c.kind !== 'load_order');

  return (
    <section style={panel}>
      <header style={{ marginBottom: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15 }}>&#9873;</span>
          <h3 style={{ margin: 0, fontSize: 14.5, fontWeight: 700, color: '#171717' }}>
            What the agent reported
          </h3>
          {concerning.length > 0 && (
            <span style={countPill}>{concerning.length} to look at</span>
          )}
        </div>
        <p style={intro}>
          Raised during the latest run of this migration. A notice changes nothing by
          itself; it is something the agent thought you should decide on or check.
        </p>
      </header>

      {error && <div style={errorBox}>{error}</div>}

      {wired.length > 0 && <WiredSummary changes={wired} />}

      {notices.map((notice) => (
        <NoticeRow key={notice.id} notice={notice} />
      ))}

      {!notices.length && wired.length > 0 && (
        <p style={{ margin: '0 0 8px', fontSize: 12.5, color: '#737373' }}>
          Nothing was flagged for you to decide on.
        </p>
      )}

      {applied.length > wired.length && (
        <details style={{ marginTop: 8 }}>
          <summary style={summaryToggle}>
            Load order the agent recomputed
          </summary>
          <div style={{ marginTop: 8 }}>
            {applied
              .filter((change) => change.kind === 'load_order')
              .map((change) => (
                <div key={change.id} style={changeRow}>
                  <code style={{ fontSize: 12, fontWeight: 700, color: '#171717' }}>
                    {change.mapping_name}
                  </code>
                  {change.rationale && (
                    <p style={{ margin: '4px 0 0', fontSize: 12, color: '#525252', width: '100%' }}>
                      <AgentInline text={change.rationale} />
                    </p>
                  )}
                </div>
              ))}
          </div>
        </details>
      )}

      {earlierCount > 0 && (
        <div style={earlierBox}>
          <span>
            {earlierCount} notice{earlierCount === 1 ? '' : 's'} from earlier attempts
            {' '}
            {earlierCount === 1 ? 'is' : 'are'} hidden. They were raised against mappings
            you have since changed, so they may no longer apply.
          </span>
          <button type="button" onClick={clearEarlier} disabled={clearing} style={clearButton}>
            {clearing ? 'Clearing…' : 'Clear them'}
          </button>
        </div>
      )}
    </section>
  );
}

function WiredSummary({ changes }: { changes: AgentChange[] }) {
  const byMapping = new Map<string, AgentChange[]>();
  for (const change of changes) {
    const list = byMapping.get(change.mapping_name) ?? [];
    list.push(change);
    byMapping.set(change.mapping_name, list);
  }

  return (
    <div style={wiredBox}>
      <div style={{ fontSize: 12.5, fontWeight: 700, color: '#171717', marginBottom: 6 }}>
        The agent changed {changes.length} field
        {changes.length === 1 ? '' : 's'} in your mappings before this migration ran
      </div>
      {[...byMapping.entries()].map(([mapping, list]) => (
        <div key={mapping} style={{ marginBottom: 6 }}>
          <code style={{ fontSize: 12, fontWeight: 700, color: '#171717' }}>{mapping}</code>
          {list.map((change) => (
            <div key={change.id} style={{ fontSize: 12, color: '#404040', marginTop: 3 }}>
              <span style={kindPill}>{change.kind.replace(/_/g, ' ')}</span>{' '}
              {change.field && <code style={{ fontSize: 11.5 }}>{change.field}</code>}
              {change.rationale && <> — <AgentInline text={change.rationale} /></>}
            </div>
          ))}
        </div>
      ))}
      <p style={{ margin: '6px 0 0', fontSize: 11.5, color: '#737373' }}>
        Every change is reversible individually from the mapping editor.
      </p>
    </div>
  );
}

function NoticeRow({ notice }: { notice: AgentChange }) {
  return (
    <div style={row(notice.severity)}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
        <code style={{ fontSize: 12.5, fontWeight: 700, color: '#171717' }}>
          {notice.mapping_name || 'This migration'}
          {notice.field ? `.${notice.field}` : ''}
        </code>
        <span style={severityPill(notice.severity)}>{notice.severity}</span>
      </div>
      <p style={{ margin: '5px 0 0', fontSize: 12.5, color: '#404040', lineHeight: 1.5 }}>
        <AgentInline text={notice.rationale} />
      </p>
      {notice.suggested_action && (
        <p style={suggestion}>
          <strong style={{ color: '#171717' }}>What to do:</strong>{' '}
          <AgentInline text={notice.suggested_action} />
        </p>
      )}
    </div>
  );
}

const earlierBox: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 10,
  flexWrap: 'wrap',
  marginTop: 10,
  paddingTop: 9,
  borderTop: '1px solid #f5f5f5',
  fontSize: 11.5,
  color: '#a3a3a3',
  lineHeight: 1.5,
};

const clearButton: React.CSSProperties = {
  padding: '3px 9px',
  borderRadius: 6,
  border: '1px solid #e5e5e5',
  background: '#fff',
  color: '#525252',
  fontSize: 11.5,
  fontWeight: 600,
  cursor: 'pointer',
};

const wiredBox: React.CSSProperties = {
  padding: 10,
  marginBottom: 10,
  borderRadius: 8,
  background: '#fafafa',
  border: '1px solid #e5e5e5',
};

const suggestion: React.CSSProperties = {
  margin: '6px 0 0',
  padding: '6px 8px',
  borderRadius: 6,
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  fontSize: 12.5,
  color: '#404040',
  lineHeight: 1.5,
};

const panel: React.CSSProperties = {
  padding: 14,
  marginBottom: 16,
  borderRadius: 10,
  background: '#fff',
  border: '1px solid #e5e5e5',
};

const intro: React.CSSProperties = {
  margin: '6px 0 0',
  fontSize: 12.5,
  color: '#737373',
  lineHeight: 1.5,
};

function row(severity: string): React.CSSProperties {
  const accent = severity === 'blocker' ? '#0a0a0a' : severity === 'warning' ? '#525252' : '#a3a3a3';
  return {
    padding: '9px 11px',
    marginTop: 8,
    borderRadius: 8,
    background: '#fafafa',
    border: '1px solid #e5e5e5',
    borderLeft: `3px solid ${accent}`,
  };
}

const changeRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  flexWrap: 'wrap',
  padding: '7px 10px',
  marginTop: 6,
  borderRadius: 7,
  background: '#fafafa',
  border: '1px solid #f5f5f5',
};

function severityPill(severity: string): React.CSSProperties {
  const tones: Record<string, [string, string]> = {
    blocker: ['#171717', '#f5f5f5'],
    warning: ['#404040', '#f5f5f5'],
    info: ['#404040', '#f5f5f5'],
  };
  const [color, background] = tones[severity] ?? tones.info;
  return {
    fontSize: 10.5,
    fontWeight: 700,
    color,
    background,
    padding: '2px 7px',
    borderRadius: 999,
    textTransform: 'uppercase',
    letterSpacing: 0.3,
  };
}

const kindPill: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 600,
  color: '#525252',
  background: '#f5f5f5',
  padding: '2px 7px',
  borderRadius: 999,
};

const countPill: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: '#404040',
  background: '#f5f5f5',
  padding: '2px 8px',
  borderRadius: 999,
};

const errorBox: React.CSSProperties = {
  padding: '8px 10px',
  marginBottom: 8,
  borderRadius: 7,
  background: '#fafafa',
  border: '1px solid #d4d4d4',
  fontSize: 12.5,
  color: '#171717',
};

const summaryToggle: React.CSSProperties = {
  fontSize: 12,
  color: '#737373',
  cursor: 'pointer',
};
