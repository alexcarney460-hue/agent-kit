---
name: agent-governance-and-boundaries
description: Before scaling Claude agents/sub-agents/MCP across real workflows, define ownership, context location, and hard red lines — govern first, then ship.
---

# Agent Governance and Boundaries

Before you add agents to a production workflow or grow a swarm, design the architecture, not just the prompts: who owns which decisions, where context lives, exactly what each agent may touch, and explicitly what it must NOT touch. Route agent actions through one orchestration layer that gives you visibility and a human override at every step. Assess first, then build, then ship — in that order.

## When to use

- Before deploying or scaling Claude agents, sub-agents, MCP, or swarms against real data or paying users.
- When agents will fire side effects: payments, deletions, prod writes, emails, on-chain actions.
- When you feel the pull to "drop AI in first" on top of messy data, undefined ownership, or tribal workflows. Pause: are you about to automate dysfunction?

## The tactic

1. **Pause before scaling.** Ask: are we actually ready to scale intelligence, or about to automate dysfunction? AI doesn't fix broken systems — it amplifies them. Messy data, broken workflows, undefined ownership, and no governance get worse, not better, when agents touch them.
2. **Normalize reality first.** Clean the data, standardize the workflow, and define ownership before any agent runs against it. Agents are workers; they need clean factories.
3. **Codify decisions.** Write down what gets decided and by whom. Put the decision rights in writing — clarity over chatter — so the agent inherits explicit rules, not tribal knowledge.
4. **Answer the four governance questions in code/config, not vibes:** Who owns the decisions? Where does context live? What can agents touch? And most importantly, what can agents *not* touch?
5. **Define red lines before production.** Name the actions no agent may take on its own — payments, deletions, prod writes. Embed these as guardrails in the workflow, not as a policy buried in a doc.
6. **Route through one orchestration layer.** Every agent action goes through a single mission-control point that gives you visibility and a human override at every step — instead of agents firing side effects directly.
7. **Start narrow, governed, accountable.** Assess it all first, then build it, then ship it. Begin with one bounded job and expand only after the boundaries hold. Teams that win aren't the ones deploying the most agents fastest — they're the ones whose governance can control them. (OutSystems' 2026 AI report: 94% of enterprises are concerned about agent sprawl and 88% reported a suspected or confirmed AI agent security incident.)
8. **Encode the boundaries up front, or the agents design around you.** If you don't design for governance originally, it will design right around you.

## Applied to our stack

- **Ownership + decision map:** Before a swarm touches a live app, write down which agent owns which decision and which require a human. Keep it in the repo (e.g. `AI-CATALOG.md`), versioned, not in your head.
- **Context location:** Be explicit about where each agent reads/writes context (Supabase tables, files, MCP) — one source of truth per concern, not five disparate places.
- **What agents may touch (Supabase):** Scope agents to their own service-role boundaries. Lean on RLS so a wrong agent can't read/write the wrong tenant's rows. Read paths and write paths are separate grants, not one god-key.
- **Hard red lines (Stripe + deletions):** Charges, refunds, Connect payouts, and destructive DB writes are never agent-autonomous. Gate them behind a human approval step — the agent proposes, a person confirms.
- **Orchestration layer (Inngest):** Run agent side effects as Inngest jobs so every action is logged, retryable, and visible — your mission-control surface with an override (cancel/pause) at every step, instead of agents calling Stripe/Supabase inline.
- **Anthropic agents/MCP:** Give each sub-agent the minimum tool set and minimum MCP scopes for its one job. Define red lines (no prod writes, no payments, no on-chain actions) in the system prompt AND enforce them in the tool layer — prompts alone are not a control.
- **Vercel/prod writes:** Agents propose diffs and deploys; a human merges to main and promotes. Never let an agent push straight to production.
- **Sequence:** assess (data + ownership + red lines) → build (narrow, governed) → ship (one bounded job, expand once boundaries hold).

## Source

Matt Murphy (@mattmurphyai)
- https://www.instagram.com/p/DTQLla7gNl-/
- https://www.instagram.com/p/DXfm6qpjWq-/
- https://www.instagram.com/p/DTxkznYgA1a/
- https://www.instagram.com/p/DUBL1B7DXkk/
- https://www.instagram.com/p/DTSsYhKDSpY/
