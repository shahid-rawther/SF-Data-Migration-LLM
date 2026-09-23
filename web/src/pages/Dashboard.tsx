import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { BackfillUnit, MigrationSession } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import { ProgressBar } from '../components/ProgressBar';
import { SessionStatusPanel } from '../components/SessionStatusPanel';

export function Dashboard() {
  const [sessions, setSessions] = useState<MigrationSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFor, setStatusFor] = useState<string | null>(null);
  // Asked for across every session, not just the one being looked at: a
  // reference deferred while migrating one object usually becomes fillable
  // because of a *later* migration of the object it points at, and by then
  // nobody is looking at the earlier session's page.
  const [ready, setReady] = useState<BackfillUnit[]>([]);

  const load = () => {
    setLoading(true);
    api.migrations
      .list()
      .then(setSessions)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
    // Reading this re-checks each deferred reference against the id map, so the
    // counts reflect everything loaded so far. A failure here must not hide the
    // session list, so it only clears the reminder.
    api.migrations.backfill
      .list()
      .then((report) => setReady(report.ready))
      .catch(() => setReady([]));
  };

  useEffect(() => {
    load();
  }, []);

  const completed = sessions.filter((s) => s.status === 'completed').length;
  const running = sessions.filter((s) => s.status === 'running').length;
  const failed = sessions.filter((s) => s.status === 'failed').length;

  return (
    <div style={pageStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <div>
          <h1 style={h1}>ITSM Migration Dashboard</h1>
          <p style={{ color: '#6b7280', margin: 0 }}>
            Manage your ServiceNow → Salesforce migrations
          </p>
        </div>
        <Link to="/new" style={primaryBtn}>
          + New Migration
        </Link>
      </div>

      {/* Quick Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 16, marginBottom: 32 }}>
        {[
          { label: 'Total Sessions', value: sessions.length, color: '#2563eb' },
          { label: 'Running', value: running, color: '#d97706' },
          { label: 'Completed', value: completed, color: '#16a34a' },
          { label: 'Failed', value: failed, color: '#dc2626' },
        ].map((stat) => (
          <div key={stat.label} style={statCard}>
            <div style={{ fontSize: 28, fontWeight: 700, color: stat.color }}>{stat.value}</div>
            <div style={{ fontSize: 13, color: '#6b7280' }}>{stat.label}</div>
          </div>
        ))}
      </div>

      {ready.length > 0 && (
        <div style={reminderCard}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>
            {ready.length} reference{ready.length !== 1 ? 's' : ''} can now be attached
          </div>
          <p style={{ margin: '0 0 10px', fontSize: 13, color: '#78350f' }}>
            These were loaded empty because the object they point at had not been
            migrated yet. That object has since loaded, so they can be filled now.
            Nothing runs until you approve it.
          </p>
          {ready.map((unit) => (
            <div key={unit.id} style={reminderRow}>
              <span>
                <strong>
                  {unit.destination_object}.{unit.field}
                </strong>{' '}
                — {unit.rows_resolvable.toLocaleString()} of{' '}
                {unit.rows_deferred.toLocaleString()} record
                {unit.rows_deferred !== 1 ? 's' : ''}
              </span>
              <Link to={`/migrations/${unit.session_id}`} style={reminderLink}>
                Review
              </Link>
            </div>
          ))}
        </div>
      )}

      {/* Sessions list */}
      {loading && <p style={{ color: '#6b7280' }}>Loading…</p>}
      {error && <p style={{ color: '#dc2626' }}>Error: {error}</p>}
      {!loading && sessions.length === 0 && (
        <div style={{ textAlign: 'center', padding: '64px 0', color: '#6b7280' }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>📦</div>
          <p>No migrations yet. Create your first one!</p>
          <Link to="/new" style={primaryBtn}>+ New Migration</Link>
        </div>
      )}

      {sessions.map((s) => (
        <Link key={s.id} to={`/migrations/${s.id}`} style={{ textDecoration: 'none' }}>
          <div style={sessionCard}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{s.source_type} → Salesforce</div>
                <div style={{ fontSize: 12, color: '#6b7280' }}>
                  {s.selected_objects.length} object{s.selected_objects.length !== 1 ? 's' : ''} selected
                  &nbsp;·&nbsp;
                  {s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                <StatusBadge status={s.status} />
                <button
                  onClick={(e) => {
                    // The whole card is a link to the session; opening the panel
                    // must not navigate away from the list.
                    e.preventDefault();
                    e.stopPropagation();
                    setStatusFor(s.id);
                  }}
                  style={statusBtn}
                >
                  Status
                </button>
              </div>
            </div>
            {s.live_progress && (
              <ProgressBar percent={s.live_progress.overall_percent} label="Overall progress" />
            )}
            <div style={{ fontSize: 12, color: '#6b7280', marginTop: 4 }}>
              Phase: {s.phase ?? 'Not started'} &nbsp;·&nbsp; ID: {s.id.slice(0, 8)}…
            </div>
          </div>
        </Link>
      ))}

      {statusFor && (
        <SessionStatusPanel
          sessionId={statusFor}
          open
          onClose={() => setStatusFor(null)}
        />
      )}
    </div>
  );
}

const pageStyle: React.CSSProperties = { padding: '32px 32px' };
const h1: React.CSSProperties = { margin: '0 0 4px', fontSize: 24, fontWeight: 700 };
const primaryBtn: React.CSSProperties = {
  padding: '10px 20px',
  background: '#2563eb',
  color: '#fff',
  borderRadius: 8,
  textDecoration: 'none',
  fontWeight: 600,
  fontSize: 14,
};
const statCard: React.CSSProperties = {
  background: '#fff',
  border: '1px solid #e5e7eb',
  borderRadius: 10,
  padding: '16px 20px',
  boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
};
const reminderCard: React.CSSProperties = {
  background: '#fffbeb',
  border: '1px solid #fcd34d',
  borderRadius: 10,
  padding: '16px 20px',
  marginBottom: 24,
  color: '#92400e',
};
const reminderRow: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: 12,
  fontSize: 13,
  padding: '6px 0',
  borderTop: '1px solid #fde68a',
};
const reminderLink: React.CSSProperties = {
  padding: '4px 12px',
  background: '#b45309',
  color: '#fff',
  borderRadius: 6,
  textDecoration: 'none',
  fontWeight: 600,
  fontSize: 12,
  whiteSpace: 'nowrap',
};
const statusBtn: React.CSSProperties = {
  padding: '4px 10px',
  background: '#fff',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 12,
  fontWeight: 600,
  color: '#374151',
  cursor: 'pointer',
};
const sessionCard: React.CSSProperties = {
  background: '#fff',
  border: '1px solid #e5e7eb',
  borderRadius: 10,
  padding: '16px 20px',
  marginBottom: 12,
  boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
  cursor: 'pointer',
  transition: 'box-shadow 0.15s',
};
