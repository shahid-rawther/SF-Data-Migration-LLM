import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../api/client';
import type { MigrationSession } from '../api/client';
import { ProgressBar } from '../components/ProgressBar';
import { MigrationControls } from '../components/MigrationControls';
import { LogViewer } from '../components/LogViewer';
import { StatusBadge } from '../components/StatusBadge';
import { useMigrationProgress } from '../hooks/useMigrationProgress';

export function MigrationDetail() {
  const { id } = useParams<{ id: string }>();
  const [session, setSession] = useState<MigrationSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<string>('pending');
  const [logs, setLogs] = useState<string[]>([]);

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

  // Append progress messages to log
  useEffect(() => {
    if (progress?.message) {
      setLogs((prev) => [...prev, `[${progress.phase}] ${progress.message}`]);
    }
    if (progress?.status && progress.status !== status) {
      setStatus(progress.status);
    }
  }, [progress]);

  if (loading) return <div style={pageStyle}><p>Loading…</p></div>;
  if (!session) return <div style={pageStyle}><p>Session not found</p></div>;

  const liveProgress = progress ?? session.live_progress;
  const overallPercent = liveProgress?.overall_percent ?? 0;
  const phase = liveProgress?.phase ?? session.phase ?? '';

  const phasePercents = {
    extraction: phase === 'extraction' ? (liveProgress?.overall_percent ?? 0) * 3 : phase === 'transformation' || phase === 'loading' ? 100 : 0,
    transformation: phase === 'transformation' ? (liveProgress?.overall_percent ?? 0) * 3 - 100 : phase === 'loading' ? 100 : 0,
    loading: phase === 'loading' ? (liveProgress?.overall_percent ?? 0) * 3 - 200 : 0,
  };

  return (
    <div style={pageStyle}>
      <div style={{ marginBottom: 20 }}>
        <Link to="/" style={{ color: '#2563eb', textDecoration: 'none', fontSize: 13 }}>← Dashboard</Link>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 24 }}>
        <div>
          <h1 style={h1}>{session.source_type} → Salesforce</h1>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <StatusBadge status={status} />
            {phase && <span style={{ fontSize: 13, color: '#6b7280' }}>Phase: <strong>{phase}</strong></span>}
            <span style={{ fontSize: 12, color: '#9ca3af' }}>ID: {session.id}</span>
          </div>
        </div>
        <MigrationControls sessionId={session.id} status={status} onStatusChange={setStatus} />
      </div>

      {/* Overall progress */}
      <div style={cardStyle}>
        <h2 style={h2}>Overall Progress</h2>
        <ProgressBar
          percent={overallPercent}
          label="Overall"
          color={status === 'failed' ? '#dc2626' : status === 'completed' ? '#16a34a' : '#2563eb'}
          height={18}
        />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginTop: 16 }}>
          <ProgressBar percent={Math.min(100, Math.max(0, phasePercents.extraction))} label="Extraction" color="#7c3aed" />
          <ProgressBar percent={Math.min(100, Math.max(0, phasePercents.transformation))} label="Transformation" color="#d97706" />
          <ProgressBar percent={Math.min(100, Math.max(0, phasePercents.loading))} label="Loading" color="#16a34a" />
        </div>
      </div>

      {/* Current object */}
      {liveProgress && liveProgress.object && (
        <div style={{ ...cardStyle, background: '#f0f9ff', border: '1px solid #bae6fd' }}>
          <div style={{ fontSize: 13, color: '#0369a1', fontWeight: 600, marginBottom: 4 }}>
            Currently processing: {liveProgress.object}
          </div>
          <div style={{ fontSize: 13, color: '#0284c7' }}>
            Batch {liveProgress.batch_current} / {liveProgress.batch_total}
            &nbsp;·&nbsp;
            {liveProgress.records_processed.toLocaleString()} processed
            &nbsp;·&nbsp;
            <span style={{ color: liveProgress.records_failed > 0 ? '#dc2626' : undefined }}>
              {liveProgress.records_failed.toLocaleString()} failed
            </span>
          </div>
          {liveProgress.message && (
            <div style={{ fontSize: 12, color: '#0369a1', marginTop: 4 }}>{liveProgress.message}</div>
          )}
        </div>
      )}

      {/* Selected objects */}
      <div style={cardStyle}>
        <h2 style={h2}>Selected Objects ({session.selected_objects.length})</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {session.selected_objects.map((name) => {
            const isCurrent = liveProgress?.object === name;
            return (
              <span
                key={name}
                style={{
                  padding: '3px 10px',
                  borderRadius: 20,
                  fontSize: 12,
                  background: isCurrent ? '#dbeafe' : '#f3f4f6',
                  color: isCurrent ? '#1e40af' : '#374151',
                  border: isCurrent ? '1px solid #93c5fd' : '1px solid transparent',
                  fontWeight: isCurrent ? 600 : 400,
                }}
              >
                {name}
              </span>
            );
          })}
        </div>
      </div>

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

const pageStyle: React.CSSProperties = { maxWidth: 900, margin: '0 auto', padding: '32px 24px' };
const h1: React.CSSProperties = { margin: '0 0 8px', fontSize: 22, fontWeight: 700 };
const h2: React.CSSProperties = { margin: '0 0 16px', fontSize: 16, fontWeight: 700 };
const cardStyle: React.CSSProperties = {
  background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12,
  padding: '20px 24px', marginBottom: 16, boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
};
