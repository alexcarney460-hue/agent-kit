---
name: insulate-third-party-apis
description: Wrap every external API in one adapter per provider, add a graceful fallback, and keep your own copy of the data so a vendor outage or breaking change can't take your app down.
---

# Insulate Third-Party APIs

Your app works great until the API changes, the rate limit kicks in, or the free tier disappears — and then it stops working overnight. The fix is to own the foundation: route every external call through one wrapper, build a fallback for when that API is down, and keep your own copy of the data that matters. Don't build your house on rented land — own the foundation, rent the features.

## When to use

- Any time the app depends on a third-party API: Stripe (incl. Connect), Anthropic Claude, Solana RPC, an SMS/email provider, or any vendor that can rate-limit or break under you.
- When you notice the same provider's calls scattered across many files — you're one breaking change away from rewriting the whole thing.
- When a vendor outage would currently leave a blank screen, a frozen UI, or lost customer data.

## The tactic

1. **Abstract your API calls.** Never call an external API directly from your main code. Wrap each provider in one function, in one file — one source of truth. When the API changes (Murphy's law: it will), you update it once, not in 40 scattered call sites.
2. **Build a fallback plan.** Ask: what happens when the API is down? If the answer is "my whole app breaks," you don't have an app — you have a wrapper around someone else's app you can't control. Build a graceful fallback: a cached response, a queue that retries, or even a message that says "back in 60 seconds." Anything beats a blank screen and silence.
3. **Own your data layer.** Your database should be yours, not theirs. If you store everything inside a third-party API and they shut down tomorrow, you lose everything — for you and your customers. Keep a copy of everything that matters in a database you control. APIs are rentals; your database is ownership. Know the difference.

## Applied to our stack

- **One adapter file per provider.** Put `lib/integrations/stripe.ts`, `lib/integrations/anthropic.ts`, `lib/integrations/solana-rpc.ts` so every call goes through one wrapper. No route, server action, or component imports the vendor SDK directly. When a provider changes, you fix the wrapper once.
- **Next.js (App Router):** server actions and route handlers call the adapter, never the raw SDK. The adapter is the natural home for retry-with-backoff and rate limiting (this tactic composes with both).
- **Supabase (Postgres):** this is your owned data layer. Mirror anything load-bearing — Stripe subscriptions/charges, Claude outputs you bill against, Solana balances/tx — into your own RLS-protected tables. If the vendor shuts down, you still have the data for you and your customers.
- **Inngest:** the retry queue for step 2. When an external call fails, enqueue an Inngest job that retries with backoff instead of failing the user's request inline.
- **Vercel:** keep a cached/last-known-good response (or a "back in 60 seconds" message) so a vendor outage degrades gracefully instead of returning a blank screen.
- **Stripe / Anthropic / Solana RPC:** especially load-bearing — these rate-limit or change under you. Route through the wrapper, mirror their data into Supabase, and never let a single vendor outage take the whole product down.

## Source

Matt Murphy (@mattmurphyai) — https://www.instagram.com/p/DYXGdH4AtnZ/
