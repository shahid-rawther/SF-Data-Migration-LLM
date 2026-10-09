import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type { HierarchyGraph, HierarchyNode, OrderStep, OrderSuggestion } from '../api/client';
import { ObjectFieldsDrawer } from './ObjectFieldsDrawer';

interface DestinationHierarchyProps {
  /** The Salesforce objects chosen on the destination side. */
  selectedObjects: string[];
  destCreds: Record<string, string>;
  /**
   * A saved connection to describe through instead of the credentials. Used
   * when this is opened on an existing session, whose credentials come back
   * masked and so cannot be sent to Salesforce.
   */
  destConnectionId?: string;
  sourceType?: string;
  /** Start with only the summary line showing. */
  defaultCollapsed?: boolean;
}

/** The Salesforce dependency hierarchy above a selection, drawn top to bottom.
 *
 *  The graph itself is computed from Salesforce's own reference metadata, so the
 *  levels and the parent-first order are facts rather than opinions. The model
 *  is asked separately, and only for advice: a reading of the sequence and what
 *  to settle first. Which object to migrate when stays the user's call.
 */
export function DestinationHierarchy({
  selectedObjects,
  destCreds,
  destConnectionId,
  sourceType,
  defaultCollapsed = false,
}: DestinationHierarchyProps) {
  const [graph, setGraph] = useState<HierarchyGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [suggestion, setSuggestion] = useState<OrderSuggestion | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestError, setSuggestError] = useState('');
  const [focus, setFocus] = useState('');
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  // The object whose fields are open in the side drawer, if any.
  const [describing, setDescribing] = useState<string | null>(null);
  // Most of an ancestor graph is reachable without being needed, so the view
  // opens on the objects the selection genuinely cannot load without and the
  // rest of the graph is there on request.
  const [neededOnly, setNeededOnly] = useState(true);

  const key = useMemo(() => [...selectedObjects].sort().join(','), [selectedObjects]);
  const requested = useRef('');

  const load = useCallback(async () => {
    if (!selectedObjects.length) {
      setGraph(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const result = await api.schema.destinationHierarchy(selectedObjects, destCreds, {
        source_type: sourceType,
        connection_id: destConnectionId,
      });
      setGraph(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read the Salesforce hierarchy');
    } finally {
      setLoading(false);
    }
  }, [selectedObjects, destCreds, destConnectionId, sourceType]);

  // The walk describes every object it visits, so it is debounced rather than
  // fired on each checkbox, and the suggestion is dropped when the selection
  // changes underneath it.
  useEffect(() => {
    if (requested.current === key) return;
    requested.current = key;
    setSuggestion(null);
    setSuggestError('');
    setFocus('');
    const timer = setTimeout(load, 500);
    return () => clearTimeout(timer);
  }, [key, load]);

  const suggest = async () => {
    setSuggesting(true);
    setSuggestError('');
    try {
      const result = await api.agents.suggestOrder(selectedObjects, destCreds, {
        source_type: sourceType,
        destination_connection_id: destConnectionId,
      });
      // The endpoint rebuilt the graph to reason over; keep the drawing and the
      // advice describing the same thing.
      setGraph(result.graph);
      setSuggestion(result.suggestion);
      if (result.suggestion.error) setSuggestError(result.suggestion.error);
    } catch (e) {
      setSuggestError(e instanceof Error ? e.message : 'The order suggestion failed');
    } finally {
      setSuggesting(false);
    }
  };

  const nodesByName = useMemo(() => {
    const map = new Map<string, HierarchyNode>();
    for (const node of graph?.nodes || []) map.set(node.object, node);
    return map;
  }, [graph]);

  const keep = useCallback(
    (name: string) => {
      if (!neededOnly) return true;
      const node = nodesByName.get(name);
      return !!node && (node.selected || node.needed);
    },
    [neededOnly, nodesByName],
  );

  // Hiding the merely-reachable ancestors can empty a whole level, so the rows
  // are rebuilt rather than filtered in place and the numbering stays contiguous.
  const rows = useMemo(() => {
    if (!graph) return [];
    if (!neededOnly) return graph.levels;
    return graph.levels
      .map((level) => level.filter(keep))
      .filter((level) => level.length > 0);
  }, [graph, neededOnly, keep]);

  // Everything the focused object depends on, all the way up, and everything
  // that depends on it — so clicking one card shows its whole chain.
  const related = useMemo(() => {
    if (!focus || !graph) return null;
    const ancestors = new Set<string>();
    const descendants = new Set<string>();
    // Walking through hidden objects would link cards the view shows as unrelated.
    const visible = graph.edges.filter((e) => keep(e.child) && keep(e.parent));
    const walk = (from: string, into: Set<string>, up: boolean) => {
      const next = visible
        .filter((e) => (up ? e.child === from : e.parent === from))
        .map((e) => (up ? e.parent : e.child));
      for (const name of next) {
        if (into.has(name)) continue;
        into.add(name);
        walk(name, into, up);
      }
    };
    walk(focus, ancestors, true);
    walk(focus, descendants, false);
    return { ancestors, descendants };
  }, [focus, graph, keep]);

  if (!selectedObjects.length) return null;

  return (
    <div style={panel}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: collapsed ? 0 : 14 }}>
        <button type="button" onClick={() => setCollapsed(!collapsed)} style={caret}>
          {collapsed ? '▸' : '▾'}
        </button>
        <div style={{ flex: 1 }}>
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: '#171717' }}>
            Salesforce dependency hierarchy
          </h3>
          <p style={{ margin: '3px 0 0', fontSize: 12.5, color: '#737373' }}>
            {graph
              ? neededOnly
                ? summariseNeeded(graph)
                : summarise(graph)
              : 'Reading what the selected objects depend on.'}
          </p>
        </div>
        {loading && (
          <span style={{ fontSize: 12, color: '#737373' }}>
            {graph ? 'Reading schema…' : 'Describing objects — the first pass can take a minute…'}
          </span>
        )}
        <button type="button" onClick={load} disabled={loading} style={ghostBtn}>
          Refresh
        </button>
      </div>

      {collapsed ? null : (
        <>
          {error && <div style={errorBox}>{error}</div>}

          {graph && graph.nodes.length > 0 && (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: '#171717' }}>
                  {neededOnly
                    ? `Needed for this migration (${
                        graph.needed_objects.length + graph.selected_objects.length
                      })`
                    : `Full hierarchy (${graph.nodes.length})`}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    setNeededOnly(!neededOnly);
                    setFocus('');
                  }}
                  style={ghostBtn}
                >
                  {neededOnly
                    ? `Show full hierarchy (${graph.nodes.length} objects)`
                    : 'Show only what is needed'}
                </button>
              </div>

              {neededOnly && (
                <p style={{ ...note, marginTop: 0, marginBottom: 12 }}>
                  {graph.needed_objects.length === 0
                    ? 'Nothing above your selection is needed: no lookup out of it is required by ' +
                      'Salesforce or filled by a mapping, so the selected objects load on their own.'
                    : `${graph.needed_objects.length} of ${graph.nodes.length - graph.selected_objects.length} ` +
                      'ancestors are needed — the lookup pointing at them is either required by ' +
                      'Salesforce or filled by one of your mappings. The rest load fine left empty.'}
                </p>
              )}

              <div style={legend}>
                <Legend swatch="#e5e5e5" border="#737373" label="you selected it" />
                <Legend swatch="#fafafa" border="#d4d4d4" label="a mapping fills it" />
                <Legend swatch="#fafafa" border="#d4d4d4" label="nothing migrates into it" />
                <span style={{ color: '#404040', fontWeight: 600 }}>★ Salesforce requires it</span>
                <span style={{ marginLeft: 'auto', color: '#a3a3a3' }}>
                  Click to trace an object's chain · hover for its fields
                </span>
              </div>

              <div style={{ overflowX: 'auto', paddingBottom: 4 }}>
                {rows.map((level, index) => (
                  <div key={index}>
                    {index > 0 && (
                      <div style={connector}>
                        <span style={{ fontSize: 12, color: '#d4d4d4' }}>▼</span>
                      </div>
                    )}
                    <div style={{ display: 'flex', alignItems: 'stretch', gap: 12 }}>
                      <div style={levelLabel}>
                        <div style={{ fontWeight: 700, color: '#404040' }}>{index + 1}</div>
                        <div style={{ fontSize: 10.5, color: '#a3a3a3', whiteSpace: 'nowrap' }}>
                          {index === 0
                            ? 'load first'
                            : index === rows.length - 1
                              ? 'load last'
                              : 'then'}
                        </div>
                      </div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, flex: 1 }}>
                        {level.map((name) => {
                          const node = nodesByName.get(name);
                          if (!node) return null;
                          return (
                            <ObjectCard
                              key={name}
                              node={node}
                              focused={focus === name}
                              dimmed={
                                !!related &&
                                focus !== name &&
                                !related.ancestors.has(name) &&
                                !related.descendants.has(name)
                              }
                              relation={
                                !related || focus === name
                                  ? ''
                                  : related.ancestors.has(name)
                                    ? 'parent'
                                    : related.descendants.has(name)
                                      ? 'child'
                                      : ''
                              }
                              onClick={() => setFocus(focus === name ? '' : name)}
                              onDescribe={() => setDescribing(name)}
                            />
                          );
                        })}
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              {graph.truncated && (
                <p style={note}>
                  The hierarchy is incomplete — {graph.truncation_reason}. Salesforce lookups can
                  chain a long way; what is shown is the part nearest your selection.
                </p>
              )}

              {graph.cycles.length > 0 && (
                <div style={warnBox}>
                  <strong>Objects that reference each other</strong>
                  {graph.cycles.map((group) => (
                    <div key={group.join()} style={{ marginTop: 4, fontSize: 12.5 }}>
                      {group.join(' ↔ ')} — no order satisfies both directions. Load one with the
                      lookup left empty, then fill it in a second pass.
                    </div>
                  ))}
                </div>
              )}

              {graph.required_gaps.length > 0 && (
                <div style={warnBox}>
                  <strong>Required lookups with nothing to point at</strong>
                  {graph.required_gaps.slice(0, 8).map((gap) => (
                    <div key={`${gap.object}.${gap.field}`} style={{ marginTop: 4, fontSize: 12.5 }}>
                      <code>
                        {gap.object}.{gap.field}
                      </code>{' '}
                      needs {gap.candidates.slice(0, 3).join(' or ')}
                      {gap.candidate_count > 3 && ` (+${gap.candidate_count - 3} more)`}, and none of
                      those are in this migration. Salesforce will reject the record unless you add
                      the parent or pin the field to an existing one.
                    </div>
                  ))}
                </div>
              )}

              <OrderPanel
                graph={graph}
                suggestion={suggestion}
                suggesting={suggesting}
                error={suggestError}
                onSuggest={suggest}
                keep={keep}
                neededOnly={neededOnly}
              />
            </>
          )}

          {graph && graph.nodes.length === 0 && !loading && (
            <p style={{ fontSize: 13, color: '#737373', margin: 0 }}>
              Nothing to draw — the selected objects could not be described. Check the Salesforce
              connection and object permissions.
            </p>
          )}
        </>
      )}

      <ObjectFieldsDrawer
        objectName={describing}
        destCreds={destCreds}
        destConnectionId={destConnectionId}
        onClose={() => setDescribing(null)}
      />
    </div>
  );
}

function ObjectCard({
  node,
  focused,
  dimmed,
  relation,
  onClick,
  onDescribe,
}: {
  node: HierarchyNode;
  focused: boolean;
  dimmed: boolean;
  relation: '' | 'parent' | 'child';
  onClick: () => void;
  onDescribe: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const tone = node.selected
    ? { bg: '#e5e5e5', border: '#737373' }
    : node.has_mapping
      ? { bg: '#fafafa', border: '#d4d4d4' }
      : { bg: '#fafafa', border: '#d4d4d4' };

  // A div rather than a button: the card carries its own Describe button, and
  // nesting one button inside another is invalid. The keyboard behaviour a
  // button would have given is restored explicitly.
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick();
        }
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setHovered(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setHovered(false);
      }}
      title={
        node.depends_on.length
          ? `Depends on ${node.depends_on
              .map(
                (d) =>
                  `${d.object} via ${d.field}` +
                  (d.required ? ' (required)' : d.mapped ? ' (mapped)' : ' (optional)'),
              )
              .join(', ')}`
          : 'Depends on nothing else in this graph'
      }
      style={{
        position: 'relative',
        textAlign: 'left',
        background: tone.bg,
        border: `${focused ? 2 : 1}px solid ${focused ? '#171717' : tone.border}`,
        borderRadius: 8,
        padding: '7px 10px',
        minWidth: 150,
        maxWidth: 260,
        cursor: 'pointer',
        opacity: dimmed ? 0.35 : 1,
        boxShadow: focused ? '0 0 0 3px rgba(10,10,10,0.10)' : 'none',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {node.required_chain && (
          <span title="Salesforce will not let the load skip this" style={{ color: '#404040' }}>
            ★
          </span>
        )}
        <span style={{ fontFamily: 'monospace', fontSize: 12.5, fontWeight: 700, color: '#171717' }}>
          {node.object}
        </span>
        {node.selected && <span style={tag('#262626', '#d4d4d4')}>selected</span>}
        {node.needed && !node.required_chain && !node.selected && (
          <span title="A mapping fills the lookup pointing at it" style={tag('#171717', '#f5f5f5')}>
            needed
          </span>
        )}
        {relation && <span style={tag('#262626', '#e5e5e5')}>{relation}</span>}
      </div>

      <div style={{ fontSize: 11, color: '#525252', marginTop: 3 }}>
        {node.has_mapping
          ? node.source_tables.length
            ? `from ${node.source_tables.slice(0, 2).join(', ')}`
            : `mapping: ${node.mapping_names.slice(0, 2).join(', ')}`
          : 'no mapping — must already exist in Salesforce'}
      </div>

      {(node.needed_by_count > 0 || node.self_reference_fields.length > 0) && (
        <div style={{ fontSize: 10.5, color: '#737373', marginTop: 3 }}>
          {node.needed_by_count > 0 &&
            `${node.needed_by_count} lookup${node.needed_by_count === 1 ? '' : 's'} point here`}
          {node.needed_by_count > 0 && node.self_reference_fields.length > 0 && ' · '}
          {node.self_reference_fields.length > 0 && 'references itself'}
        </div>
      )}

      {hovered && (
        <button
          type="button"
          // The card's own click traces the dependency chain, which is not what
          // this button is for.
          onClick={(e) => {
            e.stopPropagation();
            onDescribe();
          }}
          title={`Show the fields and types on ${node.object}`}
          style={describeBtn}
        >
          Describe
        </button>
      )}
    </div>
  );
}

function OrderPanel({
  graph,
  suggestion,
  suggesting,
  error,
  onSuggest,
  keep,
  neededOnly,
}: {
  graph: HierarchyGraph;
  suggestion: OrderSuggestion | null;
  suggesting: boolean;
  error: string;
  onSuggest: () => void;
  keep: (name: string) => boolean;
  neededOnly: boolean;
}) {
  // Before the model is asked, the graph's own parent-first order is shown. Once
  // it has answered, its sequence takes over — every step of it has already been
  // checked against the graph, and a step that conflicts says so.
  const all: OrderStep[] =
    suggestion?.steps && suggestion.steps.length
      ? suggestion.steps
      : graph.order.map((object) => ({ object, reason: '', conflict: '' }));
  // Dropping the unneeded objects cannot break the order: they only ever sat
  // above the ones that remain, so what is left is still parent-first.
  const steps = all.filter((step) => keep(step.object));

  return (
    <div style={orderBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <div style={{ flex: 1 }}>
          <strong style={{ fontSize: 13.5, color: '#171717' }}>
            Migration order{neededOnly ? ' — what is needed' : ''}
          </strong>
          <p style={{ margin: '2px 0 0', fontSize: 12, color: '#737373' }}>
            Parents first, derived from the lookups above. A suggestion — you decide what to migrate
            when. ★ marks the ones Salesforce will not accept a record without.
          </p>
        </div>
        <button type="button" onClick={onSuggest} disabled={suggesting} style={primaryBtn}>
          {suggesting ? 'Thinking…' : suggestion ? 'Ask again' : 'Explain this order'}
        </button>
      </div>

      {error && <div style={errorBox}>{error}</div>}

      {suggestion?.summary && (
        <p style={{ fontSize: 12.5, color: '#404040', margin: '0 0 10px', lineHeight: 1.5 }}>
          {suggestion.summary}
        </p>
      )}

      {suggestion && !suggestion.valid && (
        <div style={warnBox}>
          <strong>The suggested sequence conflicts with the dependencies</strong>
          <div style={{ marginTop: 4, fontSize: 12.5 }}>
            Follow the numbered order below, which comes from the reference graph. The conflicting
            steps are marked.
          </div>
        </div>
      )}

      <ol style={{ margin: 0, paddingLeft: 22, fontSize: 12.5, color: '#404040' }}>
        {steps.map((step) => {
          const required = graph.required_objects.includes(step.object);
          const mandatory =
            graph.needed_objects.includes(step.object) ||
            graph.selected_objects.includes(step.object);
          return (
            <li key={step.object} style={{ marginBottom: 5 }}>
              {required && <span style={{ color: '#404040' }}>★ </span>}
              <code style={{ fontWeight: 700, color: mandatory ? '#171717' : '#737373' }}>
                {step.object}
              </code>
              {step.reason && <span style={{ color: '#525252' }}> — {step.reason}</span>}
              {step.conflict && <span style={{ color: '#404040' }}> ({step.conflict})</span>}
            </li>
          );
        })}
      </ol>

      {suggestion && suggestion.steps.length > 0 && (
        <p style={note}>
          {suggestion.matches_computed
            ? 'The model arrived at the same sequence the dependency graph gives.'
            : 'This is the model’s sequence. It differs from the graph’s own order, which is ' +
              'normal — where objects do not depend on each other, more than one order is ' +
              'correct.'}
        </p>
      )}

      {suggestion?.decisions && suggestion.decisions.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <strong style={{ fontSize: 13, color: '#171717' }}>Settle these before you start</strong>
          {suggestion.decisions.map((decision, index) => (
            <div key={index} style={decisionRow(decision.severity)}>
              <div style={{ fontSize: 12.5, fontWeight: 600, color: '#171717' }}>
                {decision.object && (
                  <code>
                    {decision.object}
                    {decision.field ? `.${decision.field}` : ''}
                  </code>
                )}{' '}
                {decision.issue}
              </div>
              <div style={{ fontSize: 12, color: '#525252', marginTop: 2 }}>
                {decision.recommendation}
              </div>
            </div>
          ))}
        </div>
      )}

      {suggestion && suggestion.appended.length > 0 && (
        <p style={note}>
          {suggestion.appended.join(', ')} {suggestion.appended.length === 1 ? 'was' : 'were'} left
          out of the model's answer and added back from the graph.
        </p>
      )}

      {suggestion?.usage?.total_tokens ? (
        <p style={note}>
          {suggestion.usage.total_tokens.toLocaleString()} tokens over{' '}
          {suggestion.usage.calls ?? 1} call
          {(suggestion.usage.calls ?? 1) === 1 ? '' : 's'}.
        </p>
      ) : null}
    </div>
  );
}

/** One line on what the graph actually obliges, which is rarely all of it. */
function summarise(graph: HierarchyGraph): string {
  const ancestors = graph.nodes.length - graph.selected_objects.length;
  if (ancestors <= 0) {
    return 'The selected objects depend on nothing else — they can load in any order.';
  }
  const shape =
    `${ancestors} object${ancestors === 1 ? '' : 's'} sit above your selection across ` +
    `${graph.levels.length} level${graph.levels.length === 1 ? '' : 's'}. `;
  if (!graph.needed_objects.length) {
    return (
      shape +
      'None of them are needed: no lookup out of your selection is required by Salesforce ' +
      'or filled by a mapping, so the selection loads on its own.'
    );
  }
  return (
    shape +
    `${graph.needed_objects.length} of them must load first — ` +
    `${graph.needed_objects.slice(0, 4).join(', ')}` +
    `${graph.needed_objects.length > 4 ? ' and others' : ''}. The rest can be left empty.`
  );
}

/** The same facts as ``summarise``, led by what has to load first. */
function summariseNeeded(graph: HierarchyGraph): string {
  const one = graph.selected_objects.length === 1;
  const selected = listNames(graph.selected_objects);
  const needed = graph.needed_objects;
  const optional = graph.nodes.length - graph.selected_objects.length - needed.length;
  const rest =
    optional > 0
      ? ` ${optional} more object${optional === 1 ? ' sits' : 's sit'} above ${
          one ? 'it' : 'them'
        } but can load empty.`
      : '';
  if (!needed.length) {
    return (
      `${selected} can load on ${one ? 'its own' : 'their own'} — nothing above ` +
      `${one ? 'it' : 'them'} is needed.${rest}`
    );
  }
  return (
    `${selected} ${one ? 'needs' : 'need'} ${needed.length} ` +
    `object${needed.length === 1 ? '' : 's'} loaded first: ${listNames(needed)}.${rest}`
  );
}

function listNames(names: string[], limit = 4): string {
  if (names.length > limit) return `${names.slice(0, limit).join(', ')} and ${names.length - limit} more`;
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function Legend({ swatch, border, label }: { swatch: string; border: string; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
      <span
        style={{
          width: 11,
          height: 11,
          borderRadius: 3,
          background: swatch,
          border: `1px solid ${border}`,
        }}
      />
      {label}
    </span>
  );
}

const tag = (color: string, background: string): React.CSSProperties => ({
  fontSize: 9.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
  color,
  background,
  padding: '1px 5px',
  borderRadius: 4,
});

const panel: React.CSSProperties = {
  border: '1px solid #e5e5e5',
  borderRadius: 10,
  padding: 16,
  background: '#fafafa',
  marginTop: 20,
};

const caret: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  fontSize: 13,
  color: '#737373',
  padding: 0,
  width: 14,
};

const legend: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 14,
  fontSize: 12,
  color: '#737373',
  marginBottom: 12,
  flexWrap: 'wrap',
};

const levelLabel: React.CSSProperties = {
  width: 58,
  flexShrink: 0,
  textAlign: 'right',
  fontSize: 12,
  paddingTop: 8,
};

const connector: React.CSSProperties = {
  height: 18,
  marginLeft: 70,
  display: 'flex',
  alignItems: 'center',
  gap: 6,
};

const orderBox: React.CSSProperties = {
  marginTop: 16,
  padding: 14,
  background: '#fff',
  border: '1px solid #e5e5e5',
  borderRadius: 8,
};

const warnBox: React.CSSProperties = {
  marginTop: 12,
  padding: '9px 12px',
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  color: '#404040',
  fontSize: 13,
};

const errorBox: React.CSSProperties = {
  marginBottom: 12,
  padding: '9px 12px',
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  color: '#171717',
  fontSize: 12.5,
};

const note: React.CSSProperties = {
  fontSize: 11.5,
  color: '#737373',
  margin: '10px 0 0',
};

const decisionRow = (severity: string): React.CSSProperties => ({
  marginTop: 6,
  padding: '7px 10px',
  borderRadius: 6,
  borderLeft: `3px solid ${
    severity === 'blocker' ? '#0a0a0a' : severity === 'warning' ? '#737373' : '#a3a3a3'
  }`,
  background: '#fafafa',
});

const primaryBtn: React.CSSProperties = {
  padding: '6px 13px',
  background: '#171717',
  color: '#fff',
  border: 'none',
  borderRadius: 7,
  fontSize: 12.5,
  fontWeight: 600,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const describeBtn: React.CSSProperties = {
  position: 'absolute',
  top: 5,
  right: 5,
  padding: '2px 7px',
  background: '#171717',
  color: '#fff',
  border: 'none',
  borderRadius: 5,
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: 0.2,
  cursor: 'pointer',
  boxShadow: '0 1px 4px rgba(17,24,39,0.25)',
};

const ghostBtn: React.CSSProperties = {
  padding: '5px 11px',
  background: '#fff',
  border: '1px solid #d4d4d4',
  borderRadius: 7,
  fontSize: 12,
  color: '#404040',
  cursor: 'pointer',
};
