---
name: git-branch-strategy-and-staging
description: Use when shipping any change to a deployed app — work on dev, demo on a per-branch staging URL, review the diff, then merge to main for auto-deploy. Never push to main directly.
---

# Git Branch Strategy and Free Per-Branch Staging

Treat `main` as production and `dev` as your playground: do all work, testing, and breaking on `dev`, and only merge to `main` once it works. Keep everything under Git so a broken deploy rolls back in 30 seconds with one command, and use free per-branch deploy URLs (dev → staging, main → production) as a real staging environment to test and demo before going live.

## When to use

- Any time you're about to change a deployed app — before the change, not after it breaks.
- Setting up a new project: get three environments (local, staging, production) wired in ~30 minutes.
- When you catch yourself editing code on the live server, doing manual deploys, or "deploying by prayer."

## The tactic

1. **Put everything under Git.** No Git means no rollbacks — you'd be comparing files manually and hoping you remember what worked yesterday. Version control isn't Git for its own sake; it's the ability to undo. Something breaks, roll back to the last working version in 30 seconds with one command.
2. **Branch strategy.** `main` = production, `dev` = your playground. Never push to `main` directly, ever. Work in `dev`, test in `dev`, break things in `dev`.
3. **Staging environment per branch.** Every branch gets its own deploy URL: `dev` → a staging URL, `main` → the production URL. Same code, same infrastructure, totally different audience. Test with staging, demo with staging, let your team break staging — then deploy production with total confidence.
4. **Deploy checklist (fixed flow):** test on staging → review the diff → merge to `main` → auto-deploy fires. That's it.
5. **No manual deploys, no FTP uploads, no editing code on the live server.** When it works on `dev`/staging, merge to `main` and let the pipeline do the rest.
6. **Run the CI/CD safety net.** CI/CD isn't fancy — it's an automated check: does the code build, do the tests pass, are there security vulnerabilities? If clean, deploy; if not, stop, alert, then fix. Without it, you can deploy code that doesn't even compile. It's invisible when it works and felt every time something breaks.

## Applied to our stack

- **Vercel hosting:** Vercel gives every branch its own deploy URL for free — this *is* your staging environment. Connect the repo, keep `main` as the Production branch, and treat `dev` (or any feature branch) as Preview/staging. Demo and break Preview before merging.
- **Next.js (App Router) + TypeScript:** the build itself is the first CI gate — `next build` / `tsc --noEmit` must pass before a Preview is shippable. Review the `git diff` against `main` before merging.
- **Supabase:** keep schema as SQL/Drizzle migrations in the repo so DB changes ride the same branch flow and can roll back, not hand-edited in the prod project. Validate migrations against a staging/branch DB before they hit production Postgres; never edit live data as your "deploy."
- **Inngest (background jobs/cron):** functions are code in the repo, so they version and roll back with the branch. Verify job/cron changes on the Preview/staging deploy before they run against production.
- **Stripe (incl. Connect):** exercise payment and Connect flows against test mode on staging, then merge so production picks up the change via auto-deploy — never patch billing logic on the live server.
- **Anthropic Claude API / agents:** prompt, agent, and MCP changes are code under Git — test them on the staging URL and roll back in one command if an agent change regresses, instead of editing live.
- **Rollback in practice:** because every change is a commit on `main`, reverting to the last working deploy is one Git command + auto-deploy — the 30-second rollback.

Applies to every project you deploy. This is the baseline discipline under the existing PR-and-worktree workflow.

## Source

Matt Murphy ([@mattmurphyai](https://www.instagram.com/mattmurphyai/))
- https://www.instagram.com/p/DYuyZmkRXye/ (Layer 7: CI/CD — no Git = no rollbacks)
- https://www.instagram.com/p/DYuVqaeRi8q/ (Three environments in 30 minutes: branch strategy + staging + deploy checklist)
