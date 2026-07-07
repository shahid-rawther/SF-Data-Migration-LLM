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

      {/* Record counts */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginBottom: 16 }}>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #7c3aed' }}>
          <div style={statLabel}>Records Fetched</div>
          <div style={statValue}>{(liveProgress?.records_fetched ?? 0).toLocaleString()}</div>
        </div>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #16a34a' }}>
          <div style={statLabel}>Records Processed</div>
          <div style={{ ...statValue, color: '#16a34a' }}>{(liveProgress?.records_processed ?? 0).toLocaleString()}</div>
        </div>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #dc2626' }}>
          <div style={statLabel}>Records Failed</div>
          <div style={{ ...statValue, color: (liveProgress?.records_failed ?? 0) > 0 ? '#dc2626' : '#374151' }}>
            {(liveProgress?.records_failed ?? 0).toLocaleString()}
          </div>
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

      {/* Per-object breakdown */}
      {liveProgress?.records_by_object && Object.keys(liveProgress.records_by_object).length > 0 && (
        <div style={cardStyle}>
          <h2 style={h2}>Per-Object Breakdown</h2>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '2px solid #e5e7eb', textAlign: 'left' }}>
                <th style={thStyle}>Object</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Processed</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Failed</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(liveProgress.records_by_object)
                .sort(([, a], [, b]) => (b.processed + b.failed) - (a.processed + a.failed))
                .map(([name, counts]) => (
                  <tr key={name} style={{ borderBottom: '1px solid #f3f4f6' }}>
                    <td style={tdStyle}>{name}</td>
                    <td style={{ ...tdStyle, textAlign: 'right', color: '#16a34a', fontWeight: 600 }}>
                      {counts.processed.toLocaleString()}
                    </td>
                    <td style={{
                      ...tdStyle,
                      textAlign: 'right',
                      color: counts.failed > 0 ? '#dc2626' : '#374151',
                      fontWeight: counts.failed > 0 ? 600 : 400,
                    }}>
                      {counts.failed.toLocaleString()}
                    </td>
                  </tr>
                ))}
              <tr style={{ borderTop: '2px solid #e5e7eb', fontWeight: 700 }}>
                <td style={tdStyle}>Total</td>
                <td style={{ ...tdStyle, textAlign: 'right', color: '#16a34a' }}>
                  {Object.values(liveProgress.records_by_object)
                    .reduce((sum, c) => sum + c.processed, 0)
                    .toLocaleString()}
                </td>
                <td style={{
                  ...tdStyle,
                  textAlign: 'right',
                  color: Object.values(liveProgress.records_by_object).reduce((sum, c) => sum + c.failed, 0) > 0
                    ? '#dc2626' : '#374151',
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
        <div style={{ ...cardStyle, border: '1px solid #fecaca', background: '#fef2f2' }}>
          <h2 style={{ ...h2, color: '#991b1b' }}>Salesforce Load Errors</h2>
          {Object.entries(liveProgress.load_errors).map(([objName, messages]) => (
            <div key={objName} style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: '#7f1d1d', marginBottom: 6 }}>{objName}</div>
              <ul style={{ margin: 0, paddingLeft: 18, color: '#7f1d1d', fontSize: 12 }}>
                {messages.map((msg, idx) => (
                  <li key={`${objName}-${idx}`} style={{ marginBottom: 4 }}>{msg}</li>
                ))}
              </ul>
            </div>
          ))}
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
const statCardStyle: React.CSSProperties = {
  background: '#fff', border: '1px solid #e5e7eb', borderRadius: 10,
  padding: '16px 20px', boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
};
const statLabel: React.CSSProperties = { fontSize: 12, color: '#6b7280', fontWeight: 500, marginBottom: 4 };
const statValue: React.CSSProperties = { fontSize: 26, fontWeight: 700, color: '#374151' };
const thStyle: React.CSSProperties = { padding: '8px 12px', fontSize: 12, color: '#6b7280', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' };
const tdStyle: React.CSSProperties = { padding: '10px 12px' };
