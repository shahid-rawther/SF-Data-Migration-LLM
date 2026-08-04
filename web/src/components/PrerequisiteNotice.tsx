import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type {
  MappingFieldDef,
  PrerequisiteGap,
  PrerequisiteNode,
  PrerequisiteReport,
} from '../api/client';

interface Props {
  sourceType: string;
  destCreds: Record<string, string>;
  /** The Salesforce object this mapping writes to. */
  sfObject: string;
  /** Salesforce objects already covered by other cards in the run. */
  selectedObjects: string[];
  fields: MappingFieldDef[];
  /** Adds the source tables that populate a missing object to the run. */
  onAddTables: (tables: string[]) => void;
}

/**
 * Salesforce objects that must be loaded before this one, in order.
 *
 * The chain comes from Salesforce lookups rather than the source schema,
 * because it is the destination that refuses a record whose parent does not
 * exist yet.
 */
export function PrerequisiteNotice({
  sourceType,
  destCreds,
  sfObject,
  selectedObjects,
  fields,
  onAddTables,
}: Props) {
  const [report, setReport] = useState<PrerequisiteReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [showOther, setShowOther] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const requestRef = useRef(0);

  // Only the populated destination fields can change the answer, so keying the
  // refetch on those keeps ordinary editing from re-running the walk.
  const populatedSignature = useMemo(
    () =>
      fields
        .filter(
          (f) =>
            (Array.isArray(f.source_field) ? f.source_field.length > 0 : !!f.source_field) ||
            f.constant_value != null ||
            !!f.use_constant,
        )
        .map((f) => f.destination_field)
        .filter(Boolean)
        .sort()
        .join(','),
    [fields],
  );
  const selectionSignature = useMemo(
    () => [...new Set(selectedObjects)].sort().join(','),
    [selectedObjects],
  );

  const load = useCallback(async () => {
    if (!sfObject) return;
    const token = ++requestRef.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.schema.prerequisites(sfObject, destCreds, {
        source_type: sourceType,
        selected_objects: [...new Set([sfObject, ...selectedObjects])],
        draft_config: { fields },
      });
      if (token === requestRef.current) setReport(result);
    } catch (err) {
      if (token === requestRef.current) {
        setError(err instanceof Error ? err.message : 'Could not read dependencies');
      }
    } finally {
      if (token === requestRef.current) setLoading(false);
    }
    // destCreds/fields/selectedObjects are tracked through the signatures above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceType, sfObject, selectionSignature, populatedSignature]);

  useEffect(() => {
    const timer = setTimeout(load, 400);
    return () => clearTimeout(timer);
  }, [load]);

  const missing = report?.missing_objects ?? [];
  const addable = useMemo(
    () => (report?.nodes ?? []).filter((n) => !n.is_target && !n.selected && n.source_tables.length > 0),
    [report],
  );

  if (dismissed || !sfObject) return null;

  if (loading && !report) {
    return (
      <div style={{ ...panel, background: '#f8fafc', borderColor: '#e2e8f0' }}>
        <span style={{ fontSize: 12, color: '#64748b' }}>
          Checking what {sfObject} depends on in Salesforce&hellip;
        </span>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...panel, background: '#f8fafc', borderColor: '#e2e8f0' }}>
        <span style={{ fontSize: 12, color: '#b91c1c' }}>Dependency check failed: {error}</span>
        <button type="button" onClick={load} style={{ ...ghostBtn, marginLeft: 8 }}>
          Retry
        </button>
      </div>
    );
  }

  if (!report) return null;

  const gaps = report.required_gaps;
  const hierarchy = report.nodes.length > 1;
  if (!hierarchy && gaps.length === 0 && report.unresolved_references.length === 0) return null;

  const ready = missing.length === 0;
  const toggle = (name: string, on: boolean) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });

  const tablesFor = (names: string[]) => {
    const nodes = (report.nodes ?? []).filter((n) => names.includes(n.object));
    return [...new Set(nodes.flatMap((n) => n.source_tables))];
  };

  return (
    <div
      style={{
        ...panel,
        background: ready ? '#f0fdf4' : '#fffbeb',
        borderColor: ready ? '#86efac' : '#fcd34d',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <strong style={{ fontSize: 13, color: ready ? '#166534' : '#92400e' }}>
          {!hierarchy
            ? `${sfObject} has no Salesforce lookups that need another migrated object`
            : ready
              ? `Everything ${sfObject} depends on is already in this migration`
              : `${missing.length} Salesforce object${missing.length === 1 ? '' : 's'} must be loaded before ${sfObject}`}
        </strong>
        {loading && <span style={{ fontSize: 11, color: '#92400e' }}>refreshing&hellip;</span>}
        <button
          type="button"
          onClick={() => setDismissed(true)}
          style={{ ...ghostBtn, marginLeft: 'auto' }}
        >
          Dismiss
        </button>
      </div>

      {hierarchy && (
        <>
          <p style={{ margin: '0 0 10px', fontSize: 12, color: ready ? '#15803d' : '#78350f' }}>
            Salesforce assigns its own record ids, so a lookup can only be filled in once the
            record it points at has been created. Load in this order.
          </p>

          <ol style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {report.nodes.map((node, i) => (
              <PrerequisiteRow
                key={node.object}
                node={node}
                position={i + 1}
                checked={checked.has(node.object)}
                onToggle={(on) => toggle(node.object, on)}
              />
            ))}
          </ol>
        </>
      )}

      {addable.length > 0 && (
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          <button
            type="button"
            disabled={checked.size === 0}
            onClick={() => {
              onAddTables(tablesFor([...checked]));
              setChecked(new Set());
            }}
            style={{
              ...actionBtn,
              background: checked.size > 0 ? '#fef3c7' : '#f3f4f6',
              color: checked.size > 0 ? '#92400e' : '#9ca3af',
              borderColor: checked.size > 0 ? '#fcd34d' : '#d1d5db',
              cursor: checked.size > 0 ? 'pointer' : 'not-allowed',
            }}
          >
            Add selected ({checked.size})
          </button>
          <button
            type="button"
            onClick={() => onAddTables(tablesFor(addable.map((n) => n.object)))}
            style={{ ...actionBtn, background: '#fff', color: '#92400e', borderColor: '#fcd34d' }}
          >
            Add all {addable.length}
          </button>
        </div>
      )}

      {report.cycles.map((cycle) => (
        <div key={cycle.join()} style={noteStyle}>
          <strong>{cycle.join(', ')}</strong> reference each other, so no strict order exists
          between them. Load them in any order and expect some lookups to fill in only after all
          of them have run.
        </div>
      ))}

      {gaps.length > 0 && (
        <div style={{ ...noteStyle, background: '#fef2f2', borderColor: '#fecaca', color: '#991b1b' }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>Required lookups with no source</div>
          {gaps.map((gap) => (
            <div key={`${gap.object}.${gap.field}`}>
              Salesforce requires <code>{gap.object}.{gap.field}</code>, which must point at{' '}
              <GapTargets gap={gap} />. Nothing in this migration creates those records, so set a
              constant record id for this field.
            </div>
          ))}
        </div>
      )}

      {report.unresolved_references.length > 0 && (
        <div style={noteStyle}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>Lookups that will not resolve</div>
          {report.unresolved_references.map((gap) => (
            <div key={`${gap.object}.${gap.field}`}>
              <code>{gap.object}.{gap.field}</code> is mapped but points at <GapTargets gap={gap} />
              , which nothing migrates. It will stay empty unless you pin a constant record.
            </div>
          ))}
        </div>
      )}

      {report.truncated && (
        <div style={noteStyle}>
          The chain was {report.truncation_reason} — there may be more beyond what is listed.
        </div>
      )}

      {report.other_references.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <button
            type="button"
            onClick={() => setShowOther((s) => !s)}
            style={{ ...ghostBtn, padding: 0, border: 'none', background: 'none' }}
          >
            {showOther ? '\u25B2' : '\u25BC'} {report.other_references.length} unmapped lookup
            {report.other_references.length === 1 ? '' : 's'} point outside the migration
          </button>
          {showOther && (
            <div style={{ fontSize: 11, color: '#78350f', marginTop: 6, lineHeight: 1.7 }}>
              {report.other_references.map((gap) => (
                <div key={`${gap.object}.${gap.field}`}>
                  <code>
                    {gap.object}.{gap.field}
                  </code>{' '}
                  &mdash; <GapTargets gap={gap} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function GapTargets({ gap }: { gap: PrerequisiteGap }) {
  const hidden = gap.candidate_count - gap.candidates.length;
  return (
    <>
      <strong>{gap.candidates.join(', ')}</strong>
      {hidden > 0 && ` and ${hidden} other object${hidden === 1 ? '' : 's'}`}
    </>
  );
}

function PrerequisiteRow({
  node,
  position,
  checked,
  onToggle,
}: {
  node: PrerequisiteNode;
  position: number;
  checked: boolean;
  onToggle: (on: boolean) => void;
}) {
  const reasons = node.needed_by
    .map((n) => `${n.object}.${n.field}${n.required ? ' (required)' : ''}`)
    .join(', ');
  const hidden = Math.max(node.needed_by_count - node.needed_by.length, 0);

  return (
    <li style={rowStyle}>
      <span style={{ ...positionBadge, background: node.is_target ? '#2563eb' : '#94a3b8' }}>
        {position}
      </span>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <code style={{ fontSize: 12, fontWeight: 700, color: '#1e293b' }}>{node.object}</code>
          <StatusChip node={node} />
          {node.required_by_someone && !node.is_target && (
            <span style={{ ...chip, background: '#fee2e2', color: '#991b1b' }}>required</span>
          )}
        </div>

        {node.is_target ? (
          <div style={detailStyle}>the object you are mapping into</div>
        ) : (
          <>
            {node.source_tables.length > 0 && (
              <div style={detailStyle}>
                populated by {node.mapping_names.slice(0, 3).join(', ')} from{' '}
                {node.source_tables.slice(0, 3).join(', ')}
              </div>
            )}
            {reasons && (
              <div style={detailStyle}>
                needed by {reasons}
                {hidden > 0 && ` and ${hidden} more`}
              </div>
            )}
          </>
        )}

        {node.self_reference_fields.length > 0 && (
          <div style={detailStyle}>
            references itself through {node.self_reference_fields.slice(0, 4).join(', ')}
            {node.self_reference_fields.length > 4 &&
              ` and ${node.self_reference_fields.length - 4} more`}{' '}
            &mdash; parents load before children within the same run
          </div>
        )}

        {node.status === 'needs_mapping' && !node.is_target && (
          <div style={{ ...detailStyle, color: '#b91c1c' }}>
            nothing migrates this object yet, so a mapping has to be created for it
          </div>
        )}
      </div>

      {!node.is_target && !node.selected && node.source_tables.length > 0 && (
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            fontSize: 11,
            color: '#78350f',
            cursor: 'pointer',
          }}
        >
          <input type="checkbox" checked={checked} onChange={(e) => onToggle(e.target.checked)} />
          add
        </label>
      )}
    </li>
  );
}

function StatusChip({ node }: { node: PrerequisiteNode }) {
  if (node.is_target) {
    return <span style={{ ...chip, background: '#dbeafe', color: '#1d4ed8' }}>this object</span>;
  }
  if (node.selected) {
    return <span style={{ ...chip, background: '#dcfce7', color: '#166534' }}>in this migration</span>;
  }
  if (node.status === 'mapping_exists') {
    return (
      <span style={{ ...chip, background: '#fef3c7', color: '#92400e' }}>mapping available</span>
    );
  }
  return <span style={{ ...chip, background: '#fee2e2', color: '#991b1b' }}>needs a mapping</span>;
}

const panel: React.CSSProperties = {
  padding: 12,
  marginBottom: 14,
  borderRadius: 8,
  border: '1px solid',
};

const rowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  padding: '6px 0',
  borderTop: '1px solid rgba(148, 163, 184, 0.25)',
};

const positionBadge: React.CSSProperties = {
  width: 20,
  height: 20,
  borderRadius: '50%',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 10,
  fontWeight: 700,
  color: '#fff',
  flexShrink: 0,
  marginTop: 2,
};

const chip: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  borderRadius: 4,
  padding: '1px 6px',
  textTransform: 'uppercase',
  letterSpacing: 0.3,
};

const detailStyle: React.CSSProperties = {
  fontSize: 11,
  color: '#64748b',
  marginTop: 2,
  wordBreak: 'break-word',
};

const noteStyle: React.CSSProperties = {
  marginTop: 8,
  padding: 8,
  borderRadius: 6,
  background: '#fff7ed',
  border: '1px solid #fed7aa',
  fontSize: 11,
  color: '#9a3412',
  lineHeight: 1.6,
};

const actionBtn: React.CSSProperties = {
  padding: '5px 12px',
  borderRadius: 6,
  border: '1px solid',
  fontSize: 12,
  fontWeight: 600,
};

const ghostBtn: React.CSSProperties = {
  padding: '3px 8px',
  borderRadius: 6,
  border: '1px solid #d1d5db',
  background: '#fff',
  color: '#6b7280',
  cursor: 'pointer',
  fontSize: 11,
  fontWeight: 600,
};
