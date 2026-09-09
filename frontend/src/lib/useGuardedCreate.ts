import { useEffect, useRef, useState, type FormEvent } from 'react';

/** Keep editable form input until the exact submitted draft succeeds. */
export function useGuardedCreate(draft: string, create: () => Promise<void>, clear: () => void) {
  const currentDraft = useRef(draft);
  const pending = useRef(false);
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { currentDraft.current = draft; }, [draft]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError('');
    const submitted = draft;
    try {
      await create();
      if (mounted.current && currentDraft.current === submitted) clear();
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return { submit, busy, error };
}
