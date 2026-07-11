import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { MigrationSession } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import { ProgressBar } from '../components/ProgressBar';

export function Dashboard() {
  const [sessions, setSessions] = useState<MigrationSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api.migrations
      .list()
      .then(setSessions)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
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
              <StatusBadge status={s.status} />
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
    </div>
  );
}

const pageStyle: React.CSSProperties = { maxWidth: 900, margin: '0 auto', padding: '32px 24px' };
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
