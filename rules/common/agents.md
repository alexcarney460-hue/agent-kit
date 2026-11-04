# Agent Orchestration

## Agents installed in `~/.claude/agents/`

| Agent | Purpose | When to use |
|-------|---------|-------------|
| code-reviewer | Review a finished step against the plan and coding standards | After writing or changing code |
| adversarial-reviewer | Try to REFUTE a "done" claim (auth, money, side effects, missing tests) | Before calling any high-stakes change complete |

Built-in agent types (Explore, Plan, general-purpose) cover search, planning and multi-step work.

## Rules

- Independent work runs in parallel: launch several agents in one message.
- A builder's "done" is a claim, not a fact. Pair important builds with a reviewer
  that did not write the code, and reconcile its verdict yourself.
- Keep worker models at the session model; do not downgrade builders to save cost.
- Give every sub-agent the WHY (what the result enables), the exact files/paths,
  and the verdict format you expect back.
