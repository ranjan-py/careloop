# CareLoop frontend

Next.js 15 (App Router, TypeScript, Tailwind v4) UI for the CareLoop demo.
See `../contracts/CONTRACTS.md` for the frozen API/WS contract this client
implements, and the repo README for the full demo story.

## Layout

- `src/lib/types.ts` — TS mirror of the contract core models (frozen half) plus
  best-effort shapes for models the contract names but does not pin.
- `src/lib/api.ts` — typed fetch client for every REST route. Calls go to
  same-origin `/api/*` and are proxied to the backend by the rewrite in
  `next.config.ts` (`BACKEND_URL`, default `http://localhost:8000`).
- `src/lib/ws.ts` — encounter WebSocket client (binary PCM out, JSON envelope
  events in, one-time ticket auth). Connects directly to the backend host
  (default `ws://<host>:8000`, override `NEXT_PUBLIC_WS_BASE`).
- `src/lib/audio.ts` — AudioWorklet microphone capture, downsampled in-browser
  to 16 kHz mono Int16 PCM (no MediaRecorder). Chrome on localhost only.
- `src/components/` — design system (Card, StatusChip, EyebrowLabel,
  SyntheticBadge, ActionCard, RejectionModal, QADrawer, TopBanner, states).
- Routes: `/login`, `/patients`, `/patients/[id]`, `/encounters/[id]/live`,
  `/encounters/[id]/care-plan`, `/encounters/[id]/summary`, `/analytics`,
  `/ai-operations`.

Every screen renders honestly with the backend absent — explicit
"backend unreachable" states, never fallback data (spec §2.4).

## Run

```bash
npm install
npm run dev    # http://localhost:3000, expects backend on :8000
npm run build  # production build (standalone output)
```

Docker: `Dockerfile` is the production multi-stage build (used by compose);
dev iteration uses `docker-compose.dev.yml` with a bind mount + `next dev`.
