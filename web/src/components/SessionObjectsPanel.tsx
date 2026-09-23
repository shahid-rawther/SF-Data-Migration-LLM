import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type {
  MappingMeta,
  RunPlanEntry,
  SessionConnections,
  SessionRunPlan,
} from '../api/client';
import { DestinationHierarchy } from './DestinationHierarchy';

/**
 * A session's objects, with the two things a finished session usually needs
 * next: editing what a field maps to, and bringing another object in.
 *
 * Adding an object here is the same decision as choosing one in the wizard, so
 * it is given the same things to decide with — the dependency hierarchy, the
 * order advice, and a description of any object in it. That works because the
 * session records which saved connections it was created against: its
 * credentials are masked on the way out and cannot be replayed, but a
 * connection id is a handle the server resolves.
 *
 * Running from here works through the objects that have not migrated yet, so an
 * object added later joins the session instead of starting a new one, and the
 * ones already loaded keep their staged extract and their record of what they
 * left deferred. Nothing is decided silently — the plan is shown first.
 */
export function SessionObjectsPanel({
  sessionId,
  sourceType,
  status,
  onStarted,
}: {
  sessionId: string;
  sourceType: string;
  status: string;
  onStarted: (status: string) => void;
}) {
  const [plan, setPlan] = useState<SessionRunPlan | null>(null);
  const [available, setAvailable] = useState<MappingMeta[]>([]);
  const [toAdd, setToAdd] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [conns, setConns] = useState<SessionConnections | null>(null);
  const [linking, setLinking] = useState(false);
  const [linkSource, setLinkSource] = useState('');
  const [linkDest, setLinkDest] = useState('');
  // Where a mapping being considered would land in Salesforce, so the tree can
  // show it before it is committed to. Learned per mapping, on demand.
  const [destOf, setDestOf] = useState<Record<string, string>>({});

  const running = status === 'running';

  const load = useCallback(() => {
    api.migrations
      .runPlan(sessionId)
      .then(setPlan)
      .catch((e: Error) => setError(e.message));
  }, [sessionId]);

  useEffect(load, [load]);

  const loadConnections = useCallback(() => {
    api.migrations
      .connections(sessionId)
      .then((result) => {
        setConns(result);
        setLinkSource(preferred(result.source));
        setLinkDest(preferred(result.destination));
      })
      .catch(() => setConns(null));
  }, [sessionId]);

  useEffect(loadConnections, [loadConnections]);

  useEffect(() => {
    api.mappings
      .list(sourceType)
      .then(setAvailable)
      .catch(() => setAvailable([]));
  }, [sourceType]);

  const entries: RunPlanEntry[] = useMemo(
    () => (plan ? [...plan.skipped, ...plan.pending] : []),
    [plan],
  );
  const inSession = useMemo(() => new Set(entries.map((e) => e.object)), [entries]);
  const addable = useMemo(
    () => available.filter((m) => !inSession.has(m.name)).sort((a, b) => a.name.localeCompare(b.name)),
    [available, inSession],
  );

  const sourceConnId = conns?.source.connection_id ?? '';
  const destConnId = conns?.destination.connection_id ?? '';
  const linked = Boolean(sourceConnId || destConnId);

  // A ticked mapping's destination object is not in the mapping list, so it is
  // read once per mapping and kept. Without it the tree could only show the
  // session as it already is, which is the half of the question that is not
  // being asked.
  useEffect(() => {
    const unknown = toAdd.filter((name) => !(name in destOf));
    if (!unknown.length) return;
    let live = true;
    Promise.all(
      unknown.map((name) =>
        api.mappings
          .get(name)
          .then(({ config }) => [name, config.destination_object || config.salesforce_object || ''] as const)
          .catch(() => [name, ''] as const),
      ),
    ).then((pairs) => {
      if (live) setDestOf((prev) => ({ ...prev, ...Object.fromEntries(pairs) }));
    });
    return () => {
      live = false;
    };
  }, [toAdd, destOf]);

  // What the session writes into today, plus what the ticked mappings would add.
  const destObjects = useMemo(() => {
    const names = new Set<string>();
    for (const entry of entries) if (entry.destination_object) names.add(entry.destination_object);
    for (const name of toAdd) if (destOf[name]) names.add(destOf[name]);
    return [...names].sort();
  }, [entries, toAdd, destOf]);

  /** A mapping page that inherits this session's source, connections and identity. */
  const mappingHref = (mappingName?: string) => {
    const base = mappingName
      ? `/mappings/edit/${encodeURIComponent(mappingName)}`
      : '/mappings/new';
    const params = new URLSearchParams({ source: sourceType, session: sessionId });
    if (sourceConnId) params.set('srcConn', sourceConnId);
    if (destConnId) params.set('destConn', destConnId);
    return `${base}?${params.toString()}`;
  };

  const linkConnections = async () => {
    if (!linkSource && !linkDest) return;
    setBusy(true);
    setError(null);
    try {
      await api.migrations.linkConnections(sessionId, {
        source_connection_id: linkSource || undefined,
        destination_connection_id: linkDest || undefined,
      });
      setLinking(false);
      setNote('This session now reaches its source and destination on its own.');
      loadConnections();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    if (!toAdd.length) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.migrations.addObjects(sessionId, toAdd);
      setNote(
        result.added.length
          ? `Added ${result.added.join(', ')}. They will run the next time you start this session.`
          : 'Those objects were already in the session.',
      );
      setToAdd([]);
      setAdding(false);
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const run = async () => {
    if (!plan?.will_run.length) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.migrations.runRemaining(sessionId);
      setNote(`Running ${result.running.join(', ')}.`);
      onStarted('running');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runOne = async (name: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.migrations.runRemaining(sessionId, [name]);
      setNote(`Running ${name} on its own.`);
      onStarted('running');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={card}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <h2 style={h2}>Objects in this session ({entries.length})</h2>
        {!running && (
          <button onClick={() => setAdding((v) => !v)} style={secondaryBtn} disabled={busy}>
            {adding ? 'Cancel' : '+ Add objects'}
          </button>
        )}
      </div>
      <p style={hint}>
        Editing a mapping changes it everywhere it is used, not only in this
        session. Re-running an object applies the new mapping to records already
        loaded rather than duplicating them.
      </p>

      <ConnectionStrip
        conns={conns}
        linked={linked}
        linking={linking}
        busy={busy}
        linkSource={linkSource}
        linkDest={linkDest}
        onLinkSource={setLinkSource}
        onLinkDest={setLinkDest}
        onOpen={() => setLinking(true)}
        onCancel={() => setLinking(false)}
        onSave={linkConnections}
      />

      {adding && (
        <div style={addBox}>
          {addable.length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: '#6b7280' }}>
              Every mapping for this source is already in the session.{' '}
              <Link to={mappingHref()} style={link}>
                Create a new mapping
              </Link>{' '}
              to migrate something else.
            </p>
          ) : (
            <>
              <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 8 }}>
                Pick what to bring into this session. The hierarchy below follows
                what you tick, so you can see where an object lands before
                committing to it. Nothing runs until you start it.
              </div>
              <div style={addList}>
                {addable.map((mapping) => (
                  <label key={mapping.name} style={addRow}>
                    <input
                      type="checkbox"
                      checked={toAdd.includes(mapping.name)}
                      onChange={(e) =>
                        setToAdd((prev) =>
                          e.target.checked
                            ? [...prev, mapping.name]
                            : prev.filter((n) => n !== mapping.name),
                        )
                      }
                    />
                    <span>{mapping.name}</span>
                    {destOf[mapping.name] && (
                      <span style={{ fontSize: 11, color: '#9ca3af' }}>
                        → {destOf[mapping.name]}
                      </span>
                    )}
                  </label>
                ))}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <button onClick={add} style={primaryBtn} disabled={busy || !toAdd.length}>
                  {busy ? 'Adding…' : `Add ${toAdd.length || ''} to session`}
                </button>
                <Link to={mappingHref()} style={link}>
                  Map something else instead
                </Link>
              </div>
            </>
          )}
        </div>
      )}

      {error && <p style={{ color: '#dc2626', fontSize: 13 }}>{error}</p>}
      {note && <p style={{ color: '#065f46', fontSize: 13 }}>{note}</p>}

      {destConnId && destObjects.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          <DestinationHierarchy
            selectedObjects={destObjects}
            destCreds={NO_CREDENTIALS}
            destConnectionId={destConnId}
            sourceType={sourceType}
          />
        </div>
      )}

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.object} style={{ borderBottom: '1px solid #f3f4f6' }}>
              <td style={{ padding: '10px 8px 10px 0' }}>
                <div style={{ fontWeight: 600 }}>{entry.object}</div>
                <div style={{ fontSize: 12, color: '#6b7280' }}>
                  → {entry.destination_object || 'unmapped'}
                  {entry.source_rows > 0 && (
                    <> · {entry.loaded.toLocaleString()} of {entry.source_rows.toLocaleString()} loaded</>
                  )}
                </div>
              </td>
              <td style={{ padding: '10px 8px', width: 110 }}>
                <span style={stateChip(entry.state)}>{stateLabel(entry.state)}</span>
              </td>
              <td style={{ padding: '10px 0', textAlign: 'right', whiteSpace: 'nowrap' }}>
                <Link to={mappingHref(entry.object)} style={linkBtn}>
                  Edit fields
                </Link>
                {!running && (
                  <button onClick={() => runOne(entry.object)} style={ghostBtn} disabled={busy}>
                    Run
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {!running && plan && (
        <div style={runBar}>
          {plan.will_run.length === 0 ? (
            <span style={{ fontSize: 13, color: '#6b7280' }}>
              Every object here has migrated. Add one, or use Retry above to run the
              whole session again.
            </span>
          ) : (
            <>
              <span style={{ fontSize: 13, color: '#374151' }}>
                <strong>{plan.will_run.length}</strong> object
                {plan.will_run.length !== 1 ? 's' : ''} still to run
                {plan.will_skip.length > 0 && (
                  <>
                    ; {plan.will_skip.length} already migrated and will be left as
                    {plan.will_skip.length !== 1 ? ' they are' : ' it is'}
                  </>
                )}
                .
              </span>
              <button onClick={run} style={primaryBtn} disabled={busy}>
                {busy ? 'Starting…' : `Run ${plan.will_run.join(', ')}`}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Which connections the session works through, and a way to say so when it does
 * not know.
 *
 * A session created before the link was recorded, or restored from a
 * checkpoint, has credentials that cannot be replayed and no connection to
 * resolve — so the hierarchy, the field lists and the field matcher all come
 * back empty with nothing explaining why. Rather than leave that to be
 * discovered, it is stated, and the connection that reaches the same instance
 * the session was created against is already selected.
 */
function ConnectionStrip({
  conns,
  linked,
  linking,
  busy,
  linkSource,
  linkDest,
  onLinkSource,
  onLinkDest,
  onOpen,
  onCancel,
  onSave,
}: {
  conns: SessionConnections | null;
  linked: boolean;
  linking: boolean;
  busy: boolean;
  linkSource: string;
  linkDest: string;
  onLinkSource: (id: string) => void;
  onLinkDest: (id: string) => void;
  onOpen: () => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (!conns) return null;

  const nameOf = (side: SessionConnections['source']) =>
    side.candidates.find((c) => c.id === side.connection_id)?.name ?? '';

  if (linked && !linking) {
    return (
      <div style={connStrip}>
        <span>
          Working through <strong>{nameOf(conns.source) || 'no source connection'}</strong> and{' '}
          <strong>{nameOf(conns.destination) || 'no destination connection'}</strong>.
        </span>
        <button type="button" onClick={onOpen} style={ghostBtn} disabled={busy}>
          Change
        </button>
      </div>
    );
  }

  if (!linking) {
    return (
      <div style={connPrompt}>
        <div style={{ fontSize: 13, fontWeight: 600, color: '#92400e', marginBottom: 4 }}>
          This session has no saved connection to work through
        </div>
        <p style={{ margin: '0 0 10px', fontSize: 12.5, color: '#92400e' }}>
          Its credentials are held for running the migration but are masked when
          read back, so nothing here can describe a Salesforce object. Point it at
          a saved connection and the dependency hierarchy, the field lists and the
          field matcher all work as they did in the wizard.
        </p>
        <button type="button" onClick={onOpen} style={primaryBtn} disabled={busy}>
          Choose connections
        </button>
      </div>
    );
  }

  return (
    <div style={connPrompt}>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 10 }}>
        <ConnectionPicker
          label={`Source (${conns.source_type})`}
          side={conns.source}
          value={linkSource}
          onChange={onLinkSource}
        />
        <ConnectionPicker
          label="Destination (salesforce)"
          side={conns.destination}
          value={linkDest}
          onChange={onLinkDest}
        />
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          type="button"
          onClick={onSave}
          style={primaryBtn}
          disabled={busy || (!linkSource && !linkDest)}
        >
          {busy ? 'Linking…' : 'Use these connections'}
        </button>
        <button type="button" onClick={onCancel} style={ghostBtn} disabled={busy}>
          Cancel
        </button>
      </div>
      <p style={{ margin: '10px 0 0', fontSize: 11.5, color: '#92400e' }}>
        The session adopts the credentials of whatever you pick, so this is also
        how a session whose password has changed is put back to work.
      </p>
    </div>
  );
}

function ConnectionPicker({
  label,
  side,
  value,
  onChange,
}: {
  label: string;
  side: SessionConnections['source'];
  value: string;
  onChange: (id: string) => void;
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: '#374151' }}>
      <span style={{ fontWeight: 600 }}>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} style={selectStyle}>
        <option value="">— leave as it is —</option>
        {side.candidates.map((candidate) => (
          <option key={candidate.id} value={candidate.id}>
            {candidate.name}
            {candidate.matches_session ? ' — this session was created against it' : ''}
          </option>
        ))}
      </select>
      {side.candidates.length === 0 && (
        <span style={{ fontSize: 11.5, color: '#92400e' }}>
          No saved connection of this type. Save one in the migration wizard first.
        </span>
      )}
    </label>
  );
}

/** The connection to offer: the one it was created against, else the only one. */
function preferred(side: SessionConnections['source']): string {
  if (side.connection_id) return side.connection_id;
  const match = side.candidates.find((candidate) => candidate.matches_session);
  if (match) return match.id;
  return side.candidates.length === 1 ? side.candidates[0].id : '';
}

function stateLabel(state: RunPlanEntry['state']): string {
  if (state === 'completed') return 'Migrated';
  if (state === 'in_progress') return 'Part done';
  if (state === 'unmapped') return 'No mapping';
  return 'Not run';
}

function stateChip(state: RunPlanEntry['state']): React.CSSProperties {
  const palette: Record<string, [string, string]> = {
    completed: ['#dcfce7', '#166534'],
    in_progress: ['#fef3c7', '#92400e'],
    pending: ['#f3f4f6', '#374151'],
    unmapped: ['#fee2e2', '#991b1b'],
  };
  const [background, color] = palette[state] ?? palette.pending;
  return {
    background,
    color,
    padding: '3px 9px',
    borderRadius: 20,
    fontSize: 11,
    fontWeight: 600,
    whiteSpace: 'nowrap',
  };
}

/** Stable identity so the hierarchy's effects do not re-fire on every render. */
const NO_CREDENTIALS: Record<string, string> = {};

const connStrip: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  flexWrap: 'wrap',
  fontSize: 12.5,
  color: '#4b5563',
  padding: '8px 12px',
  background: '#f9fafb',
  border: '1px solid #e5e7eb',
  borderRadius: 8,
  marginBottom: 14,
};
const connPrompt: React.CSSProperties = {
  padding: 14,
  background: '#fffbeb',
  border: '1px solid #fde68a',
  borderRadius: 8,
  marginBottom: 14,
};
const selectStyle: React.CSSProperties = {
  padding: '7px 10px',
  border: '1px solid #d1d5db',
  borderRadius: 7,
  fontSize: 13,
  minWidth: 240,
  background: '#fff',
};

const card: React.CSSProperties = {
  background: '#fff',
  border: '1px solid #e5e7eb',
  borderRadius: 12,
  padding: '20px 24px',
  marginBottom: 16,
  boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
};
const h2: React.CSSProperties = { margin: 0, fontSize: 16, fontWeight: 700 };
const hint: React.CSSProperties = { margin: '0 0 14px', fontSize: 12, color: '#6b7280' };
const addBox: React.CSSProperties = {
  border: '1px solid #e5e7eb',
  borderRadius: 8,
  padding: 14,
  marginBottom: 14,
  background: '#f9fafb',
};
const addList: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))',
  gap: 6,
  maxHeight: 200,
  overflowY: 'auto',
  marginBottom: 12,
};
const addRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  fontSize: 13,
  cursor: 'pointer',
};
const runBar: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: 12,
  flexWrap: 'wrap',
  marginTop: 16,
  paddingTop: 14,
  borderTop: '1px solid #e5e7eb',
};
const primaryBtn: React.CSSProperties = {
  padding: '8px 16px',
  background: '#2563eb',
  color: '#fff',
  border: 'none',
  borderRadius: 8,
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
};
const secondaryBtn: React.CSSProperties = {
  padding: '6px 12px',
  background: '#fff',
  border: '1px solid #d1d5db',
  borderRadius: 8,
  fontSize: 12,
  fontWeight: 600,
  color: '#374151',
  cursor: 'pointer',
};
const ghostBtn: React.CSSProperties = {
  padding: '4px 10px',
  marginLeft: 8,
  background: '#fff',
  border: '1px solid #d1d5db',
  borderRadius: 6,
  fontSize: 12,
  fontWeight: 600,
  color: '#374151',
  cursor: 'pointer',
};
const linkBtn: React.CSSProperties = {
  padding: '4px 10px',
  background: '#eff6ff',
  border: '1px solid #bfdbfe',
  borderRadius: 6,
  fontSize: 12,
  fontWeight: 600,
  color: '#1d4ed8',
  textDecoration: 'none',
};
const link: React.CSSProperties = { color: '#2563eb' };
