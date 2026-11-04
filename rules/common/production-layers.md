# The 13 Layers of Production (Planning Gate)

> Source: a voice note, "ALL 13 LAYERS OF CODING" (2026-06-07). The premise:
> most AI/"vibe"-coded apps ship 2 layers — a front end and a database, maybe
> auth — and silently skip 10+ others. **Those missing layers are exactly what
> separates a demo from a real product.** This file makes covering them a
> mandatory part of planning.

## THE RULE (mandatory, every project + every non-trivial feature)

During the **planning phase** — before writing implementation code — the plan
(PRD / plan doc / task list) MUST explicitly walk all 13 layers
below. For **each** layer, record one of:

- **Covered** — how it's handled (which tool/pattern/file), or
- **Inherited** — already provided by the existing stack/platform (name it), or
- **N/A** — deliberately out of scope, **with a one-line reason**.

No layer may be silently absent. A plan that doesn't address all 13 is not
ready to execute. Carry the 13-layer table into the plan doc and into the
task list so coverage is visible and reviewable.

> This composes with the existing workflow: see
> [development-workflow.md](./development-workflow.md) (Plan First),
> [security.md](./security.md) (layers 4/8/9), and
> [testing.md](./testing.md) (layer 7).

## The 13 Layers — coverage checklist

| # | Layer | Plan must answer |
|---|-------|------------------|
| 1 | **Front-end foundations** | UI framework, routing, state mgmt, responsive + a11y (WCAG), design system/tokens |
| 2 | **APIs & backend logic** | Endpoints / server actions, input validation at every boundary, business logic location, explicit error handling |
| 3 | **Database & storage** | Schema + migrations, indexing strategy, file/object storage, data retention |
| 4 | **Auth & permissions** | AuthN (login/SSO/claim flow), AuthZ (roles/scopes), session/token lifecycle |
| 5 | **Hosting & deployment** | Where it runs, build + deploy pipeline, env/config management, domains/DNS |
| 6 | **Cloud & compute** | Compute model (serverless/containers/edge), scaling primitives, infra-as-code |
| 7 | **CI/CD & version control** | Git workflow + branch protection, automated tests in CI, deploy gates, rollback path |
| 8 | **Security & RLS** | Tenant isolation / row-level security, secrets management, OWASP (XSS/CSRF/SQLi), authz checks server-side |
| 9 | **Rate limiting** | Per-endpoint / per-IP / per-user throttling, abuse + quota protection, spoofable-IP hardening |
| 10 | **Caching & CDN** | Response/data caching, CDN for static assets, cache-invalidation strategy |
| 11 | **Load balancing & scaling** | Horizontal scale, load balancing, autoscale triggers, concurrency/connection limits |
| 12 | **Error tracking & logs** | Structured logging, error tracking (e.g. Sentry), metrics + tracing, alerting/on-call |
| 13 | **Availability & recovery** | Backups + PITR, disaster recovery, health checks, failover, uptime/SLO targets |

## How to apply it

1. **In the plan doc / PRD:** include the table above with a filled-in
   "Covered / Inherited / N/A + why" cell per layer. The plan author must
   produce this section; treat a missing 13-layer section as an incomplete plan.
2. **In the task list:** any layer marked *Covered* that needs new work becomes
   a task. Layers marked *Inherited* name the existing mechanism (e.g. "RLS via
   Supabase policies", "rate limiting via existing middleware").
3. **In review:** the 13-layer table is part of the definition-of-ready. Flag
   any **N/A** that looks like an unexamined gap rather than a real decision.
4. **Scale sensibly:** a throwaway prototype may legitimately mark 6/8/9/10/11/13
   as N/A — but it must say so on purpose, not by omission. A production launch
   may not hand-wave the operational layers (8–13); that's the whole point.

## Applied to our stack

Default verdicts for the Next.js + Supabase + Vercel + Inngest + Stripe + Anthropic stack (confirm per project; don't assume):

1. **Front-end** — Covered: Next.js App Router + TypeScript + Tailwind + shadcn (GSAP/Lenis for marketing). Check a11y + responsive.
2. **APIs/backend** — Covered: route handlers / server actions. Validate every input at the boundary (zod); explicit error handling, no leaked internals.
3. **DB/storage** — Inherited: Supabase Postgres. Covered: SQL/Drizzle migrations, indexes, Supabase Storage for files.
4. **Auth** — Inherited: Supabase Auth. Covered: roles/scopes, session/token lifecycle, Stripe-customer linkage where billing-gated.
5. **Hosting/deploy** — Inherited: Vercel (build + deploy pipeline, env vars, domains/DNS). Note: some repos require commits authored as the deploy-bound email or Vercel blocks the deploy.
6. **Cloud/compute** — Inherited: Vercel serverless/edge. Covered: Inngest for background jobs/cron (don't run long work in request handlers).
7. **CI/CD** — Covered: git + branch protection, CI tests (lint/typecheck/unit), Vercel preview as deploy gate, rollback via Vercel/redeploy.
8. **Security/RLS** — Covered: Supabase RLS policies on every tenant table (own-row SELECT/INSERT/UPDATE), no RLS-OFF tables, secrets in env (never git-committed), server-side authz, OWASP. This is the layer most often faked — verify policies actually exist, not just that RLS is "on".
9. **Rate limiting** — Covered: per-endpoint/IP/user throttling on public + auth + webhook routes; harden against spoofable IPs.
10. **Caching/CDN** — Inherited: Vercel CDN for static/edge. Covered: response/data caching + invalidation where it matters.
11. **Load balancing/scaling** — Inherited: Vercel autoscaling. Covered: Supabase connection/pooling limits, Inngest concurrency caps.
12. **Error tracking/logs** — Covered: structured logging + error tracking (e.g. Sentry, wired on both client and server — confirm the backend instrumentation actually loads), alerting. AI code hides bugs here; assume nothing.
13. **Availability/recovery** — Inherited: Supabase backups/PITR + Vercel uptime. Covered: health checks, documented restore path, SLO target.

AI cost note (Anthropic API): when an agent/MCP/multi-agent path touches the LLM, treat it like layer 9 — set per-request and per-source token/budget caps before shipping the design-heavy path, or cost runs away.
