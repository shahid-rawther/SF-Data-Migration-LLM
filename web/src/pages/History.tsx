import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { MigrationSession } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import { SessionStatusPanel } from '../components/SessionStatusPanel';

export function History() {
  const [sessions, setSessions] = useState<MigrationSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFor, setStatusFor] = useState<string | null>(null);

  useEffect(() => {
    api.migrations.list().then(setSessions).finally(() => setLoading(false));
  }, []);

  const finished = sessions.filter((s) =>
    ['completed', 'failed', 'stopped'].includes(s.status),
  );

  return (
    <div style={pageStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <h1 style={h1}>Migration History</h1>
        <Link to="/" style={{ color: '#171717', textDecoration: 'none', fontSize: 13 }}>← Dashboard</Link>
      </div>

      {loading && <p style={{ color: '#737373' }}>Loading…</p>}
      {!loading && finished.length === 0 && (
        <p style={{ color: '#737373', textAlign: 'center', padding: '48px 0' }}>
          No completed migrations yet.
        </p>
      )}

      {finished.map((s) => (
        <div key={s.id} style={cardStyle}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>{s.source_type} → Salesforce</div>
              <div style={{ fontSize: 12, color: '#737373' }}>
                {s.selected_objects.length} objects &nbsp;·&nbsp;
                Started: {s.started_at ? new Date(s.started_at).toLocaleString() : '—'} &nbsp;·&nbsp;
                Ended: {s.completed_at ? new Date(s.completed_at).toLocaleString() : '—'}
              </div>
              <div style={{ fontSize: 11, color: '#a3a3a3', marginTop: 2 }}>ID: {s.id}</div>
            </div>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
              <StatusBadge status={s.status} />
              <button onClick={() => setStatusFor(s.id)} style={statusBtn}>
                Status
              </button>
              <Link
                to={`/migrations/${s.id}`}
                style={{ color: '#171717', fontSize: 13, textDecoration: 'none' }}
              >
                View details →
              </Link>
            </div>
          </div>

          <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {s.selected_objects.slice(0, 10).map((name) => (
              <span key={name} style={objectTag}>{name}</span>
            ))}
            {s.selected_objects.length > 10 && (
              <span style={{ ...objectTag, background: '#f5f5f5', color: '#737373' }}>
                +{s.selected_objects.length - 10} more
              </span>
            )}
          </div>
        </div>
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

const pageStyle: React.CSSProperties = { padding: '32px', width: '100%', maxWidth: 1600, margin: '0 auto' };
const h1: React.CSSProperties = { margin: 0, fontSize: 22, fontWeight: 700 };
const cardStyle: React.CSSProperties = {
  background: '#fff', border: '1px solid #e5e5e5', borderRadius: 10,
  padding: '16px 20px', marginBottom: 12, boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
};
const statusBtn: React.CSSProperties = {
  padding: '4px 10px', background: '#fff', border: '1px solid #d4d4d4',
  borderRadius: 6, fontSize: 12, fontWeight: 600, color: '#404040', cursor: 'pointer',
};
const objectTag: React.CSSProperties = {
  padding: '2px 8px', borderRadius: 20, fontSize: 11,
  background: '#f5f5f5', color: '#262626',
};
