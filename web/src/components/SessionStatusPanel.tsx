import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { SessionObjectStatus, SessionStatusReport } from '../api/client';

interface SessionStatusPanelProps {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}

/** How often a running session's numbers are re-read while the panel is open. */
const LIVE_POLL_MS = 4000;

export function SessionStatusPanel({ sessionId, open, onClose }: SessionStatusPanelProps) {
  const [report, setReport] = useState<SessionStatusReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (showSpinner: boolean) => {
      if (showSpinner) setLoading(true);
      api.migrations
        .status(sessionId)
        .then((next) => {
          setReport(next);
          setError(null);
        })
        .catch((e: Error) => setError(e.message))
        .finally(() => setLoading(false));
    },
    [sessionId],
  );

  useEffect(() => {
    if (!open) return;
    load(true);
  }, [open, load]);

  // A running session moves while the panel is open, so its counts are re-read.
  // A finished one never changes, and is left alone.
  const isRunning = report ? ['running', 'paused'].includes(report.status) : false;
  useEffect(() => {
    if (!open || !isRunning) return;
    const timer = setInterval(() => load(false), LIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [open, isRunning, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const totals = report?.totals;

  return (
    <>
      <div style={backdrop} onClick={onClose} />
      <aside style={drawer} role="complementary" aria-label="Migration status">
        <header style={header}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700 }}>Migration status</div>
            <div style={{ fontSize: 12, color: '#6b7280', marginTop: 2 }}>
              {report ? `${report.source_type} → Salesforce` : sessionId.slice(0, 8)}
              {report?.phase ? ` · ${report.phase}` : ''}
            </div>
          </div>
          <button onClick={onClose} style={closeBtn} aria-label="Close">
            ✕
          </button>
        </header>

        <div style={body}>
          {loading && !report && <p style={muted}>Loading…</p>}
          {error && <p style={{ color: '#dc2626', fontSize: 13 }}>{error}</p>}

          {report && totals && (
            <>
              <section style={section}>
                <div style={rowBetween}>
                  <span style={sectionTitle}>Overall</span>
                  <span style={{ fontSize: 12, color: '#6b7280' }}>
                    {totals.objects_completed} / {totals.objects} objects
                  </span>
                </div>
                <RowBar
                  loaded={totals.loaded}
                  total={totals.source_rows}
                  percent={totals.percent}
                  color={totals.failed > 0 ? '#d97706' : '#16a34a'}
                />
                <div style={{ fontSize: 12, color: '#6b7280', marginTop: 6 }}>
                  {totals.loaded.toLocaleString()} of {totals.source_rows.toLocaleString()}{' '}
                  source records loaded
                  {totals.failed > 0 && (
                    <span style={{ color: '#dc2626' }}>
                      {' '}
                      · {totals.failed.toLocaleString()} failed
                    </span>
                  )}
                </div>
                {report.dry_run && (
                  <div style={noteBox}>
                    Dry run — nothing was written to Salesforce, so no records are
                    counted as loaded.
                  </div>
                )}
              </section>

              <section style={section}>
                <div style={sectionTitle}>Objects</div>
                {report.objects.length === 0 && (
                  <p style={muted}>This session has no objects selected.</p>
                )}
                {report.objects.map((entry) => (
                  <ObjectRow key={entry.object} entry={entry} />
                ))}
              </section>

              {report.checkpoint && (
                <section style={section}>
                  <div style={sectionTitle}>Resume point</div>
                  <dl style={metaList}>
                    <Meta
                      label="Saved"
                      value={
                        report.checkpoint.saved_at
                          ? new Date(report.checkpoint.saved_at).toLocaleString()
                          : '—'
                      }
                    />
                    <Meta
                      label="Checkpoint"
                      value={`#${report.checkpoint.sequence}${
                        report.checkpoint.phase ? ` · ${report.checkpoint.phase}` : ''
                      }`}
                    />
                    <Meta
                      label="Ids recorded"
                      value={report.checkpoint.id_mapping_count.toLocaleString()}
                    />
                    <Meta
                      label="Local extract"
                      value={
                        report.checkpoint.stage_available
                          ? 'On disk — resume skips extraction'
                          : 'Cleared — resume re-extracts'
                      }
                    />
                  </dl>
                  {report.checkpoint.credentials_required && (
                    <div style={noteBox}>
                      Credentials are never checkpointed. Re-select the source and
                      destination connections before resuming this session.
                    </div>
                  )}
                  {report.checkpoint.last_error && (
                    <div style={{ ...noteBox, background: '#fef2f2', color: '#991b1b' }}>
                      Last save failed: {report.checkpoint.last_error}
                    </div>
                  )}
                </section>
              )}

              <div style={{ fontSize: 11, color: '#9ca3af' }}>
                Loaded counts come from the record of migrated ids, so they survive
                a restart and are what a resume skips.
              </div>
            </>
          )}
        </div>
      </aside>
    </>
  );
}

function ObjectRow({ entry }: { entry: SessionObjectStatus }) {
  const color =
    entry.failed > 0
      ? '#dc2626'
      : entry.state === 'completed'
        ? '#16a34a'
        : entry.current
          ? '#2563eb'
          : '#9ca3af';

  return (
    <div style={{ ...objectCard, borderLeft: `3px solid ${color}` }}>
      <div style={rowBetween}>
        <div style={{ fontSize: 13, fontWeight: 600, wordBreak: 'break-word' }}>
          {entry.source_table || entry.object}
          <span style={{ color: '#9ca3af', fontWeight: 400 }}> → </span>
          {entry.destination_object || '—'}
        </div>
        <StateTag entry={entry} />
      </div>

      <div style={{ fontSize: 11, color: '#9ca3af', margin: '2px 0 8px' }}>
        {entry.object}
        {entry.source_tables.length > 1 &&
          ` · joins ${entry.source_tables.slice(1).join(', ')}`}
      </div>

      {entry.state === 'unmapped' ? (
        <div style={{ fontSize: 12, color: '#b45309' }}>
          No mapping config found, so nothing can be counted for this object.
        </div>
      ) : (
        <>
          <RowBar
            loaded={entry.loaded}
            total={entry.source_rows}
            percent={entry.percent}
            color={color}
          />
          <div style={{ fontSize: 12, color: '#374151', marginTop: 6 }}>
            <strong>{entry.loaded.toLocaleString()}</strong> of{' '}
            {entry.source_rows > 0 ? entry.source_rows.toLocaleString() : '?'} rows
            {entry.failed > 0 && (
              <span style={{ color: '#dc2626' }}>
                {' '}
                · {entry.failed.toLocaleString()} failed
              </span>
            )}
          </div>
          {entry.source_rows === 0 && (
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>
              Source total unknown — nothing is staged on disk for this table.
            </div>
          )}
          {entry.source_rows_origin === 'checkpoint' && (
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>
              Source total as recorded at the last checkpoint.
            </div>
          )}
          {entry.errors.length > 0 && (
            <ul style={errorList}>
              {entry.errors.slice(0, 3).map((message, idx) => (
                <li key={idx}>{message}</li>
              ))}
              {entry.errors.length > 3 && <li>+{entry.errors.length - 3} more</li>}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

function StateTag({ entry }: { entry: SessionObjectStatus }) {
  const label = entry.current
    ? 'running'
    : entry.state === 'in_progress'
      ? 'partial'
      : entry.state;
  const palette: Record<string, { bg: string; fg: string }> = {
    completed: { bg: '#dcfce7', fg: '#166534' },
    running: { bg: '#dbeafe', fg: '#1e40af' },
    partial: { bg: '#fef3c7', fg: '#92400e' },
    pending: { bg: '#f3f4f6', fg: '#6b7280' },
    unmapped: { bg: '#fee2e2', fg: '#991b1b' },
  };
  const tone = palette[label] ?? palette.pending;
  return (
    <span
      style={{
        padding: '2px 8px',
        borderRadius: 20,
        fontSize: 11,
        fontWeight: 600,
        whiteSpace: 'nowrap',
        background: tone.bg,
        color: tone.fg,
      }}
    >
      {label}
    </span>
  );
}

function RowBar({
  loaded,
  total,
  percent,
  color,
}: {
  loaded: number;
  total: number;
  percent: number;
  color: string;
}) {
  // With no known source total the bar cannot be a fraction of anything, so it
  // stays empty rather than implying progress that has not been measured.
  const width = total > 0 ? Math.min(100, Math.max(0, percent)) : loaded > 0 ? 100 : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div style={barTrack}>
        <div
          style={{
            width: `${width}%`,
            height: '100%',
            background: color,
            borderRadius: 10,
            transition: 'width 0.4s ease',
          }}
        />
      </div>
      <span style={{ fontSize: 11, color: '#6b7280', minWidth: 38, textAlign: 'right' }}>
        {total > 0 ? `${width.toFixed(0)}%` : '—'}
      </span>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt style={{ fontSize: 12, color: '#6b7280' }}>{label}</dt>
      <dd style={{ fontSize: 12, margin: 0, color: '#374151' }}>{value}</dd>
    </>
  );
}

const backdrop: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(15, 23, 42, 0.35)',
  zIndex: 40,
};
const drawer: React.CSSProperties = {
  position: 'fixed',
  top: 0,
  right: 0,
  bottom: 0,
  width: 'min(440px, 100vw)',
  background: '#fff',
  borderLeft: '1px solid #e5e7eb',
  boxShadow: '-8px 0 24px rgba(15, 23, 42, 0.12)',
  zIndex: 41,
  display: 'flex',
  flexDirection: 'column',
};
const header: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'flex-start',
  gap: 12,
  padding: '16px 20px',
  borderBottom: '1px solid #e5e7eb',
};
const closeBtn: React.CSSProperties = {
  border: '1px solid #e5e7eb',
  background: '#fff',
  borderRadius: 8,
  width: 28,
  height: 28,
  cursor: 'pointer',
  color: '#6b7280',
  lineHeight: 1,
};
const body: React.CSSProperties = {
  padding: '16px 20px 32px',
  overflowY: 'auto',
  flex: 1,
};
const section: React.CSSProperties = { marginBottom: 24 };
const sectionTitle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: '#6b7280',
  marginBottom: 8,
  display: 'block',
};
const rowBetween: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'baseline',
  gap: 8,
};
const objectCard: React.CSSProperties = {
  background: '#f9fafb',
  border: '1px solid #e5e7eb',
  borderRadius: 8,
  padding: '10px 12px',
  marginBottom: 10,
};
const barTrack: React.CSSProperties = {
  flex: 1,
  height: 8,
  background: '#e5e7eb',
  borderRadius: 10,
  overflow: 'hidden',
};
const metaList: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'auto 1fr',
  columnGap: 12,
  rowGap: 4,
  margin: 0,
};
const muted: React.CSSProperties = { color: '#6b7280', fontSize: 13 };
const noteBox: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 10px',
  borderRadius: 8,
  background: '#f0f9ff',
  color: '#0369a1',
  fontSize: 12,
};
const errorList: React.CSSProperties = {
  margin: '8px 0 0',
  paddingLeft: 16,
  fontSize: 11,
  color: '#991b1b',
};
