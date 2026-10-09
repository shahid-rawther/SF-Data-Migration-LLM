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
import { Disclosure } from '../components/Disclosure';
import { MoreMenu } from '../components/MoreMenu';
import { Sheet, SheetContent, SheetTitle } from '../components/ui/sheet';
import { useMigrationProgress } from '../hooks/useMigrationProgress';

export function MigrationDetail() {
  const { id } = useParams<{ id: string }>();
  const [session, setSession] = useState<MigrationSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<string>('pending');
  const [logs, setLogs] = useState<string[]>([]);
  const [statusOpen, setStatusOpen] = useState(false);
  const [resolutionsOpen, setResolutionsOpen] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  const [fillOpen, setFillOpen] = useState(false);
  const [fillUnavailable, setFillUnavailable] = useState<string | null>('Still reading this session');
  const [diagnosisCount, setDiagnosisCount] = useState(0);
  const [logOpenChoice, setLogOpen] = useState<boolean | null>(null);

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

  // The log is what there is to watch during a run, so it starts open then.
  const logOpen = logOpenChoice ?? status === 'running';

  const settled = ['completed', 'completed_with_errors', 'failed'].includes(status);
  useEffect(() => {
    if (!id) return;
    api.agents
      .diagnoses(id)
      .then((d) => setDiagnosisCount(d.length))
      .catch(() => setDiagnosisCount(0));
  }, [id, settled, askOpen]);

  if (loading) return <div style={pageStyle}><p>Loading…</p></div>;
  if (!session) return <div style={pageStyle}><p>Session not found</p></div>;

  const liveProgress = progress ?? session.live_progress;
  const counts = liveProgress ?? session.last_run;
  const countsFromLastRun = !liveProgress && !!session.last_run;
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
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button onClick={() => setAskOpen(true)} style={statusBtn}>
            Ask the agent
            {diagnosisCount > 0 && <span style={countPill}>{diagnosisCount}</span>}
          </button>
          <MoreMenu
            items={[
              {
                label: 'Migration status',
                description: 'Checkpoint and per-object progress in detail',
                onSelect: () => setStatusOpen(true),
              },
              {
                label: 'Resolutions',
                description: 'Fixes applied to this session’s mappings',
                onSelect: () => setResolutionsOpen(true),
              },
              {
                label: 'Fill a column',
                description: 'Set or correct one field on records already migrated',
                disabled: fillUnavailable !== null,
                title: fillUnavailable ?? undefined,
                onSelect: () => setFillOpen(true),
              },
            ]}
          />
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
      {countsFromLastRun && session.last_run?.saved_at && (
        <div style={{ fontSize: 12, color: '#737373', marginBottom: 6 }}>
          Last run, {new Date(session.last_run.saved_at).toLocaleString()} — see “Objects in this session” for totals across all runs
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginBottom: 16 }}>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #262626' }}>
          <div style={statLabel}>Records Fetched</div>
          <div style={statValue}>{(counts?.records_fetched ?? 0).toLocaleString()}</div>
        </div>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #171717' }}>
          <div style={statLabel}>Records Processed</div>
          <div style={{ ...statValue, color: '#171717' }}>{(counts?.records_processed ?? 0).toLocaleString()}</div>
        </div>
        <div style={{ ...statCardStyle, borderLeft: '4px solid #0a0a0a' }}>
          <div style={statLabel}>Records Failed</div>
          <div style={{ ...statValue, color: (counts?.records_failed ?? 0) > 0 ? '#0a0a0a' : '#404040' }}>
            {(counts?.records_failed ?? 0).toLocaleString()}
          </div>
        </div>
      </div>

      {status === 'running' && phase === 'preparation' && (
        <div style={{ ...cardStyle, background: '#fafafa', border: '1px solid #d4d4d4' }}>
          <div style={{ fontSize: 13, color: '#404040', fontWeight: 600, marginBottom: 4 }}>
            {liveProgress?.message || 'Preparing the run'}
          </div>
          <div style={{ fontSize: 13, color: '#525252' }}>
            The agent checks how the selected objects link to each other before any data is pulled.
            This can take a few minutes, and the counts stay at 0 until extraction starts.
          </div>
        </div>
      )}

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

      {/* Failures the automatic repairs could not fix wait in the side panel;
          this line is how anyone finds out they are there */}
      {diagnosisCount > 0 && (
        <div style={{ ...cardStyle, display: 'flex', alignItems: 'center', gap: 12, padding: '14px 20px' }}>
          <span style={{ flex: 1, fontSize: 13, color: '#262626' }}>
            <strong>
              {diagnosisCount} problem{diagnosisCount === 1 ? '' : 's'} the automatic repairs could not fix
            </strong>{' '}
            — the agent has explained {diagnosisCount === 1 ? 'it' : 'them'} and suggested fixes.
          </span>
          <button onClick={() => setAskOpen(true)} style={statusBtn}>Review fixes</button>
        </div>
      )}

      {/* Concerns the agent raised, including ones no fixed check looks for */}
      <AgentNoticesPanel sessionId={session.id} refreshKey={backfillRefreshKey} />

      <Sheet open={askOpen} onOpenChange={setAskOpen}>
        <SheetContent className="max-w-2xl overflow-y-auto p-4">
          <SheetTitle className="sr-only">Ask the agent</SheetTitle>
          <AgentAssistantPanel
            sessionId={session.id}
            status={status}
            refreshKey={backfillRefreshKey}
            onStarted={(s) => {
              setStatus(s);
              setLogs([]);
              setAskOpen(false);
            }}
          />
        </SheetContent>
      </Sheet>

      {/* References the load left empty, waiting on a decision to attach them,
          plus any from earlier runs that this one has just made fillable */}
      <BackfillPanel
        sessionId={session.id}
        refreshKey={backfillRefreshKey}
        unblocked={liveProgress?.unblocked_backfill ?? []}
      />

      {/* Putting a value into one column of an object that already migrated.
          Offered from the More menu; only past fills show here */}
      <ColumnFillPanel
        sessionId={session.id}
        sourceType={session.source_type}
        refreshKey={backfillRefreshKey}
        compact
        open={fillOpen}
        onClose={() => setFillOpen(false)}
        onAvailabilityChange={setFillUnavailable}
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

      <Disclosure
        label="Session details"
        hint={`ID ${session.id.slice(0, 8)} · ${session.config?.options?.disable_load ? 'dry run' : 'live run'}`}
        variant="boxed"
        style={{ marginBottom: 12, padding: '12px 20px' }}
      >
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
          <div style={{ gridColumn: '1 / -1', color: '#737373' }}><strong>ID:</strong> {session.id}</div>
        </div>
      </Disclosure>

      <Disclosure
        label="Live activity"
        count={logs.length}
        hint={logs.length ? logs[logs.length - 1] : 'messages appear here while a run is going'}
        open={logOpen}
        onOpenChange={setLogOpen}
        variant="boxed"
        style={{ marginBottom: 16, padding: '12px 20px' }}
      >
        <LogViewer lines={logs} />
      </Disclosure>
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
const countPill: React.CSSProperties = {
  marginLeft: 6, fontSize: 11, fontWeight: 700, color: '#fff', background: '#171717',
  borderRadius: 999, padding: '1px 7px',
};
const thStyle: React.CSSProperties = { padding: '8px 12px', fontSize: 12, color: '#737373', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' };
const tdStyle: React.CSSProperties = { padding: '10px 12px' };
