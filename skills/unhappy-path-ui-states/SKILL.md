---
name: unhappy-path-ui-states
description: For every network-touching component, build all four states (loading/error/empty/success), wrap external calls in try/catch with a retry button, and retry with exponential backoff instead of freezing or showing a blank screen.
---

# Unhappy-Path UI States

Your app handles the happy path: card goes through, data saves, confetti pops. But when the card declines you get a blank screen, silence, and the customer's gone. The UI is the easiest part — AI nails it in 10 minutes — but production front end isn't a pretty page, it's a system. This skill is the executable detail behind "Layer 1": build for everything else.

## When to use

- Any component that fetches or mutates data over the network (payments, APIs, DB writes).
- Right after a vibecoded screen "looks done" — before you trust it on a real device, real connection, real users.
- Anywhere a failure currently produces a frozen UI, an infinite spinner, or a blank screen.

## The tactic

1. **Try/catch every external call.** Every payment, every API call, every database write — wrap it, catch it, handle it. On failure, show the user what happened (being honest always wins) and give them a retry button. Don't just freeze the UI.
2. **Build all four states for every UI component, no exceptions.** Loading state, error state, empty state, success state. Every component. The user should never see a blank screen, ever.
3. **Add retry logic with exponential backoff.** First retry 1s, second retry 2s, third retry 4s. Then fail gracefully with a very clear user message. Don't hammer the server, don't freeze the UI — give the user feedback every step of the way.
4. **Add boundaries that catch crashes** without taking down the whole app, so one component's failure doesn't blank the whole page.

## Applied to our stack

- **Next.js (App Router):** Use the built-in `loading.tsx` and `error.tsx` (with its `reset()` retry) per route segment; add an `<ErrorBoundary>` around client components so one crash doesn't take down the page. In Server Components, model the empty state explicitly (`if (rows.length === 0)`), not just success.
- **Supabase:** Every `supabase.from(...)` / RPC returns `{ data, error }` — branch on all four: loading (pending), `error` set, `data` empty array, `data` present. Treat an RLS denial or auth-expired as an error state with a retry, not a silent empty list.
- **Stripe:** Wrap charge/Checkout/Connect calls in try/catch. On a declined card, surface the decline reason and a retry button instead of a blank screen. Never auto-retry a charge with backoff (that risks double-charging) — retry is for idempotent reads.
- **Inngest:** Background steps already get retries; mirror that on the client. Show a "processing" state while a job runs, poll/subscribe for completion, and surface a clear failure state with retry if the job ends in error.
- **Anthropic Claude API:** Wrap calls in try/catch and apply exponential backoff (1s → 2s → 4s) on `429`/`529`/overloaded and timeouts. Keep a visible loading state during streaming; on final failure show a clear message and a retry button rather than a hung spinner.
- **Vercel:** Serverless functions have a wall-clock timeout — a slow upstream (Stripe, Claude, a heavy query) can exhaust it and the request dies. Set a client fetch timeout shorter than the function limit so a hung call surfaces as a clear timeout error state (with retry) instead of a frozen UI waiting on a request that will never return.

Backoff helper sketch (reads only; never for non-idempotent writes like Stripe charges):

```ts
async function withBackoff<T>(fn: () => Promise<T>, delays = [1000, 2000, 4000]) {
  for (let i = 0; ; i++) {
    try {
      return await fn();
    } catch (err) {
      if (i >= delays.length) throw err; // fail gracefully -> show clear message
      await new Promise((r) => setTimeout(r, delays[i]));
    }
  }
}
```

## Source

Matt Murphy (@mattmurphyai)
- https://www.instagram.com/p/DYfDSOzgC-x/
- https://www.instagram.com/p/DYfZI0qx9xF/
