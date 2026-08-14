"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Minimal client-side data hook: fetch on mount (and when `key` changes),
 * expose honest loading / error / data states. No caching, no retries —
 * failures are rendered, not hidden (spec §2.4).
 */
export function useApi<T>(fetcher: () => Promise<T>, key: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetcherRef
      .current()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err);
          setData(null);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...key, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  return { data, error, loading, reload };
}

/** localStorage-backed string — used to hand encounter/patient ids to the
 * AI Operations view without a list route (CONTRACTS v1 has none). */
export function useStoredId(
  storageKey: string,
): [string, (value: string) => void] {
  const [value, setValue] = useState("");
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(storageKey);
      if (stored) setValue(stored);
    } catch {
      /* storage unavailable — fine */
    }
  }, [storageKey]);
  const update = useCallback(
    (next: string) => {
      setValue(next);
      try {
        window.localStorage.setItem(storageKey, next);
      } catch {
        /* storage unavailable — fine */
      }
    },
    [storageKey],
  );
  return [value, update];
}

export function rememberId(storageKey: string, value: string): void {
  try {
    window.localStorage.setItem(storageKey, value);
  } catch {
    /* storage unavailable — fine */
  }
}

export const STORAGE_KEYS = {
  lastEncounterId: "careloop.lastEncounterId",
  lastPatientId: "careloop.lastPatientId",
  lastCarePlanId: "careloop.lastCarePlanId",
};
