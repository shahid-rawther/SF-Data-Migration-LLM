import React from 'react';
import type { SemanticSuggestReport } from '../api/client';

/**
 * The outcome of one model-assisted field matching run, rendered identically in
 * the wizard's table cards and the standalone mapping editor.
 *
 * It reports what was discarded as well as what was accepted: a proposal the
 * schema checks rejected is the most useful thing on screen when a run returns
 * fewer pairs than expected.
 */
export function SemanticMatchPanel({
  report,
  error,
}: {
  report: SemanticSuggestReport | null;
  error?: string;
}) {
  if (error) {
    return <div style={panel('#fafafa', '#e5e5e5', '#171717')}>{error}</div>;
  }
  if (!report) return null;

  return (
    <div style={panel('#fafafa', '#e5e5e5', '#171717')}>
      <strong>
        {report.matches.length > 0
          ? `${report.matches.length} match${
              report.matches.length === 1 ? '' : 'es'
            } added for review`
          : 'No matches found'}
      </strong>
      {report.summary && <p style={{ margin: '6px 0 0' }}>{report.summary}</p>}
      <p style={{ margin: '6px 0 0', color: '#737373' }}>
        Compared {report.considered_source_fields} unmapped source field(s) against{' '}
        {report.considered_destination_fields} writable destination field(s).
        {report.truncated && ' The lists were capped, so run again after accepting these.'}
      </p>
      {report.rejected.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer' }}>
            {report.rejected.length} proposal(s) discarded by schema checks
          </summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18, color: '#737373' }}>
            {report.rejected.map((r, i) => (
              <li key={i}>{r.reason}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

const panel = (
  background: string,
  border: string,
  color: string,
): React.CSSProperties => ({
  background,
  border: `1px solid ${border}`,
  color,
  borderRadius: 8,
  padding: '10px 12px',
  fontSize: 12,
  marginBottom: 12,
});
