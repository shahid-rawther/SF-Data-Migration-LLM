import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type {
  AgentDiagnosis,
  AgentMessage,
  AgentProposal,
  ProposalPreviewRow,
} from '../api/client';
import { FieldEntry } from './AgentFieldEntry';
import { AgentInline, AgentMarkdown } from './AgentMarkdown';

interface Props {
  sessionId: string;
  status: string;
  /** Re-read when the run reaches a point that could have changed the answer. */
  refreshKey?: number;
  onStarted: (status: string) => void;
}

/**
 * Where a failure nothing fixed automatically gets explained, and fixed.
 *
 * The deterministic rules and the repair agent only handle errors someone
 * anticipated. When rows are still rejected after them, the diagnosis agent
 * writes up why, with ranked fixes; this panel shows that, and lets the person
 * ask the agent anything or tell it what to change. The agent only ever
 * proposes: a change reaches a mapping when Apply is clicked, it is journalled
 * like every other agent change, and it can be reverted one field at a time.
 */
export function AgentAssistantPanel({ sessionId, status, refreshKey = 0, onStarted }: Props) {
  const [diagnoses, setDiagnoses] = useState<AgentDiagnosis[]>([]);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [proposals, setProposals] = useState<AgentProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [busyProposal, setBusyProposal] = useState<number | null>(null);
  const [busySuggestion, setBusySuggestion] = useState<string | null>(null);
  const [error, setError] = useState('');
  const threadEnd = useRef<HTMLDivElement>(null);

  const running = status === 'running';

  const load = useCallback(async () => {
    try {
      const [scoped, conversation] = await Promise.all([
        api.agents.diagnoses(sessionId),
        api.agents.chatHistory(sessionId),
      ]);
      setDiagnoses(scoped);
      setMessages(conversation.messages);
      setProposals(conversation.proposals);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not read the assistant conversation');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [messages.length, sending]);

  const proposalById = useMemo(
    () => new Map(proposals.map((proposal) => [proposal.id, proposal])),
    [proposals],
  );

  const send = async (text?: string) => {
    const message = (text ?? draft).trim();
    if (!message || sending) return;
    setSending(true);
    setError('');
    setDraft('');
    // Shown straight away; the stored copy replaces it on reload.
    setMessages((prev) => [
      ...prev,
      {
        id: -Date.now(),
        session_id: sessionId,
        role: 'user',
        content: message,
        proposal_id: null,
        created_at: new Date().toISOString(),
      },
    ]);
    try {
      await api.agents.chat(sessionId, message);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The agent could not answer');
    } finally {
      setSending(false);
      await load();
    }
  };

  const proposeSuggestion = async (diagnosis: AgentDiagnosis, index: number) => {
    const key = `${diagnosis.id}:${index}`;
    setBusySuggestion(key);
    setError('');
    try {
      const result = await api.agents.proposeFromDiagnosis(diagnosis.id, index);
      if (!result.proposal_id) {
        const reasons = (result.rejected || []).map((r) => r.reason).filter(Boolean);
        setError(
          `This fix no longer applies to the mapping${reasons.length ? `: ${reasons.join('; ')}` : '.'} ` +
            'Ask the agent below for another way.',
        );
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create the proposal');
    } finally {
      setBusySuggestion(null);
    }
  };

  const decide = async (proposal: AgentProposal, action: 'apply' | 'reject') => {
    setBusyProposal(proposal.id);
    setError('');
    try {
      if (action === 'apply') await api.agents.applyProposal(proposal.id);
      else await api.agents.rejectProposal(proposal.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : `Could not ${action} the proposal`);
    } finally {
      setBusyProposal(null);
      await load();
    }
  };

  const rerun = async (proposal: AgentProposal) => {
    setBusyProposal(proposal.id);
    setError('');
    try {
      await api.migrations.runRemaining(sessionId, proposal.rerun);
      onStarted('running');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the re-run');
    } finally {
      setBusyProposal(null);
    }
  };

  const clear = async () => {
    setError('');
    try {
      await api.agents.clearChat(sessionId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not clear the conversation');
    }
  };

  // Suggestions already turned into a proposal are not offered twice.
  const proposedTitles = useMemo(
    () => new Set(proposals.filter((p) => p.status !== 'rejected').map((p) => p.summary)),
    [proposals],
  );

  return (
    <section style={panel}>
      <header style={{ marginBottom: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <h3 style={{ margin: 0, fontSize: 14.5, fontWeight: 700, color: '#171717' }}>
            Ask the agent
          </h3>
          {messages.length > 0 && (
            <button type="button" onClick={clear} style={ghostButton} disabled={sending}>
              Clear conversation
            </button>
          )}
        </div>
        <p style={intro}>
          When rows are still rejected after the automatic repairs, the agent explains why
          and suggests fixes here. You can also ask it anything about this migration, or
          tell it what to change. It only proposes changes: nothing is written to a
          mapping until you click Apply.
        </p>
      </header>

      {error && <div style={errorBox}>{error}</div>}

      {loading ? (
        <div style={{ ...skeleton, height: 60 }} />
      ) : (
        <>
          {diagnoses.map((diagnosis) => (
            <DiagnosisCard
              key={diagnosis.id}
              diagnosis={diagnosis}
              busyKey={busySuggestion}
              proposedTitles={proposedTitles}
              onPropose={(index) => proposeSuggestion(diagnosis, index)}
              onAsk={(text) => send(text)}
              disabled={sending}
            />
          ))}

          {messages.length > 0 && (
            <div style={thread}>
              {messages.map((message) => {
                const proposal =
                  message.proposal_id != null ? proposalById.get(message.proposal_id) : undefined;
                return (
                  <div key={message.id} style={message.role === 'user' ? userBubble : agentBubble}>
                    <div style={roleLabel}>{message.role === 'user' ? 'You' : 'Agent'}</div>
                    {message.role === 'user' ? (
                      <div style={{ whiteSpace: 'pre-wrap' }}>{message.content}</div>
                    ) : (
                      <AgentMarkdown text={message.content} />
                    )}
                    {proposal && (
                      <ProposalCard
                        proposal={proposal}
                        busy={busyProposal === proposal.id}
                        running={running}
                        onApply={() => decide(proposal, 'apply')}
                        onReject={() => decide(proposal, 'reject')}
                        onRerun={() => rerun(proposal)}
                      />
                    )}
                  </div>
                );
              })}
              {sending && (
                <div style={agentBubble}>
                  <div style={roleLabel}>Agent</div>
                  <div style={{ color: '#737373' }}>
                    Investigating the mapping and the rejected rows…
                  </div>
                </div>
              )}
              <div ref={threadEnd} />
            </div>
          )}

          <div style={composer}>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={2}
              placeholder={
                'e.g. "Why are Incident rows failing?" or "Map close_code to Cancelled_Reason__c"'
              }
              style={input}
              disabled={sending}
            />
            <button
              type="button"
              onClick={() => void send()}
              style={primaryButton}
              disabled={sending || !draft.trim()}
            >
              {sending ? 'Working…' : 'Send'}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function DiagnosisCard({
  diagnosis,
  busyKey,
  proposedTitles,
  onPropose,
  onAsk,
  disabled,
}: {
  diagnosis: AgentDiagnosis;
  busyKey: string | null;
  proposedTitles: Set<string>;
  onPropose: (index: number) => void;
  onAsk: (text: string) => void;
  disabled: boolean;
}) {
  return (
    <div style={diagnosisBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={pill}>Diagnosis</span>
        <code style={{ fontSize: 12.5, fontWeight: 700, color: '#171717' }}>
          {diagnosis.mapping_name}
        </code>
        <span style={{ fontSize: 11, color: '#a3a3a3' }}>
          {new Date(diagnosis.created_at).toLocaleString()}
        </span>
      </div>
      <div style={{ marginTop: 8, fontSize: 13, color: '#262626' }}>
        <AgentMarkdown text={diagnosis.analysis} />
      </div>
      {diagnosis.evidence.length > 0 && (
        <ul style={evidenceList}>
          {diagnosis.evidence.map((line, idx) => (
            <li key={idx}><AgentInline text={line} /></li>
          ))}
        </ul>
      )}
      {diagnosis.suggestions.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={sectionLabel}>Suggested fixes</div>
          {diagnosis.suggestions.map((suggestion, index) => {
            const key = `${diagnosis.id}:${index}`;
            const actionable = suggestion.resolutions.length > 0;
            const proposed = proposedTitles.has(suggestion.title);
            return (
              <div key={key} style={suggestionRow}>
                <div style={{ flex: 1, minWidth: 220 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#171717' }}>
                    {index + 1}. <AgentInline text={suggestion.title} />
                  </div>
                  <div style={{ fontSize: 12.5, color: '#525252', marginTop: 2, lineHeight: 1.5 }}>
                    <AgentMarkdown text={suggestion.explanation} compact />
                  </div>
                </div>
                {actionable ? (
                  <button
                    type="button"
                    style={secondaryButton}
                    disabled={disabled || busyKey === key || proposed}
                    onClick={() => onPropose(index)}
                  >
                    {proposed ? 'Proposed below' : busyKey === key ? 'Preparing…' : 'Propose this fix'}
                  </button>
                ) : (
                  <button
                    type="button"
                    style={ghostButton}
                    disabled={disabled}
                    onClick={() =>
                      onAsk(
                        `For ${diagnosis.mapping_name}: how do I carry out "${suggestion.title}"? ` +
                          'If a mapping change can do it, propose it.',
                      )
                    }
                  >
                    Ask how
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProposalCard({
  proposal,
  busy,
  running,
  onApply,
  onReject,
  onRerun,
}: {
  proposal: AgentProposal;
  busy: boolean;
  running: boolean;
  onApply: () => void;
  onReject: () => void;
  onRerun: () => void;
}) {
  const pending = proposal.status === 'pending';
  return (
    <div style={proposalBox}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={pill}>Proposal</span>
        <span style={{ fontSize: 12.5, fontWeight: 600, color: '#171717' }}>
          <AgentInline text={proposal.summary || proposal.mapping_name} />
        </span>
        <span style={statusPill(proposal.status)}>{proposal.status}</span>
      </div>

      {proposal.preview.length > 0 ? (
        <table style={diffTable}>
          <thead>
            <tr>
              <th style={th}>Field</th>
              <th style={th}>Now</th>
              <th style={th}>After</th>
            </tr>
          </thead>
          <tbody>
            {proposal.preview.map((row, idx) => (
              <PreviewRow key={idx} row={row} />
            ))}
          </tbody>
        </table>
      ) : (
        <p style={{ margin: '6px 0 0', fontSize: 12.5, color: '#525252' }}>
          No mapping change — this only re-runs {proposal.rerun.join(', ')}.
        </p>
      )}

      {proposal.error && (
        <p style={{ margin: '6px 0 0', fontSize: 12, color: '#171717' }}>{proposal.error}</p>
      )}

      <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
        {pending && (
          <>
            <button type="button" style={primaryButton} disabled={busy || running} onClick={onApply}>
              {busy ? 'Applying…' : proposal.preview.length ? 'Apply' : 'Confirm'}
            </button>
            <button type="button" style={ghostButton} disabled={busy} onClick={onReject}>
              Reject
            </button>
          </>
        )}
        {proposal.status === 'applied' && proposal.rerun.length > 0 && (
          <button type="button" style={secondaryButton} disabled={busy || running} onClick={onRerun}>
            {running ? 'Running…' : `Re-run ${proposal.rerun.join(', ')}`}
          </button>
        )}
      </div>
      {pending && running && (
        <p style={{ margin: '6px 0 0', fontSize: 11.5, color: '#737373' }}>
          Wait for the current run to finish before applying.
        </p>
      )}
      {proposal.status === 'applied' && proposal.preview.length > 0 && (
        <p style={{ margin: '6px 0 0', fontSize: 11.5, color: '#737373' }}>
          Written to the mapping and journalled; each field can be reverted from the agent
          change log.
        </p>
      )}
    </div>
  );
}

function PreviewRow({ row }: { row: ProposalPreviewRow }) {
  return (
    <tr>
      <td style={td}>
        <code style={{ fontSize: 11.5, fontWeight: 700 }}>{row.field || row.mapping}</code>
        <div style={{ fontSize: 11, color: '#737373' }}>{row.kind.replace(/_/g, ' ')}</div>
      </td>
      <td style={td}>
        <FieldEntry value={row.before} />
      </td>
      <td style={td}>
        <FieldEntry value={row.after} />
      </td>
    </tr>
  );
}

const panel: React.CSSProperties = {
  padding: 14,
  marginBottom: 16,
  borderRadius: 10,
  background: '#fff',
  border: '1px solid #e5e5e5',
};
const intro: React.CSSProperties = {
  margin: '6px 0 0',
  fontSize: 12.5,
  color: '#737373',
  lineHeight: 1.5,
};
const errorBox: React.CSSProperties = {
  padding: '8px 10px',
  marginBottom: 8,
  borderRadius: 7,
  background: '#fafafa',
  border: '1px solid #d4d4d4',
  fontSize: 12.5,
  color: '#171717',
};
const skeleton: React.CSSProperties = {
  borderRadius: 8,
  background: 'linear-gradient(90deg, #f5f5f5 25%, #ebebeb 50%, #f5f5f5 75%)',
  backgroundSize: '200% 100%',
  animation: 'shimmer 1.5s ease-in-out infinite',
};
const diagnosisBox: React.CSSProperties = {
  padding: 12,
  marginBottom: 10,
  borderRadius: 8,
  background: '#fafafa',
  border: '1px solid #e5e5e5',
  borderLeft: '3px solid #171717',
};
const evidenceList: React.CSSProperties = {
  margin: '8px 0 0',
  paddingLeft: 18,
  fontSize: 12.5,
  color: '#404040',
  lineHeight: 1.55,
};
const sectionLabel: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: '#737373',
  marginBottom: 6,
};
const suggestionRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 10,
  flexWrap: 'wrap',
  padding: '8px 10px',
  marginBottom: 6,
  borderRadius: 7,
  background: '#fff',
  border: '1px solid #e5e5e5',
};
const thread: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  maxHeight: 520,
  overflowY: 'auto',
  padding: '4px 2px',
  marginBottom: 10,
};
const bubbleBase: React.CSSProperties = {
  padding: '8px 11px',
  borderRadius: 9,
  fontSize: 13,
  lineHeight: 1.55,
  maxWidth: '88%',
};
const userBubble: React.CSSProperties = {
  ...bubbleBase,
  alignSelf: 'flex-end',
  background: '#171717',
  color: '#fafafa',
};
const agentBubble: React.CSSProperties = {
  ...bubbleBase,
  alignSelf: 'flex-start',
  background: '#fafafa',
  color: '#262626',
  border: '1px solid #e5e5e5',
};
const roleLabel: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  opacity: 0.6,
  marginBottom: 2,
};
const proposalBox: React.CSSProperties = {
  marginTop: 8,
  padding: 10,
  borderRadius: 8,
  background: '#fff',
  border: '1px solid #d4d4d4',
  color: '#262626',
};
const diffTable: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  marginTop: 8,
  fontSize: 12,
};
const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '5px 6px',
  fontSize: 10.5,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: '#737373',
  borderBottom: '1px solid #e5e5e5',
};
const td: React.CSSProperties = {
  padding: '6px',
  verticalAlign: 'top',
  borderBottom: '1px solid #f5f5f5',
  wordBreak: 'break-word',
};
const composer: React.CSSProperties = { display: 'flex', gap: 8, alignItems: 'flex-end' };
const input: React.CSSProperties = {
  flex: 1,
  resize: 'vertical',
  padding: '8px 10px',
  border: '1px solid #d4d4d4',
  borderRadius: 8,
  fontSize: 13,
  minHeight: 42,
};
const primaryButton: React.CSSProperties = {
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
const secondaryButton: React.CSSProperties = {
  padding: '6px 12px',
  background: '#fff',
  border: '1px solid #d4d4d4',
  borderRadius: 8,
  fontSize: 12,
  fontWeight: 600,
  color: '#171717',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};
const ghostButton: React.CSSProperties = {
  padding: '4px 10px',
  background: '#fff',
  border: '1px solid #e5e5e5',
  borderRadius: 6,
  fontSize: 11.5,
  fontWeight: 600,
  color: '#525252',
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};
const pill: React.CSSProperties = {
  fontSize: 10.5,
  fontWeight: 700,
  color: '#404040',
  background: '#f5f5f5',
  padding: '2px 8px',
  borderRadius: 999,
  textTransform: 'uppercase',
  letterSpacing: 0.3,
};

function statusPill(status: string): React.CSSProperties {
  const tone =
    status === 'applied' ? '#171717' : status === 'pending' ? '#404040' : '#a3a3a3';
  return {
    fontSize: 10.5,
    fontWeight: 700,
    color: tone,
    border: `1px solid ${tone}`,
    padding: '1px 7px',
    borderRadius: 999,
    textTransform: 'uppercase',
    letterSpacing: 0.3,
  };
}
