/** A field mapping in the few lines that decide what it writes. */
export function FieldEntry({ value, maxChars = 220 }: { value: unknown; maxChars?: number }) {
  if (value == null) return <span style={{ color: '#a3a3a3' }}>not mapped</span>;
  if (typeof value !== 'object') return <code style={codeText}>{String(value)}</code>;
  const entry = value as Record<string, unknown>;
  const lines: [string, string][] = [];
  if (entry.source_field) lines.push(['from', String(entry.source_field)]);
  if (entry.constant_value != null && entry.constant_value !== '')
    lines.push(['constant', String(entry.constant_value)]);
  if (entry.transformation_function) lines.push(['transform', String(entry.transformation_function)]);
  if (entry.transformation_params && Object.keys(entry.transformation_params as object).length)
    lines.push(['params', JSON.stringify(entry.transformation_params)]);
  if (entry.skip_if_not_found) lines.push(['skip if unresolved', 'yes']);
  if (entry.defer_to_backfill) lines.push(['deferred', 'yes']);
  if (!lines.length) return <span style={{ color: '#a3a3a3' }}>direct copy</span>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      {lines.map(([label, text]) => (
        <div key={label} style={{ fontSize: 11.5, wordBreak: 'break-word' }}>
          <span style={{ color: '#737373' }}>{label}: </span>
          <code style={codeText}>{text.length > maxChars ? `${text.slice(0, maxChars)}…` : text}</code>
        </div>
      ))}
    </div>
  );
}

const codeText: React.CSSProperties = { fontSize: 11.5, color: '#171717' };
