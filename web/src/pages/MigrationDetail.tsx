import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../api/client';
import type { MigrationSession } from '../api/client';
import { ProgressBar } from '../components/ProgressBar';
import { MigrationControls } from '../components/MigrationControls';
import { LogViewer } from '../components/LogViewer';
import { StatusBadge } from '../components/StatusBadge';
import { SessionStatusPanel } from '../components/SessionStatusPanel';
import { ResolutionsPanel } from '../components/ResolutionsPanel';
import { BackfillPanel } from '../components/BackfillPanel';
import { ColumnFillPanel } from '../components/ColumnFillPanel';
import { AgentNoticesPanel } from '../components/AgentNoticesPanel';
import { AgentAssistantPanel } from '../components/AgentAssistantPanel';
import { SessionObjectsPanel } from '../components/SessionObjectsPanel';
import { useMigrationProgress } from '../hooks/useMigrationProgress';

export function MigrationDetail() {
  const { id } = useParams<{ id: string }>();
  const [session, setSession] = useState<MigrationSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<string>('pending');
  const [logs, setLogs] = useState<string[]>([]);
  const [statusOpen, setStatusOpen] = useState(false);
  const [resolutionsOpen, setResolutionsOpen] = useState(false);

  const { progress } = useMigrationProgress(
    session && ['running', 'paused'].includes(status) ? id ?? null : null,
  );

  useEffect(() => {
    if (!id) return;
    api.migrations.get(id).then((s) => {
      setSession(s);
      setStatus(s.status);
      setLoading(false);
    });
  }, [id]);

  // Append each incoming progress message once
  useEffect(() => {
    if (progress?.message) {
      setLogs((prev) => [...prev, `[${progress.phase}] ${progress.message}`]);
    }
  }, [progress]);

  // Keep local status synced with backend progress state
  useEffect(() => {
    if (progress?.status && progress.status !== status) {
      setStatus(progress.status);
    }
  }, [progress?.status, status]);

  if (loading) return <div style={pageStyle}><p>Loading…</p></div>;
  if (!session) return <div style={pageStyle}><p>Session not found</p></div>;

  const liveProgress = progress ?? session.live_progress;
  const overallPercent = liveProgress?.overall_percent ?? 0;
  const phase = liveProgress?.phase ?? session.phase ?? '';

  // What was deferred only becomes knowable once the load has finished, so the
  // panel is re-read when the run settles or when the run itself reports some.
  const backfillRefreshKey =
    (['completed', 'completed_with_errors', 'failed'].includes(status) ? 1 : 0) +
    (liveProgress?.pending_backfill?.length ?? 0);

  // What has loaded — and so what still needs running — only changes when a run
  // settles, so the objects panel is rebuilt on each status transition rather
  // than polled.
  const objectsRefreshKey = status;

  const phasePercents = {
    extraction: phase === 'extraction' ? (liveProgress?.overall_percent ?? 0) * 3 : phase === 'transformation' || phase === 'loading' ? 100 : 0,
    transformation: phase === 'transformation' ? (liveProgress?.overall_percent ?? 0) * 3 - 100 : phase === 'loading' ? 100 : 0,
    loading: phase === 'loading' ? (liveProgress?.overall_percent ?? 0) * 3 - 200 : 0,
  };

  return (
    <div style={pageStyle}>
      <div style={{ marginBottom: 20 }}>
        <Link to="/" style={{ color: '#171717', textDecoration: 'none', fontSize: 13 }}>← Dashboard</Link>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 24 }}>
        <div>
          <h1 style={h1}>{session.source_type} → Salesforce</h1>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <StatusBadge status={status} />
            {phase && <span style={{ fontSize: 13, color: '#737373' }}>Phase: <strong>{phase}</strong></span>}
            <span style={{ fontSize: 12, color: '#a3a3a3' }}>ID: {session.id}</span>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button onClick={() => setStatusOpen(true)} style={statusBtn}>
            Migration status
          </button>
          <button onClick={() => setResolutionsOpen(true)} style={statusBtn}>
            Resolutions
          </button>
          <MigrationControls sessionId={session.id} status={status} onStatusChange={(s) => {
            setStatus(s);
            if (s === 'running') setLogs([]);
          }} />
        </div>
      </div>

      <SessionStatusPanel
        sessionId={session.id}
        open={statusOpen}
        onClose={() => setStatusOpen(false)}
      />

      <ResolutionsPanel
        sessionId={session.id}
        open={resolutionsOpen}
        onClose={() => setResolutionsOpen(false)}
      />

      {/* Overall progress */}
      <div style={cardStyle}>
        <h2 style={h2}>Overall Progress</h2>
        <ProgressBar
          percent={overallPercent}
          label="Overall"
          color={status === 'failed' ? '#0a0a0a' : status === 'completed' ? '#171717' : '#171717'}
          height={18}
        />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginTop: 16 }}>
          <ProgressBar percent={Math.min(100, Math.max(0, phasePercents.extraction))} label="Extraction" color="#262626" />
          <ProgressBar percent={Math.min(100, Math.max(0, phasePercents.transformation))} label="Transformation" color="#525252" />
          <ProgressBar percent={Math.min(100, Math.max(0, phasePercents.loading))} label="Loading" color="#171717" />
        </div>
      </div>

      {/* Record counts */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginBottom: 16 }}>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #262626' }}>
          <div style={statLabel}>Records Fetched</div>
          <div style={statValue}>{(liveProgress?.records_fetched ?? 0).toLocaleString()}</div>
        </div>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #171717' }}>
          <div style={statLabel}>Records Processed</div>
          <div style={{ ...statValue, color: '#171717' }}>{(liveProgress?.records_processed ?? 0).toLocaleString()}</div>
        </div>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #0a0a0a' }}>
          <div style={statLabel}>Records Failed</div>
          <div style={{ ...statValue, color: (liveProgress?.records_failed ?? 0) > 0 ? '#0a0a0a' : '#404040' }}>
            {(liveProgress?.records_failed ?? 0).toLocaleString()}
          </div>
        </div>
      </div>

      {/* Current object */}
      {liveProgress && liveProgress.object && (
        <div style={{ ...cardStyle, background: '#fafafa', border: '1px solid #d4d4d4' }}>
          <div style={{ fontSize: 13, color: '#404040', fontWeight: 600, marginBottom: 4 }}>
            Currently processing: {liveProgress.object}
          </div>
          <div style={{ fontSize: 13, color: '#525252' }}>
            Batch {liveProgress.batch_current} / {liveProgress.batch_total}
            &nbsp;·&nbsp;
            {liveProgress.records_processed.toLocaleString()} processed
            &nbsp;·&nbsp;
            <span style={{ color: liveProgress.records_failed > 0 ? '#0a0a0a' : undefined }}>
              {liveProgress.records_failed.toLocaleString()} failed
            </span>
          </div>
          {liveProgress.message && (
            <div style={{ fontSize: 12, color: '#404040', marginTop: 4 }}>{liveProgress.message}</div>
          )}
        </div>
      )}

      {/* Concerns the agent raised, including ones no fixed check looks for */}
      <AgentNoticesPanel sessionId={session.id} refreshKey={backfillRefreshKey} />

      {/* Failures the automatic repairs could not fix: explained, discussed,
          and fixed only on approval */}
      <AgentAssistantPanel
        sessionId={session.id}
        status={status}
        refreshKey={backfillRefreshKey}
        onStarted={(s) => {
          setStatus(s);
          setLogs([]);
        }}
      />

      {/* References the load left empty, waiting on a decision to attach them,
          plus any from earlier runs that this one has just made fillable */}
      <BackfillPanel
        sessionId={session.id}
        refreshKey={backfillRefreshKey}
        unblocked={liveProgress?.unblocked_backfill ?? []}
      />

      {/* Putting a value into one column of an object that already migrated.
          Not something owed by the run, so it sits quietly rather than asking */}
      <ColumnFillPanel
        sessionId={session.id}
        sourceType={session.source_type}
        refreshKey={backfillRefreshKey}
      />

      {/* Per-object breakdown */}
      {liveProgress?.records_by_object && Object.keys(liveProgress.records_by_object).length > 0 && (
        <div style={cardStyle}>
          <h2 style={h2}>Per-Object Breakdown</h2>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '2px solid #e5e5e5', textAlign: 'left' }}>
                <th style={thStyle}>Object</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Processed</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Failed</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(liveProgress.records_by_object)
                .sort(([, a], [, b]) => (b.processed + b.failed) - (a.processed + a.failed))
                .map(([name, counts]) => (
                  <tr key={name} style={{ borderBottom: '1px solid #f5f5f5' }}>
                    <td style={tdStyle}>{name}</td>
                    <td style={{ ...tdStyle, textAlign: 'right', color: '#171717', fontWeight: 600 }}>
                      {counts.processed.toLocaleString()}
                    </td>
                    <td style={{
                      ...tdStyle,
                      textAlign: 'right',
                      color: counts.failed > 0 ? '#0a0a0a' : '#404040',
                      fontWeight: counts.failed > 0 ? 600 : 400,
                    }}>
                      {counts.failed.toLocaleString()}
                    </td>
                  </tr>
                ))}
              <tr style={{ borderTop: '2px solid #e5e5e5', fontWeight: 700 }}>
                <td style={tdStyle}>Total</td>
                <td style={{ ...tdStyle, textAlign: 'right', color: '#171717' }}>
                  {Object.values(liveProgress.records_by_object)
                    .reduce((sum, c) => sum + c.processed, 0)
                    .toLocaleString()}
                </td>
                <td style={{
                  ...tdStyle,
                  textAlign: 'right',
                  color: Object.values(liveProgress.records_by_object).reduce((sum, c) => sum + c.failed, 0) > 0
                    ? '#0a0a0a' : '#404040',
                }}>
                  {Object.values(liveProgress.records_by_object)
                    .reduce((sum, c) => sum + c.failed, 0)
                    .toLocaleString()}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {/* Salesforce loading errors */}
      {liveProgress?.load_errors && Object.keys(liveProgress.load_errors).length > 0 && (
        <div style={{ ...cardStyle, border: '1px solid #e5e5e5', background: '#fafafa' }}>
          <h2 style={{ ...h2, color: '#171717' }}>Salesforce Load Errors</h2>
          {Object.entries(liveProgress.load_errors).map(([objName, messages]) => (
            <div key={objName} style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: '#0a0a0a', marginBottom: 6 }}>{objName}</div>
              <ul style={{ margin: 0, paddingLeft: 18, color: '#0a0a0a', fontSize: 12 }}>
                {messages.map((msg, idx) => (
                  <li key={`${objName}-${idx}`} style={{ marginBottom: 4 }}>{msg}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {/* The session's objects, where mappings are edited and more are added */}
      <SessionObjectsPanel
        key={objectsRefreshKey}
        sessionId={session.id}
        sourceType={session.source_type}
        status={status}
        onStarted={(s) => {
          setStatus(s);
          setLogs([]);
        }}
      />

      {/* Migration info */}
      <div style={cardStyle}>
        <h2 style={h2}>Session Info</h2>
        <div style={{ fontSize: 13, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <div><strong>Source:</strong> {session.source_type}</div>
          <div><strong>Destination:</strong> Salesforce</div>
          <div><strong>Started:</strong> {session.started_at ? new Date(session.started_at).toLocaleString() : 'Not started'}</div>
          <div><strong>Last updated:</strong> {new Date(session.updated_at).toLocaleString()}</div>
          {session.completed_at && (
            <div><strong>Completed:</strong> {new Date(session.completed_at).toLocaleString()}</div>
          )}
          <div>
            <strong>Dry run:</strong>{' '}
            {session.config?.options?.disable_load ? 'Yes' : 'No'}
          </div>
        </div>
      </div>

      {/* Live log */}
      <div style={cardStyle}>
        <h2 style={h2}>Live Activity</h2>
        <LogViewer lines={logs} />
      </div>
    </div>
  );
}

const pageStyle: React.CSSProperties = { padding: '32px', width: '100%', maxWidth: 1600, margin: '0 auto' };
const h1: React.CSSProperties = { margin: '0 0 8px', fontSize: 22, fontWeight: 700 };
const h2: React.CSSProperties = { margin: '0 0 16px', fontSize: 16, fontWeight: 700 };
const cardStyle: React.CSSProperties = {
  background: '#fff', border: '1px solid #e5e5e5', borderRadius: 12,
  padding: '20px 24px', marginBottom: 16, boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
};
const statCardStyle: React.CSSProperties = {
  background: '#fff', border: '1px solid #e5e5e5', borderRadius: 10,
  padding: '16px 20px', boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
};
const statLabel: React.CSSProperties = { fontSize: 12, color: '#737373', fontWeight: 500, marginBottom: 4 };
const statValue: React.CSSProperties = { fontSize: 26, fontWeight: 700, color: '#404040' };
const statusBtn: React.CSSProperties = {
  padding: '8px 14px', background: '#fff', border: '1px solid #d4d4d4',
  borderRadius: 8, fontSize: 13, fontWeight: 600, color: '#404040', cursor: 'pointer',
};
const thStyle: React.CSSProperties = { padding: '8px 12px', fontSize: 12, color: '#737373', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' };
const tdStyle: React.CSSProperties = { padding: '10px 12px' };
