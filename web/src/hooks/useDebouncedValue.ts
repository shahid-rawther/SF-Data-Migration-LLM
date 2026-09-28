import { useEffect, useState } from 'react';

/** Pause, in ms, before a search box's value is acted on.
 *
 *  Short enough that a pause between words still feels immediate, long enough
 *  that the filtering does not run on every keystroke of a fast typist. */
export const SEARCH_DEBOUNCE_MS = 180;

/** Pause for searches that leave the browser. Longer, because each attempt is
 *  a request against the source or the org rather than a pass over an array. */
export const REMOTE_SEARCH_DEBOUNCE_MS = 400;

/**
 * Hold a fast-changing value still until it stops changing.
 *
 * The input keeps the live value so typing is never delayed; whatever the value
 * drives — filtering a long list, describing an object, querying an org — reads
 * the debounced copy instead. The second element is true while the two differ,
 * which is what tells the view its results are a keystroke behind.
 */
export function useDebouncedValue<T>(value: T, delay: number = SEARCH_DEBOUNCE_MS): [T, boolean] {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return [debounced, debounced !== value];
}
