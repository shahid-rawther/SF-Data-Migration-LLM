import { useMemo, useState } from 'react';
import { api } from '../api/client';
import type {
  ReadinessCheck,
  ReadinessCredentials,
  ReadinessGap,
  ReadinessRequirement,
  ReadinessResolution,
  ReadinessSalesforceAction,
  ReadinessSourceAction,
  ReadinessStatus,
  ReadinessSuggestion,
} from '../api/client';

interface DataReadinessPanelProps {
  sourceType: string;
  /** The mappings this migration will run, as saved in step 4. */
  mappingNames: string[];
  sourceCreds: Record<string, string>;
  destCreds: Record<string, string>;
  /**
   * Called after a suggestion is applied or undone. ``touched`` are mappings
   * whose saved config changed, ``included`` are mappings the suggestion added
   * to the run; the wizard holds its own copy of both and has to re-read them.
   */
  onMappingsChanged: (touched: string[], included: string[]) => Promise<void> | void;
}

/** Whether the source can supply what Salesforce insists on, before anything runs.
 *
 *  The classification is deterministic; the agent only investigates the gaps
 *  and suggests fixes, in the mapping, in Salesforce or in the source. Every
 *  suggestion is shown as the exact change it would make, and nothing is
 *  written until Apply is clicked — twice, when it writes to either system.
 */
export function DataReadinessPanel({
  sourceType,
  mappingNames,
  sourceCreds,
  destCreds,
  onMappingsChanged,
}: DataReadinessPanelProps) {
  const [check, setCheck] = useState<ReadinessCheck | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  // "gap:suggestion" of the one being applied or undone.
  const [busy, setBusy] = useState('');
  // A suggestion that writes to Salesforce waits here for its second click.
  const [confirming, setConfirming] = useState('');
  const [messages, setMessages] = useState<Record<string, { ok: boolean; text: string }>>({});
  const [showCovered, setShowCovered] = useState(false);

  const credentials: ReadinessCredentials = {
    source_credentials: sourceCreds,
    destination_credentials: destCreds,
  };
  const sourceName = sourceType === 'servicenow' ? 'ServiceNow' : 'source';

  const run = async () => {
    setRunning(true);
    setError('');
    setMessages({});
    setConfirming('');
    try {
      setCheck(
        await api.agents.readiness({
          source_type: sourceType,
          selected_objects: mappingNames,
          ...credentials,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The readiness check failed');
    } finally {
      setRunning(false);
    }
  };

  const refresh = async (id: number) => {
    try {
      setCheck(await api.agents.getReadiness(id));
    } catch {
      // The action itself succeeded; a stale view is corrected by checking again.
    }
  };

  const say = (key: string, ok: boolean, text: string) =>
    setMessages((prev) => ({ ...prev, [key]: { ok, text } }));

  const apply = async (gapIndex: number, suggestionIndex: number, suggestion: ReadinessSuggestion) => {
    if (!check) return;
    const key = `${gapIndex}:${suggestionIndex}`;
    if (writesToSystems(suggestion) && confirming !== key) {
      setConfirming(key);
      return;
    }
    setConfirming('');
    setBusy(key);
    try {
      const result = await api.agents.applyReadiness(check.id, gapIndex, suggestionIndex, credentials);
      const touched = unique([
        ...result.changes.filter((c) => c.applied !== false).map((c) => c.mapping_name),
        ...suggestion.resolutions.map((r) => r.mapping),
      ]);
      await onMappingsChanged(touched, result.include_mappings);
      say(
        key,
        true,
        appliedSentence(
          (result.source ?? []).reduce((n, s) => n + s.records, 0),
          result.salesforce.length,
          result.change_ids.length,
          result.include_mappings,
          sourceName,
        ),
      );
      await refresh(check.id);
    } catch (e) {
      say(key, false, e instanceof Error ? e.message : 'Could not apply this suggestion');
    } finally {
      setBusy('');
    }
  };

  const undo = async (gapIndex: number, suggestionIndex: number, suggestion: ReadinessSuggestion) => {
    if (!check) return;
    const key = `${gapIndex}:${suggestionIndex}`;
    const applied = check.applied[key];
    if (!applied) return;
    setBusy(key);
    const problems: string[] = [];
    try {
      if (applied.change_ids.length) {
        try {
          await api.agents.revertRun(applied.run_id);
        } catch (e) {
          problems.push(e instanceof Error ? e.message : 'the mapping change could not be undone');
        }
      }
      // Mappings first, so nothing is left pointing at a record about to go.
      const records = check.sf_changes.filter(
        (c) => applied.sf_change_ids.includes(c.id) && c.revertible && !c.reverted,
      );
      for (const change of records) {
        try {
          await api.agents.revertReadinessSfChange(change.id, credentials);
        } catch (e) {
          problems.push(
            `${change.object_name} ${change.record_id}: ${e instanceof Error ? e.message : 'not deleted'}`,
          );
        }
      }
      const sourceChanges = (check.source_changes ?? []).filter(
        (c) => (applied.source_change_ids ?? []).includes(c.id) && !c.reverted,
      );
      let kept = 0;
      for (const change of sourceChanges) {
        try {
          kept += (await api.agents.revertReadinessSourceChange(change.id, credentials)).kept;
        } catch (e) {
          problems.push(
            `${change.table_name}.${change.field_name}: ${e instanceof Error ? e.message : 'not restored'}`,
          );
        }
      }
      const fields = check.sf_changes.filter(
        (c) => applied.sf_change_ids.includes(c.id) && !c.revertible,
      );
      await onMappingsChanged(unique(suggestion.resolutions.map((r) => r.mapping)), []);
      if (problems.length) {
        say(key, false, `Partly undone: ${problems.join('; ')}`);
      } else {
        say(
          key,
          true,
          'Undone.' +
            (kept
              ? ` ${kept} ${sourceName} record${kept === 1 ? ' was' : 's were'} edited since and kept as they are.`
              : '') +
            (fields.length
              ? ` ${fields.map((f) => `${f.object_name}.${f.field_name}`).join(', ')} was left in ` +
                'Salesforce — delete it in Setup if it is not needed.'
              : '') +
            (applied.include_mappings.length
              ? ` ${applied.include_mappings.join(', ')} stays in this migration; remove it in step 4 if you ` +
                'do not want it.'
              : ''),
        );
      }
      await refresh(check.id);
    } finally {
      setBusy('');
    }
  };

  const requirements = useMemo(() => {
    const byKey = new Map<string, ReadinessRequirement>();
    for (const r of check?.report.requirements || []) byKey.set(r.key, r);
    return byKey;
  }, [check]);

  // Grouped by destination object, the unit a person thinks in when fixing.
  const groups = useMemo(() => {
    const map = new Map<string, { gap: ReadinessGap; index: number }[]>();
    (check?.gaps || []).forEach((gap, index) => {
      const list = map.get(gap.object) ?? [];
      list.push({ gap, index });
      map.set(gap.object, list);
    });
    return [...map.entries()];
  }, [check]);

  const covered = (check?.report.requirements || []).filter((r) => !r.is_gap);
  const stale =
    !!check &&
    [...check.report.selected].sort().join(',') !== [...mappingNames].sort().join(',');

  return (
    <div style={panel}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ flex: 1 }}>
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: '#171717' }}>Data readiness</h3>
          <p style={{ margin: '3px 0 0', fontSize: 12.5, color: '#737373' }}>
            Checks that {sourceType === 'servicenow' ? 'ServiceNow' : 'the source'} actually has data for
            every field Salesforce requires, and that every record a required lookup points at can exist.
            For each gap the agent suggests a fix in the mapping, in Salesforce or in {sourceName}; nothing
            is changed until you click Apply.
          </p>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running || mappingNames.length === 0}
          style={primaryBtn}
        >
          {running ? 'Checking…' : check ? 'Check again' : 'Check data readiness'}
        </button>
      </div>

      {running && (
        <p style={note}>
          Counting source rows and reading the Salesforce schema, then asking the agent about any gaps.
          This usually takes one to two minutes.
        </p>
      )}
      {error && <div style={errorBox}>{error}</div>}

      {check && !running && (
        <div style={{ marginTop: 14 }}>
          {stale && (
            <div style={warnBox}>
              The mappings in this migration changed since this check. Check again to see the requirements of
              what was added.
            </div>
          )}

          <div style={summaryBox(check.gaps.length === 0)}>
            <div style={{ fontWeight: 700, fontSize: 14, color: '#171717' }}>
              {check.gaps.length === 0
                ? 'Ready: the source can fill everything Salesforce requires'
                : `${check.gaps.length} gap${check.gaps.length === 1 ? '' : 's'} to settle before migrating`}
            </div>
            {check.summary && (
              <div style={{ fontSize: 13, color: '#404040', marginTop: 4, lineHeight: 1.5 }}>{check.summary}</div>
            )}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
              {Object.entries(check.report.status_counts).map(([status, count]) => (
                <span key={status} style={chip(status === 'covered')}>
                  {count} {STATUS_LABEL[status as ReadinessStatus] ?? status}
                </span>
              ))}
            </div>
          </div>

          {check.error && (
            <p style={note}>
              The agent could not finish ({check.error}), so the suggestions below come from the built-in
              rules.
            </p>
          )}
          {check.report.warnings.map((w) => (
            <p key={w} style={note}>
              {w}
            </p>
          ))}

          {check.report.chain_mappings.length > 0 && (
            <p style={{ ...note, color: '#404040' }}>
              Also checked, because the selection depends on them:{' '}
              {check.report.chain_mappings.map((name, i) => {
                const via = unique(
                  check.report.requirements.filter((r) => r.mapping === name).flatMap((r) => r.via),
                );
                return (
                  <span key={name}>
                    {i > 0 && ', '}
                    <code>{name}</code>
                    {via.length > 0 && ` (via ${via.join(', ')})`}
                  </span>
                );
              })}
              . They are not in this migration unless a suggestion adds them.
            </p>
          )}

          {check.report.capacity.map((c) => (
            <div key={c.mapping} style={c.short ? warnBox : { ...note, marginTop: 8 }}>
              <strong>User licences for {c.mapping}:</strong> {c.detail}
            </div>
          ))}

          {groups.map(([object, entries]) => (
            <div key={object} style={{ marginTop: 16 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: '#171717', marginBottom: 6 }}>{object}</div>
              {entries.map(({ gap, index }) => (
                <GapCard
                  key={gap.key}
                  gap={gap}
                  gapIndex={index}
                  requirement={requirements.get(gap.key)}
                  check={check}
                  sourceName={sourceName}
                  busy={busy}
                  confirming={confirming}
                  messages={messages}
                  onApply={apply}
                  onUndo={undo}
                  onCancel={() => setConfirming('')}
                />
              ))}
            </div>
          ))}

          {covered.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <button type="button" onClick={() => setShowCovered(!showCovered)} style={linkBtn}>
                {showCovered ? '▾' : '▸'} {covered.length} required field{covered.length === 1 ? '' : 's'}{' '}
                already covered
              </button>
              {showCovered && (
                <ul style={{ margin: '6px 0 0', paddingLeft: 20, fontSize: 12.5, color: '#525252' }}>
                  {covered.map((r) => (
                    <li key={r.key} style={{ marginBottom: 3 }}>
                      <code>
                        {r.object}.{r.field}
                      </code>{' '}
                      — {r.detail}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {check.usage?.total_tokens ? (
            <p style={note}>
              {check.usage.total_tokens.toLocaleString()} tokens over {check.usage.calls ?? 1} call
              {(check.usage.calls ?? 1) === 1 ? '' : 's'}.
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}

function GapCard({
  gap,
  gapIndex,
  requirement,
  check,
  sourceName,
  busy,
  confirming,
  messages,
  onApply,
  onUndo,
  onCancel,
}: {
  gap: ReadinessGap;
  gapIndex: number;
  requirement: ReadinessRequirement | undefined;
  check: ReadinessCheck;
  sourceName: string;
  busy: string;
  confirming: string;
  messages: Record<string, { ok: boolean; text: string }>;
  onApply: (gap: number, suggestion: number, s: ReadinessSuggestion) => void;
  onUndo: (gap: number, suggestion: number, s: ReadinessSuggestion) => void;
  onCancel: () => void;
}) {
  const coverage =
    requirement && requirement.total_rows > 0 && requirement.empty_rows >= 0
      ? `${requirement.empty_rows.toLocaleString()} of ${requirement.total_rows.toLocaleString()} source rows empty`
      : requirement && requirement.total_rows > 0
        ? `${requirement.total_rows.toLocaleString()} source rows`
        : '';

  return (
    <div style={gapCard}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <code style={{ fontWeight: 700, fontSize: 13, color: '#171717' }}>
          {gap.field ? `${gap.object}.${gap.field}` : gap.object}
        </code>
        <span style={badge}>{STATUS_LABEL[gap.status] ?? gap.status}</span>
        {!gap.in_run && <span style={badge}>from {gap.mapping}, not in this run</span>}
        {coverage && <span style={{ fontSize: 11.5, color: '#737373' }}>{coverage}</span>}
      </div>
      <p style={{ fontSize: 12.5, color: '#404040', margin: '6px 0 0', lineHeight: 1.5 }}>{gap.analysis}</p>

      {gap.suggestions.map((suggestion, index) => {
        const key = `${gapIndex}:${index}`;
        const applied = check.applied[key];
        const live = applied && !applied.reverted;
        const message = messages[key];
        return (
          <div key={index} style={suggestionBox(index === 0)}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#171717' }}>
                  {index === 0 && gap.suggestions.length > 1 && <span style={recommended}>Recommended</span>}
                  {suggestion.title || 'Suggested change'}
                </div>
                {suggestion.explanation && (
                  <div style={{ fontSize: 12.5, color: '#525252', marginTop: 3, lineHeight: 1.5 }}>
                    {suggestion.explanation}
                  </div>
                )}
              </div>
              <div style={{ flexShrink: 0 }}>
                {live ? (
                  <button
                    type="button"
                    onClick={() => onUndo(gapIndex, index, suggestion)}
                    disabled={!!busy}
                    style={ghostBtn}
                  >
                    {busy === key ? 'Undoing…' : 'Undo'}
                  </button>
                ) : suggestion.applicable ? (
                  <button
                    type="button"
                    onClick={() => onApply(gapIndex, index, suggestion)}
                    disabled={!!busy}
                    style={applyBtn}
                  >
                    {busy === key ? 'Applying…' : 'Apply'}
                  </button>
                ) : suggestion.needs_person ? (
                  <span style={badge}>Needs a person</span>
                ) : null}
              </div>
            </div>

            <Preview suggestion={suggestion} requirement={requirement} sourceName={sourceName} />

            {suggestion.manual_steps.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <div style={previewLabel}>You will need to</div>
                <ol style={{ margin: '2px 0 0', paddingLeft: 20, fontSize: 12.5, color: '#404040' }}>
                  {suggestion.manual_steps.map((step, i) => (
                    <li key={i} style={{ marginBottom: 2 }}>
                      {step}
                    </li>
                  ))}
                </ol>
              </div>
            )}

            {suggestion.rejected.length > 0 && (
              <p style={{ ...note, marginTop: 8 }}>
                {suggestion.applicable ? 'Left out' : 'Cannot be applied'} — the change did not pass validation:{' '}
                {suggestion.rejected.map((r) => r.reason).join('; ')}
              </p>
            )}

            {confirming === key && (
              <div style={confirmBox}>
                <div style={{ fontSize: 12.5, color: '#171717', fontWeight: 600 }}>
                  This writes to{' '}
                  {[
                    (suggestion.source_actions ?? []).length ? `${sourceName} (live data)` : '',
                    suggestion.salesforce_actions.length ? 'your Salesforce org' : '',
                  ]
                    .filter(Boolean)
                    .join(' and ')}
                  .
                </div>
                <div style={{ fontSize: 12, color: '#525252', marginTop: 2 }}>
                  {[
                    ...(suggestion.source_actions ?? []).map((a) => describeSourceAction(a, sourceName)),
                    ...suggestion.salesforce_actions.map(describeAction),
                  ].join(' ')}{' '}
                  {(suggestion.source_actions ?? []).length > 0 &&
                    `Undo empties those ${sourceName} values again, except on records edited since. `}
                  {suggestion.salesforce_actions.length > 0 &&
                    'Created Salesforce records can be undone; created fields are left in place.'}
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                  <button
                    type="button"
                    onClick={() => onApply(gapIndex, index, suggestion)}
                    disabled={!!busy}
                    style={applyBtn}
                  >
                    Confirm and apply
                  </button>
                  <button type="button" onClick={onCancel} style={ghostBtn}>
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {live && !message && (
              <p style={{ ...note, marginTop: 8, color: '#404040' }}>
                Applied {new Date(applied.applied_at).toLocaleString()}.
              </p>
            )}
            {message && (
              <p style={{ ...note, marginTop: 8, color: message.ok ? '#404040' : '#0a0a0a', fontWeight: message.ok ? 400 : 600 }}>
                {message.text}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Exactly what Apply would do, in the order it does it. */
function Preview({
  suggestion,
  requirement,
  sourceName,
}: {
  suggestion: ReadinessSuggestion;
  requirement: ReadinessRequirement | undefined;
  sourceName: string;
}) {
  const sourceActions = suggestion.source_actions ?? [];
  const hasAny =
    suggestion.include_mappings.length ||
    sourceActions.length ||
    suggestion.salesforce_actions.length ||
    suggestion.resolutions.length;
  if (!hasAny) return null;
  return (
    <div style={previewBox}>
      {suggestion.include_mappings.length > 0 && (
        <PreviewRow label="Adds to this migration">
          {suggestion.include_mappings.map((m) => (
            <code key={m} style={{ marginRight: 6 }}>
              {m}
            </code>
          ))}
        </PreviewRow>
      )}
      {sourceActions.map((action, i) => (
        <PreviewRow key={`s${i}`} label={`${sourceName}, step ${i + 1}`}>
          {describeSourceAction(action, sourceName)}
        </PreviewRow>
      ))}
      {suggestion.salesforce_actions.map((action, i) => (
        <PreviewRow key={`a${i}`} label={`Salesforce, step ${i + 1}`}>
          {describeAction(action)}
          {action.kind === 'create_record' && action.values && (
            <div style={{ fontFamily: 'monospace', fontSize: 11.5, color: '#525252', marginTop: 2 }}>
              {Object.entries(action.values)
                .map(([k, v]) => `${k} = ${String(v)}`)
                .join(' · ')}
            </div>
          )}
        </PreviewRow>
      ))}
      {suggestion.resolutions.map((resolution, i) => (
        <PreviewRow key={`r${i}`} label={`Mapping ${resolution.mapping}`}>
          <code>{resolution.field}</code>:{' '}
          <span style={{ color: '#737373' }}>{before(resolution, requirement)}</span> →{' '}
          <span style={{ color: '#171717' }}>{after(resolution)}</span>
        </PreviewRow>
      ))}
    </div>
  );
}

function PreviewRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 10, fontSize: 12.5, color: '#404040', marginTop: 4 }}>
      <div style={{ ...previewLabel, width: 150, flexShrink: 0, paddingTop: 1 }}>{label}</div>
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
    </div>
  );
}

function before(resolution: ReadinessResolution, requirement: ReadinessRequirement | undefined): string {
  if (!requirement || requirement.field !== resolution.field || requirement.mapping !== resolution.mapping) {
    return 'as mapped now';
  }
  if (requirement.value_source === 'constant' || requirement.value_source === 'env_constant') return 'a constant';
  if (requirement.source_field) return `from ${requirement.source_field}`;
  return 'not mapped';
}

function after(r: ReadinessResolution): string {
  const record = (value: unknown) =>
    /^\{\{action:(\d+)\}\}$/.test(String(value ?? '').trim())
      ? `the record created in Salesforce step ${Number(String(value).match(/\d+/)?.[0] ?? 0) + 1}`
      : String(value ?? '');
  const params = r.transform_params && Object.keys(r.transform_params).length
    ? ` (${Object.entries(r.transform_params)
        .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
        .join(', ')})`
    : '';
  switch (r.kind) {
    case 'type_coercion':
      return `from ${r.source_field || 'its source'} through ${r.transform ?? 'a transform'}${params}`;
    case 'value_map':
      return `value map of ${Object.keys(r.value_map ?? {}).length} source values${
        r.source_field ? ` from ${r.source_field}` : ''
      }`;
    case 'record_value_map':
      return `source values mapped to ${Object.keys(r.value_map ?? {}).length} existing records${
        r.value_map_default ? `, otherwise ${record(r.value_map_default)}` : ''
      }`;
    case 'lookup_reference':
      return `resolved from ${r.source_field || 'the source id'} against ${(r.parent_mappings ?? []).join(', ')}`;
    case 'deferred_lookup':
      return 'resolved in a second pass, once its parent has loaded';
    case 'self_reference_guard':
      return 'resolved against records this mapping loads';
    case 'constant_reference':
      return `always ${record(r.constant_value)}`;
    case 'remove_field':
      return 'no longer written';
    case 'external_id_upsert':
      return 'upserted on an external id';
    case 'notice':
      return `note only: ${r.suggested_action || r.rationale}`;
    default:
      return r.kind.replace(/_/g, ' ');
  }
}

function describeAction(action: ReadinessSalesforceAction): string {
  if (action.kind === 'create_custom_field') {
    return `Create the custom field ${action.object}.${action.field_api_name} (${action.field_type ?? 'Text'}${
      action.length ? `, ${action.length}` : ''
    }).`;
  }
  const name = action.values?.Name ?? action.values?.DeveloperName ?? action.values?.LastName;
  return `Create one ${action.object} record${name ? ` named "${String(name)}"` : ''}.`;
}

function describeSourceAction(action: ReadinessSourceAction, sourceName: string): string {
  const count = action.affected != null ? `${action.affected.toLocaleString()} ` : '';
  const value = action.reference_table ? `the ${action.reference_table} record ${action.value}` : `"${action.value}"`;
  return `Set ${action.table}.${action.field} to ${value} on the ${count}${sourceName} record${
    action.affected === 1 ? '' : 's'
  } where it is empty${action.filter ? ` and ${action.filter}` : ''}.`;
}

function writesToSystems(suggestion: ReadinessSuggestion): boolean {
  return suggestion.salesforce_actions.length > 0 || (suggestion.source_actions ?? []).length > 0;
}

function appliedSentence(
  sourceRecords: number,
  records: number,
  changes: number,
  included: string[],
  sourceName: string,
): string {
  const parts: string[] = [];
  if (sourceRecords) parts.push(`${sourceRecords} ${sourceName} record${sourceRecords === 1 ? '' : 's'} updated`);
  if (records) parts.push(`${records} Salesforce change${records === 1 ? '' : 's'}`);
  if (changes) parts.push(`${changes} mapping change${changes === 1 ? '' : 's'}`);
  if (included.length) parts.push(`${included.join(', ')} added to this migration`);
  return `Applied: ${parts.join(', ') || 'nothing needed changing'}. Run the compatibility check again to see the effect.`;
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

const STATUS_LABEL: Record<ReadinessStatus, string> = {
  covered: 'covered',
  partial: 'partly empty in the source',
  unmapped_with_candidate: 'not mapped, source has a candidate',
  no_source: 'no source data',
  parent_migratable: 'parent must load first',
  parent_creatable: 'parent can be created',
  parent_not_creatable: 'parent must already exist',
  capacity: 'not enough licences',
};

const panel: React.CSSProperties = {
  border: '1px solid #e5e5e5',
  borderRadius: 10,
  padding: 16,
  background: '#fafafa',
  marginTop: 20,
};

const summaryBox = (ready: boolean): React.CSSProperties => ({
  padding: 12,
  borderRadius: 8,
  background: '#fff',
  border: `1px solid ${ready ? '#d4d4d4' : '#a3a3a3'}`,
});

const chip = (muted: boolean): React.CSSProperties => ({
  fontSize: 11,
  fontWeight: 600,
  padding: '2px 8px',
  borderRadius: 10,
  background: muted ? '#f5f5f5' : '#e5e5e5',
  color: muted ? '#737373' : '#262626',
});

const gapCard: React.CSSProperties = {
  background: '#fff',
  border: '1px solid #e5e5e5',
  borderRadius: 8,
  padding: 12,
  marginBottom: 8,
};

const suggestionBox = (first: boolean): React.CSSProperties => ({
  marginTop: 10,
  padding: 10,
  borderRadius: 6,
  border: `1px solid ${first ? '#d4d4d4' : '#e5e5e5'}`,
  background: first ? '#fafafa' : '#fff',
});

const previewBox: React.CSSProperties = {
  marginTop: 8,
  padding: '6px 10px 8px',
  background: '#fff',
  border: '1px dashed #d4d4d4',
  borderRadius: 6,
};

const previewLabel: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
  color: '#737373',
};

const confirmBox: React.CSSProperties = {
  marginTop: 10,
  padding: 10,
  borderRadius: 6,
  border: '1px solid #737373',
  background: '#fff',
};

const badge: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 600,
  padding: '1px 7px',
  borderRadius: 4,
  background: '#f5f5f5',
  color: '#404040',
  border: '1px solid #e5e5e5',
  whiteSpace: 'nowrap',
};

const recommended: React.CSSProperties = {
  fontSize: 9.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
  color: '#fff',
  background: '#262626',
  padding: '1px 5px',
  borderRadius: 4,
  marginRight: 6,
  verticalAlign: 1,
};

const warnBox: React.CSSProperties = {
  marginTop: 10,
  padding: '9px 12px',
  background: '#fff',
  border: '1px solid #d4d4d4',
  borderRadius: 8,
  color: '#404040',
  fontSize: 12.5,
};

const errorBox: React.CSSProperties = {
  marginTop: 12,
  padding: '9px 12px',
  background: '#fff',
  border: '1px solid #a3a3a3',
  borderRadius: 8,
  color: '#171717',
  fontSize: 12.5,
};

const note: React.CSSProperties = {
  fontSize: 11.5,
  color: '#737373',
  margin: '10px 0 0',
};

const primaryBtn: React.CSSProperties = {
  padding: '8px 16px',
  background: '#171717',
  color: '#fff',
  border: 'none',
  borderRadius: 8,
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const applyBtn: React.CSSProperties = {
  padding: '5px 13px',
  background: '#171717',
  color: '#fff',
  border: 'none',
  borderRadius: 6,
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const ghostBtn: React.CSSProperties = {
  padding: '5px 11px',
  background: '#fff',
  border: '1px solid #d4d4d4',
  borderRadius: 6,
  fontSize: 12,
  color: '#404040',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const linkBtn: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  fontSize: 12.5,
  color: '#525252',
  cursor: 'pointer',
};
