import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { MigratableObject } from '../api/client';

export function useSourceObjects(sourceId: string | null) {
  const [objects, setObjects] = useState<MigratableObject[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sourceId) {
      setObjects([]);
      return;
    }
    setLoading(true);
    setError(null);
    api.sources
      .listObjects(sourceId)
      .then(setObjects)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [sourceId]);

  return { objects, loading, error };
}
