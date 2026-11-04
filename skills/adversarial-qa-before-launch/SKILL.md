---
name: adversarial-qa-before-launch
description: Before launch, stop demoing the happy path to yourself — test a real device/browser matrix, hammer fields with dirty and oversized input, and silently watch 5 strangers use it.
---

# Adversarial QA Before Launch

A demo pretending to be a product impresses the people you show it to; a product serves people who get zero context. The gap between "works on my machine" and "works for everyone" is where the real bugs live — and your users find them first, then send uninstalls instead of bug reports. Close that gap with three deliberate adversarial passes before you ship.

## When to use

- Before any public launch, paid rollout, or investor/customer demo of a new product or feature.
- When you've only ever tested on your own machine, your own account, your own data — the happy path.
- After a vibecoded build, where the AI never tested apostrophes, oversized input, old devices, or Safari.

## The tactic

1. **Device/browser matrix.** You don't need a lab. Use BrowserStack's free tier, or grab two old phones from a drawer. Test on the worst device you can find: oldest browser, slowest connection, smallest screen. A six-year-old Android, an un-updated 2019 phone, mobile data, and Safari specifically — half your features can quietly break there. If it works on the worst device, it works everywhere.

2. **Edge-case / dirty input.** Your AI never tested these; your users will in the first hour.
   - Put an apostrophe in **every** field (names, search, everything) — it breaks databases and forms.
   - Paste 10,000 characters into a field built for 50.
   - Leave required fields empty and hit submit.
   - Hit the submit button 47 times (double/triple submit).
   - Paste emojis and weird characters into the search bar.
   - Upload an oversized file (e.g. a 200 MB upload).
   - Hit Back three times in the middle of a request.

3. **5 cold strangers, zero context.** Hand the app to 5 people who fit your target user — **not** your friends, your co-founder, or your mom. Say nothing, explain nothing, don't help. Watch where they tap, where they stall, where they give up. If they can't figure it out in 30 seconds, it's not ready. A demo needs context; a product never does.

4. **Add the boring stuff.** A product is a hundred boring things done right: error messages, loading states, empty states, password reset, terms-of-service pages. None of it is exciting; all of it is required.

Fix the crashes and confusion you find before users do — because a user who finds the bug uninstalls instead of filing a report.

## Applied to our stack

- **Next.js (App Router):** Test on real Safari/iOS and old Android, not just desktop Chrome. Verify loading/error/empty states exist (`loading.tsx`, `error.tsx`, empty-state UI). Confirm layouts hold on the smallest screen and that Back-button mid-request (route changes during a pending action) doesn't corrupt state.
- **Supabase (Postgres, RLS, Auth):** The apostrophe-in-a-name case is exactly the "their account doesn't even exist anymore" bug — make sure inserts are parameterized and the name field round-trips. Sign up a stranger with `O'Brien`, with emojis, with a 10,000-char bio. Test that RLS blocks one stranger from seeing another's row, not just that the happy-path user sees their own.
- **Vercel:** Use a per-branch preview deployment as the staging URL you hand to the 5 strangers and the old phones — never QA on production. Throttle the connection (slow 3G) against the preview to catch the slow-network failures.
- **Inngest (jobs/cron):** The "submit 47 times" / "form submits twice" cases hit background jobs hardest — verify job handlers are idempotent so a double-click doesn't fire the same email, charge, or export twice.
- **Stripe (incl. Connect):** Double-submit on checkout must not double-charge. Test declined cards, empty card fields, and an interrupted (Back-button) payment flow — not just one successful test card.
- **Anthropic Claude API + agents/MCP:** Paste 10,000 characters into any AI input to confirm you handle context limits and don't crash; paste emojis and adversarial prompts; verify timeouts and rate-limit errors surface a real error message, not a spinner that hangs forever.

## Source

Matt Murphy ([@mattmurphyai](https://www.instagram.com/mattmurphyai/)):
- https://www.instagram.com/p/DYhc3KSA_D1/
- https://www.instagram.com/p/DX2OR7nRQIP/
- https://www.instagram.com/p/DYVJUa3xSOu/
- https://www.instagram.com/p/DYZ1EeNgGIu/
