import { useEffect, useRef, useState } from 'react';
import type { ProgressEvent } from '../api/client';

export function useMigrationProgress(sessionId: string | null) {
  const [progress, setProgress] = useState<ProgressEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!sessionId) return;

    const es = new EventSource(`/api/migrations/${sessionId}/progress`);
    esRef.current = es;

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as ProgressEvent;
        setProgress(data);
      } catch {
        // ignore parse errors
      }
    };

    es.onerror = () => {
      setError('Lost connection to server');
      es.close();
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [sessionId]);

  const close = () => {
    esRef.current?.close();
    esRef.current = null;
  };

  return { progress, error, close };
}
