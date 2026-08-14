"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useApi } from "@/lib/hooks";
import { api, BackendUnreachableError } from "@/lib/api";
import { StatusChip } from "./ui";

const NAV = [
  { href: "/patients", label: "Patients" },
  { href: "/analytics", label: "Analytics" },
  { href: "/ai-operations", label: "AI Operations" },
];

/** Header + page frame. The synthetic-prototype TopBanner lives in the root
 * layout so it is persistent on every screen including /login. */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const me = useApi(() => api.me(), []);

  async function logout() {
    try {
      await api.logout();
    } catch {
      /* logging out of an unreachable backend — just leave */
    }
    router.push("/login");
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-hairline bg-card">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
          <div className="flex items-center gap-10">
            <Link href="/patients" className="font-display text-2xl tracking-tight">
              CareLoop
            </Link>
            <nav className="flex items-center gap-6">
              {NAV.map((item) => {
                const active = pathname?.startsWith(item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`text-sm ${
                      active
                        ? "font-semibold text-ink"
                        : "text-ink-muted hover:text-ink"
                    }`}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            {me.data ? (
              <>
                <span className="text-sm text-ink-muted">
                  {me.data.clinician.name}
                </span>
                <button
                  onClick={logout}
                  className="text-sm text-ink-muted underline-offset-2 hover:text-ink hover:underline"
                >
                  Log out
                </button>
              </>
            ) : me.error instanceof BackendUnreachableError ? (
              <StatusChip tone="bad">Backend unreachable</StatusChip>
            ) : me.error ? (
              <Link href="/login" className="text-sm text-cta hover:underline">
                Sign in
              </Link>
            ) : null}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">{children}</main>
    </div>
  );
}
