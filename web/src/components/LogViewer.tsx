import { useEffect, useRef } from 'react';

interface LogViewerProps {
  lines: string[];
  maxLines?: number;
}

export function LogViewer({ lines, maxLines = 100 }: LogViewerProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [lines]);

  const displayLines = lines.slice(-maxLines);

  return (
    <div
      ref={ref}
      style={{
        background: '#0f172a',
        color: '#94a3b8',
        fontFamily: 'monospace',
        fontSize: 12,
        padding: 12,
        borderRadius: 8,
        height: 200,
        overflowY: 'auto',
        lineHeight: 1.6,
      }}
    >
      {displayLines.length === 0 ? (
        <span style={{ color: '#475569' }}>Waiting for migration to start…</span>
      ) : (
        displayLines.map((line, i) => (
          <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
            {line}
          </div>
        ))
      )}
    </div>
  );
}
