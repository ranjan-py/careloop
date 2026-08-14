"use client";

import { ApiError, BackendUnreachableError, describeError } from "@/lib/api";
import { GhostButton, StatusChip } from "./ui";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-hairline bg-card p-6 text-sm text-ink-muted">
      <span
        aria-hidden
        className="size-4 animate-spin rounded-full border-2 border-hairline-strong border-t-cta"
      />
      {label}
    </div>
  );
}

/**
 * Honest failure surface (spec §2.4) — no fake data fallbacks, ever.
 * Distinguishes "backend unreachable" from an API-level error.
 */
export function ErrorState({
  error,
  onRetry,
  title,
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}) {
  const unreachable = error instanceof BackendUnreachableError;
  const status = error instanceof ApiError ? error.status : undefined;
  return (
    <div className="rounded-2xl border border-bad/30 bg-bad-soft/60 p-6">
      <div className="flex items-center gap-2">
        <StatusChip tone="bad">
          {unreachable ? "Backend unreachable" : status === 401 ? "Not signed in" : "Request failed"}
        </StatusChip>
        {status !== undefined && (
          <span className="font-mono text-xs text-ink-muted">HTTP {status}</span>
        )}
      </div>
      <p className="mt-3 font-display text-lg text-ink">
        {title ?? (unreachable ? "The CareLoop backend did not respond." : "This data could not be loaded.")}
      </p>
      <p className="mt-1 text-sm text-ink-muted">{describeError(error)}</p>
      <p className="mt-1 text-xs text-ink-faint">
        Nothing is substituted in its place — this prototype never shows
        fallback data as if it were live.
      </p>
      {onRetry && (
        <div className="mt-4">
          <GhostButton onClick={onRetry}>Retry</GhostButton>
        </div>
      )}
    </div>
  );
}

export function DegradedNote({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 rounded-xl border border-warn/30 bg-warn-soft px-4 py-2 text-sm text-warn">
      <span aria-hidden className="size-1.5 rounded-full bg-warn" />
      {text}
    </div>
  );
}
