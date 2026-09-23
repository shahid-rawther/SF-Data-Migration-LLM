import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { ColumnFillJob, ColumnFillTarget, SessionConnections } from '../api/client';
import { ColumnFillDrawer } from './ColumnFillDrawer';

interface Props {
  sessionId: string;
  sourceType: string;
  /** Re-read when the run reaches a point that could have changed the answer. */
  refreshKey?: number;
}

/**
 * A way to put a value into one column of an object that has already migrated.
 *
 * Unlike the panels around it, this one does not appear because something needs
 * attention — there is nothing to report and nothing owed. It is an action, and
 * an action nobody knows about might as well not exist, so it stays on the page
 * as one quiet row rather than hiding until some condition makes it relevant.
 *
 * It appears once the session has loaded records, because that is the point at
 * which there is a column to fill; before that the button could only fail.
 *
 * The work itself happens in a drawer. Keeping it there is what lets this stay a
 * single row: the rule builder needs real room, and it is not something anyone
 * needs to look at while reading the run.
 */
export function ColumnFillPanel({ sessionId, sourceType, refreshKey = 0 }: Props) {
  const [targets, setTargets] = useState<ColumnFillTarget[]>([]);
  const [jobs, setJobs] = useState<ColumnFillJob[]>([]);
  const [conns, setConns] = useState<SessionConnections | null>(null);
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const [t, j] = await Promise.all([
        api.migrations.columnFill.targets(sessionId),
        api.migrations.columnFill.list(sessionId),
      ]);
      setTargets(t.targets);
      setJobs(j.jobs);
    } catch {
      // Reading this is not worth an error on a page about a migration run.
      setTargets([]);
    } finally {
      setLoaded(true);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  useEffect(() => {
    api.migrations
      .connections(sessionId)
      .then(setConns)
      .catch(() => setConns(null));
  }, [sessionId]);

  // Before anything has loaded there is no record to update, so the row would
  // offer something that could only fail. Past fills keep it present after that.
  if (!loaded || (!targets.length && !jobs.length)) return null;

  const destConnId = conns?.destination.connection_id ?? '';
  const sourceConnId = conns?.source.connection_id ?? '';
  const linked = Boolean(destConnId);
  const recorded = jobs.filter((j) => j.status !== 'draft');

  return (
    <section style={panel}>
      <div style={rowLayout}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
            <span style={{ fontSize: 14, color: '#6b7280' }}>&#9998;</span>
            <h3 style={{ margin: 0, fontSize: 13.5, fontWeight: 700, color: '#111827' }}>
              Fill a column
            </h3>
          </div>
          <p style={intro}>
            Add or correct values on records this session has already migrated — a
            constant, a value matched from a source column, or one worked out with
            conditions. You see what it would write before anything is sent.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen(true)}
          disabled={!linked || !targets.length}
          title={
            !targets.length
              ? 'Nothing in this session has loaded records yet'
              : !linked
                ? 'Link a Salesforce connection to this session first'
                : 'Choose a column and see what would be written'
          }
          style={button(!linked || !targets.length)}
        >
          Fill a column
        </button>
      </div>

      {!linked && targets.length > 0 && (
        <p style={hint}>
          This session has no saved Salesforce connection linked, so the column's
          allowed values cannot be read. Link one under the objects below.
        </p>
      )}

      {recorded.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary style={summaryToggle}>
            {recorded.length} column{recorded.length === 1 ? '' : 's'} filled here
          </summary>
          <div style={{ marginTop: 7 }}>
            {recorded.map((job) => (
              <div key={job.id} style={jobRow(job.status)}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
                  <code style={{ fontSize: 12, fontWeight: 700, color: '#111827' }}>
                    {job.destination_object}.{job.field}
                  </code>
                  <span style={modePill}>{MODE_LABELS[job.mode] ?? job.mode}</span>
                  {job.saved_to_mapping && (
                    <span style={savedPill}>also in the mapping</span>
                  )}
                </div>
                <p style={{ margin: '3px 0 0', fontSize: 11.5, color: '#4b5563' }}>
                  {job.summary}
                </p>
                {job.detail && job.status === 'failed' && (
                  <p style={{ margin: '2px 0 0', fontSize: 11, color: '#b91c1b' }}>
                    {job.detail}
                  </p>
                )}
              </div>
            ))}
          </div>
        </details>
      )}

      <ColumnFillDrawer
        sessionId={sessionId}
        sourceType={sourceType}
        sourceCreds={NO_CREDENTIALS}
        destCreds={NO_CREDENTIALS}
        sourceConnectionId={sourceConnId || undefined}
        destConnectionId={destConnId || undefined}
        open={open}
        onClose={() => setOpen(false)}
        onFilled={() => void load()}
      />
    </section>
  );
}

const MODE_LABELS: Record<string, string> = {
  constant: 'same value',
  value_map: 'matched from source',
  rules: 'conditions',
};

// The session's credentials are masked when read back and cannot be replayed, so
// the connection id is what the server resolves the describe calls through.
const NO_CREDENTIALS: Record<string, string> = {};

const panel: React.CSSProperties = {
  background: '#ffffff',
  border: '1px solid #e5e7eb',
  borderRadius: 10,
  padding: '11px 14px',
  marginTop: 14,
};

const rowLayout: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 14,
  flexWrap: 'wrap',
};

const intro: React.CSSProperties = {
  margin: '4px 0 0',
  fontSize: 12,
  color: '#6b7280',
  lineHeight: 1.55,
  maxWidth: 620,
};

const hint: React.CSSProperties = {
  margin: '8px 0 0',
  fontSize: 11.5,
  color: '#92400e',
  background: '#fffbeb',
  border: '1px solid #fde68a',
  borderRadius: 7,
  padding: '6px 9px',
  lineHeight: 1.5,
};

const button = (disabled: boolean): React.CSSProperties => ({
  fontSize: 12.5,
  fontWeight: 600,
  color: disabled ? '#9ca3af' : '#374151',
  background: disabled ? '#f9fafb' : '#ffffff',
  border: `1px solid ${disabled ? '#e5e7eb' : '#d1d5db'}`,
  borderRadius: 7,
  padding: '7px 14px',
  cursor: disabled ? 'not-allowed' : 'pointer',
  flex: '0 0 auto',
});

const summaryToggle: React.CSSProperties = {
  fontSize: 11.5,
  color: '#6b7280',
  cursor: 'pointer',
};

function jobRow(status: string): React.CSSProperties {
  return {
    border: '1px solid #f3f4f6',
    borderLeft: `3px solid ${status === 'completed' ? '#10b981' : status === 'failed' ? '#ef4444' : '#9ca3af'}`,
    borderRadius: 7,
    padding: '7px 9px',
    marginBottom: 6,
  };
}

const modePill: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  color: '#4b5563',
  background: '#f3f4f6',
  border: '1px solid #e5e7eb',
  borderRadius: 999,
  padding: '1px 6px',
};

const savedPill: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  color: '#3730a3',
  background: '#e0e7ff',
  borderRadius: 999,
  padding: '1px 6px',
};
