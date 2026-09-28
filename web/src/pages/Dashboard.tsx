import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Activity, AlertTriangle, CheckCircle2, Database, Gauge, Plus } from 'lucide-react';
import { api } from '../api/client';
import type { BackfillUnit, MigrationSession } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import { ProgressBar } from '../components/ProgressBar';
import { SessionStatusPanel } from '../components/SessionStatusPanel';
import { Button } from '../components/ui/button';
import { Card, CardContent } from '../components/ui/card';

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
    <div className="page-container">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">ITSM Migration Dashboard</h1>
          <p className="page-description mt-1">
            Manage your ServiceNow → Salesforce migrations
          </p>
        </div>
        <Button asChild>
          <Link to="/new"><Plus /> New Migration</Link>
        </Button>
      </div>

      {/* Quick Stats */}
      <div className="mb-8 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-4">
        {[
          { label: 'Total Sessions', value: sessions.length, icon: Database, tone: 'bg-muted text-foreground' },
          { label: 'Running', value: running, icon: Activity, tone: 'bg-muted text-foreground' },
          { label: 'Completed', value: completed, icon: CheckCircle2, tone: 'bg-foreground text-background' },
          { label: 'Failed', value: failed, icon: AlertTriangle, tone: 'bg-muted text-muted-foreground' },
        ].map((stat) => (
          <Card key={stat.label}>
            <CardContent className="flex items-center justify-between p-5">
              <div>
                <div className="text-2xl font-semibold tabular-nums">{stat.value}</div>
                <div className="mt-1 text-xs text-muted-foreground">{stat.label}</div>
              </div>
              <div className={`flex size-10 items-center justify-center rounded-lg ${stat.tone}`}>
                <stat.icon className="size-5" />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {ready.length > 0 && (
        <div style={reminderCard}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>
            {ready.length} reference{ready.length !== 1 ? 's' : ''} can now be attached
          </div>
          <p style={{ margin: '0 0 10px', fontSize: 13, color: '#404040' }}>
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
      {loading && <p style={{ color: '#737373' }}>Loading…</p>}
      {error && <p style={{ color: '#0a0a0a' }}>Error: {error}</p>}
      {!loading && sessions.length === 0 && (
        <div style={{ textAlign: 'center', padding: '64px 0', color: '#737373' }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>📦</div>
          <p>No migrations yet. Create your first one!</p>
          <Button asChild><Link to="/new"><Plus /> New Migration</Link></Button>
        </div>
      )}

      {sessions.map((s) => (
        <Link key={s.id} to={`/migrations/${s.id}`} style={{ textDecoration: 'none' }}>
          <Card className="mb-3 transition-shadow hover:shadow-md">
            <CardContent className="p-5">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{s.source_type} → Salesforce</div>
                <div style={{ fontSize: 12, color: '#737373' }}>
                  {s.selected_objects.length} object{s.selected_objects.length !== 1 ? 's' : ''} selected
                  &nbsp;·&nbsp;
                  {s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                <StatusBadge status={s.status} />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={(e) => {
                    // The whole card is a link to the session; opening the panel
                    // must not navigate away from the list.
                    e.preventDefault();
                    e.stopPropagation();
                    setStatusFor(s.id);
                  }}
                >
                  <Gauge /> Status
                </Button>
              </div>
            </div>
            {s.live_progress && (
              <ProgressBar percent={s.live_progress.overall_percent} label="Overall progress" />
            )}
            <div style={{ fontSize: 12, color: '#737373', marginTop: 4 }}>
              Phase: {s.phase ?? 'Not started'} &nbsp;·&nbsp; ID: {s.id.slice(0, 8)}…
            </div>
            </CardContent>
          </Card>
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

const reminderCard: React.CSSProperties = {
  background: '#fafafa',
  border: '1px solid #d4d4d4',
  borderRadius: 10,
  padding: '16px 20px',
  marginBottom: 24,
  color: '#404040',
};
const reminderRow: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: 12,
  fontSize: 13,
  padding: '6px 0',
  borderTop: '1px solid #e5e5e5',
};
const reminderLink: React.CSSProperties = {
  padding: '4px 12px',
  background: '#404040',
  color: '#fff',
  borderRadius: 6,
  textDecoration: 'none',
  fontWeight: 600,
  fontSize: 12,
  whiteSpace: 'nowrap',
};
