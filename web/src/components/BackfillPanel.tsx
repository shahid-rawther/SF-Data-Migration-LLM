import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { BackfillUnit } from '../api/client';

interface Props {
  sessionId: string;
  /** Re-read when the run reaches a point that could have changed the answer. */
  refreshKey?: number;
  /**
   * Deferrals belonging to *earlier* sessions that this run has just made
   * fillable. They are not this session's to approve, so they are reported
   * rather than listed with the others — but they have to be reported
   * somewhere, because the session that owns them has no reason to be open.
   */
  unblocked?: BackfillUnit[];
}

/**
 * References that were loaded empty on purpose, and the decision to fill them.
 *
 * Some lookups cannot be resolved while the records are being created. If two
 * objects point at each other there is no order that would work: whichever is
 * created first has nothing to point at, because the other side has no
 * destination ids yet. The load leaves that one field empty and records the rows,
 * and this is where those rows are attached.
 *
 * Filling them means issuing updates against records that are already in the
 * destination, so it is never done automatically. Each reference is listed with
 * what it would change, and nothing runs until it is approved here.
 */
export function BackfillPanel({ sessionId, refreshKey = 0, unblocked = [] }: Props) {
  const [units, setUnits] = useState<BackfillUnit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const report = await api.migrations.backfill.list(sessionId);
      setUnits(report.units);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read the deferred references');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const act = useCallback(
    async (unit: BackfillUnit, action: 'approve' | 'skip' | 'run') => {
      setBusy(unit.id);
      setError('');
      try {
        if (action === 'run') {
          // Approval is recorded first, so the run is always acting on a
          // decision that exists rather than implying one.
          if (unit.status !== 'approved') await api.migrations.backfill.decide(unit.id, true);
          await api.migrations.backfill.run(unit.id);
        } else {
          await api.migrations.backfill.decide(unit.id, action === 'approve');
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'That did not work');
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  if (loading) return null;
  if (!units.length && !error) return null;

  const waiting = units.filter((u) => u.status === 'pending' || u.status === 'approved');
  const settled = units.filter((u) => !waiting.includes(u));

  return (
    <section style={panel}>
      <header style={{ marginBottom: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 15 }}>&#8635;</span>
          <h3 style={{ margin: 0, fontSize: 14.5, fontWeight: 700, color: '#111827' }}>
            References waiting to be attached
          </h3>
          {waiting.length > 0 && (
            <span style={countPill}>{waiting.length} needing a decision</span>
          )}
        </div>
        <p style={intro}>
          These lookups could not be filled while the records were being created, because
          the objects involved reference each other and one of them had to go first. The
          records loaded with the field empty. Attaching them now means updating records
          already in the destination, so nothing runs until you approve it.
        </p>
      </header>

      {error && <div style={errorBox}>{error}</div>}

      {unblocked.length > 0 && (
        <div style={unblockedBox}>
          <strong style={{ display: 'block', marginBottom: 4 }}>
            This run unblocked {unblocked.length} reference
            {unblocked.length === 1 ? '' : 's'} from an earlier migration
          </strong>
          <p style={{ margin: '0 0 6px' }}>
            Loading these objects gave those records something to point at. They
            belong to another session, so open it to approve the fill.
          </p>
          {unblocked.map((unit) => (
            <div key={unit.id} style={{ fontSize: 12, marginTop: 3 }}>
              <code style={{ fontWeight: 700 }}>
                {unit.destination_object}.{unit.field}
              </code>{' '}
              — {unit.rows_resolvable.toLocaleString()} record(s) can now be attached
              <span style={{ color: '#6b7280' }}> (session {unit.session_id.slice(0, 8)})</span>
            </div>
          ))}
        </div>
      )}

      {waiting.map((unit) => (
        <UnitRow key={unit.id} unit={unit} busy={busy === unit.id} onAct={act} />
      ))}

      {settled.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary style={summaryToggle}>
            {settled.length} already decided
          </summary>
          <div style={{ marginTop: 8 }}>
            {settled.map((unit) => (
              <UnitRow key={unit.id} unit={unit} busy={busy === unit.id} onAct={act} />
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function UnitRow({
  unit,
  busy,
  onAct,
}: {
  unit: BackfillUnit;
  busy: boolean;
  onAct: (unit: BackfillUnit, action: 'approve' | 'skip' | 'run') => void;
}) {
  // Nothing to attach yet: the object it points at has not been loaded, so
  // running now would update nothing. Offering the button would be misleading.
  const fillable = unit.rows_resolvable > 0;
  const decided = unit.status !== 'pending' && unit.status !== 'approved';
  const awaiting = unit.awaiting ?? [];

  return (
    <div style={row(unit.status)}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
        <code style={{ fontSize: 12.5, fontWeight: 700, color: '#111827' }}>
          {unit.destination_object}.{unit.field}
        </code>
        <span style={statusPill(unit.status)}>{unit.status}</span>
        <span style={reasonPill}>
          {unit.reason === 'self_reference' ? 'points at its own object' : 'circular reference'}
        </span>
      </div>

      <p style={{ margin: '5px 0 0', fontSize: 12.5, color: '#374151', lineHeight: 1.5 }}>
        {unit.summary}
      </p>

      {unit.detail && (
        // Not always a failure: a pass that reached some rows but not others
        // reports here what is still waiting and why.
        <p
          style={{
            margin: '4px 0 0',
            fontSize: 11.5,
            color: unit.status === 'failed' ? '#b91c1c' : '#6b7280',
          }}
        >
          {unit.detail}
        </p>
      )}

      {!decided && (
        <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            disabled={busy || !fillable}
            onClick={() => onAct(unit, 'run')}
            title={
              fillable
                ? `Update ${unit.rows_resolvable.toLocaleString()} record(s) to attach this reference`
                : awaiting.length
                  ? `Nothing can be attached yet — add ${awaiting.join(', ')} to this migration and run it`
                  : 'Nothing can be attached yet — the object this points at has not been loaded'
            }
            style={primaryBtn(busy || !fillable)}
          >
            {busy
              ? 'Attaching…'
              : fillable
                ? `Attach ${unit.rows_resolvable.toLocaleString()} record${unit.rows_resolvable === 1 ? '' : 's'}`
                : 'Nothing to attach yet'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onAct(unit, 'skip')}
            title="Leave these records without the reference"
            style={quietBtn}
          >
            Leave empty
          </button>
        </div>
      )}

      {unit.status === 'skipped' && (
        <button
          type="button"
          disabled={busy}
          onClick={() => onAct(unit, 'approve')}
          style={{ ...quietBtn, marginTop: 8 }}
        >
          Reconsider
        </button>
      )}
    </div>
  );
}

const panel: React.CSSProperties = {
  background: '#fffbeb',
  border: '1px solid #fcd34d',
  borderRadius: 10,
  padding: '12px 14px',
  marginTop: 14,
};

const intro: React.CSSProperties = {
  margin: '6px 0 0',
  fontSize: 12,
  color: '#78350f',
  lineHeight: 1.55,
};

const countPill: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: '#92400e',
  background: '#fef3c7',
  border: '1px solid #fcd34d',
  borderRadius: 999,
  padding: '1px 7px',
};

const reasonPill: React.CSSProperties = {
  fontSize: 10.5,
  color: '#6b7280',
  background: '#f3f4f6',
  border: '1px solid #e5e7eb',
  borderRadius: 999,
  padding: '1px 6px',
};

function statusPill(status: string): React.CSSProperties {
  const tones: Record<string, [string, string]> = {
    pending: ['#92400e', '#fef3c7'],
    approved: ['#1e40af', '#dbeafe'],
    running: ['#3730a3', '#e0e7ff'],
    completed: ['#065f46', '#d1fae5'],
    failed: ['#991b1b', '#fee2e2'],
    skipped: ['#4b5563', '#f3f4f6'],
  };
  const [color, background] = tones[status] ?? tones.pending;
  return {
    fontSize: 10.5,
    fontWeight: 700,
    color,
    background,
    borderRadius: 999,
    padding: '1px 7px',
  };
}

function row(status: string): React.CSSProperties {
  return {
    background: '#ffffff',
    border: '1px solid #e5e7eb',
    borderLeft: `3px solid ${status === 'completed' ? '#10b981' : status === 'failed' ? '#ef4444' : status === 'skipped' ? '#9ca3af' : '#f59e0b'}`,
    borderRadius: 8,
    padding: '9px 11px',
    marginBottom: 7,
  };
}

function primaryBtn(disabled: boolean): React.CSSProperties {
  return {
    fontSize: 12,
    fontWeight: 600,
    color: disabled ? '#9ca3af' : '#ffffff',
    background: disabled ? '#f3f4f6' : '#1d4ed8',
    border: `1px solid ${disabled ? '#e5e7eb' : '#1d4ed8'}`,
    borderRadius: 6,
    padding: '5px 11px',
    cursor: disabled ? 'not-allowed' : 'pointer',
  };
}

const quietBtn: React.CSSProperties = {
  fontSize: 12,
  color: '#374151',
  background: '#ffffff',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  padding: '5px 11px',
  cursor: 'pointer',
};

const summaryToggle: React.CSSProperties = {
  fontSize: 12,
  color: '#6b7280',
  cursor: 'pointer',
};

const unblockedBox: React.CSSProperties = {
  fontSize: 12,
  color: '#065f46',
  background: '#ecfdf5',
  border: '1px solid #a7f3d0',
  borderRadius: 6,
  padding: '8px 10px',
  marginBottom: 10,
  lineHeight: 1.55,
};

const errorBox: React.CSSProperties = {
  fontSize: 12,
  color: '#991b1b',
  background: '#fee2e2',
  border: '1px solid #fecaca',
  borderRadius: 6,
  padding: '7px 9px',
  marginBottom: 8,
};
