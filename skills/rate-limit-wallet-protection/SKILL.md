---
name: rate-limit-wallet-protection
description: When an endpoint calls a paid API (Anthropic/LLM), handles auth, or serves public data, cap requests per IP/user so one bot can't run up a four-figure bill or scrape/brute-force you.
---

# Rate-Limit AI, Auth, and Public Endpoints to Protect Your Wallet

A single rogue bot can hit an unprotected endpoint 10,000 times in an hour, and if that endpoint calls a paid API you get a four-figure invoice for calls nobody authorized. Rate limiting caps how many requests one user or IP can make in a time window — that doesn't slow real users (who make 2-5 requests/min), it stops the bots (which make hundreds). It's roughly a five-minute setup that can save you thousands.

## When to use

- Any route that calls a paid API (Anthropic/Claude, or any LLM) — these are the expensive ones.
- Authentication endpoints — to stop brute-force attacks.
- Public data endpoints — to stop scraper bots.
- Before launch, and any time you add a new endpoint in one of the three classes above.

## The tactic

1. **Cap requests per user or IP per window.** Pick a limit that fits your app — e.g. 50 per minute or 1,000 per hour. Real users sit at 2-5 req/min, so a sane cap is invisible to them and brutal to bots.
2. **Prioritize three endpoint classes first**, in order of damage:
   - AI/LLM endpoints (cost runaway — the ones that generate the invoice).
   - Authentication endpoints (credential stuffing / brute force).
   - Public data endpoints (scraping).
3. **Pick a tool you already have.** Every major framework has a rate-limiting library. Two fast paths:
   - Vercel's built-in rate limiting.
   - Upstash serverless Redis for custom per-user/per-IP limits.
4. **Key on the authenticated user where you can.** Client IPs are spoofable; an authenticated user id is a sturdier key for logged-in routes. Fall back to IP for anonymous traffic.
5. **Return the cap as a clean 429** (too many requests) rather than failing silently, so legitimate clients can back off.
6. **Ship it before something else does.** "Cap it before something else does" — this is the cheapest insurance you'll buy.

## Applied to our stack

- **Anthropic API routes (all projects):** the #1 priority. Any Next.js route/server action or Inngest job that calls Claude must sit behind a cap. This is the direct fix for the model-in-a-loop cost-runaway risk — set a per-user/per-IP limit on every generation endpoint.
- **Next.js (App Router):** apply limits in `middleware.ts` for blanket coverage, or inside individual route handlers / server actions for the three priority classes. Return `429`.
- **Vercel:** use Vercel's built-in rate limiting for the fastest path; no extra infra to run.
- **Upstash (serverless Redis):** when you need custom per-user/per-IP windows that persist across serverless invocations — the natural fit for distinct limits per endpoint class.
- **Supabase Auth:** put a cap in front of login/sign-up/password routes to blunt brute-force and credential stuffing before requests reach Supabase.
- **Inngest:** background jobs/cron that fan out to a paid API still spend money — bound concurrency/throughput there too, not just on the public-facing HTTP layer.
- **Stripe (incl. Connect):** treat checkout/webhook-adjacent and payout-touching endpoints as auth-class — cap and key on the authenticated user to deter abuse.

## Source

Matt Murphy (@mattmurphyai), "Layer 9 of 13 — Rate Limiting": https://www.instagram.com/p/DYz-1mDxTXN/
