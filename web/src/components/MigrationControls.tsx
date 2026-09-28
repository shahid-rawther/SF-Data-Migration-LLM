import { useState } from 'react';
import { api } from '../api/client';

interface MigrationControlsProps {
  sessionId: string;
  status: string;
  onStatusChange: (status: string) => void;
}

export function MigrationControls({ sessionId, status, onStatusChange }: MigrationControlsProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (action: 'start' | 'pause' | 'resume' | 'stop' | 'retry') => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.migrations[action](sessionId);
      onStatusChange(res.status);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      {status === 'pending' && (
        <button onClick={() => act('start')} disabled={loading} style={btnStyle('#171717')}>
          ▶ Start Migration
        </button>
      )}
      {status === 'running' && (
        <>
          <button onClick={() => act('pause')} disabled={loading} style={btnStyle('#525252')}>
            ⏸ Pause
          </button>
          <button onClick={() => act('stop')} disabled={loading} style={btnStyle('#0a0a0a')}>
            ⏹ Stop
          </button>
        </>
      )}
      {status === 'paused' && (
        <>
          <button onClick={() => act('resume')} disabled={loading} style={btnStyle('#171717')}>
            ▶ Resume
          </button>
          <button onClick={() => act('stop')} disabled={loading} style={btnStyle('#0a0a0a')}>
            ⏹ Stop
          </button>
        </>
      )}
      {(status === 'failed' || status === 'stopped') && (
        <button onClick={() => act('retry')} disabled={loading} style={btnStyle('#171717')}>
          ↻ Restart Migration
        </button>
      )}
      {loading && <span style={{ fontSize: 13, color: '#737373' }}>…</span>}
      {error && <span style={{ fontSize: 13, color: '#0a0a0a' }}>{error}</span>}
    </div>
  );
}

function btnStyle(bg: string): React.CSSProperties {
  return {
    padding: '8px 18px',
    background: bg,
    color: '#fff',
    border: 'none',
    borderRadius: 6,
    cursor: 'pointer',
    fontWeight: 600,
    fontSize: 14,
  };
}
