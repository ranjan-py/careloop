"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import { Card, EyebrowLabel, PillButton } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      router.push("/patients");
    } catch (err) {
      setError(describeError(err));
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-[calc(100vh-2rem)] items-center justify-center px-6">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <EyebrowLabel className="justify-center">
            Clinician sign-in
          </EyebrowLabel>
          <h1 className="mt-2 font-display text-5xl tracking-tight">CareLoop</h1>
          <p className="mt-3 text-sm text-ink-muted">
            Closed-loop clinical execution prototype. Local demo account only.
          </p>
        </div>
        <Card>
          <form onSubmit={submit} className="space-y-4">
            <label className="block text-sm">
              <span className="text-ink-muted">Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="maya.patel@careloop.demo"
                required
                autoComplete="username"
                className="mt-1 w-full rounded-lg border border-hairline-strong bg-card px-3 py-2.5 text-sm outline-none focus:border-cta"
              />
            </label>
            <label className="block text-sm">
              <span className="text-ink-muted">Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                className="mt-1 w-full rounded-lg border border-hairline-strong bg-card px-3 py-2.5 text-sm outline-none focus:border-cta"
              />
            </label>
            {error && (
              <p className="rounded-lg border border-bad/30 bg-bad-soft px-3 py-2 text-sm text-bad">
                {error}
              </p>
            )}
            <PillButton type="submit" disabled={busy} className="w-full">
              {busy ? "Signing in…" : "Sign in"}
            </PillButton>
          </form>
        </Card>
        <p className="mt-4 text-center text-xs text-ink-faint">
          Seeded demo credentials are in your local .env — nothing here is a
          real account.
        </p>
      </div>
    </main>
  );
}
