---
name: handoff
description: End-of-session prep — writes HANDOVER.md (8 sections) to the project folder; next session reads that ONE file
---

# /handoff — write HANDOVER.md, hand the next session ONE thing

The end-of-session pattern ("save everything and write a handoff", "give me a stopping point and a prompt"). The 8-section HANDOVER.md pattern (P1, 2026-08-10): the outgoing session writes ONE structured `HANDOVER.md` into the project dir; the next session reads that one file and picks up. Nothing goes on the clipboard.

## Step 1 — Detect the project

In order: (1) cwd under `~/workspace/<name>` (or another project folder) → that project; (2) `$ARGUMENTS` names a project → use it; (3) project named in this conversation → confirm "**Handoff to <best-guess>? (Y/other)**"; (4) nothing → `~/Desktop/HANDOVER.md` and tell the operator.

## Step 2 — Write `HANDOVER.md` (the project dir; if one exists, rename it to `HANDOVER.prev.md` first)

Exactly 8 sections, filled ONLY from this conversation:

```markdown
# HANDOVER — <project> — <YYYY-MM-DD HH:MM CT>

## 1. Goal (what this project is driving at right now)
## 2. Done this session (concrete, verified — cite the output that proves it)
## 3. In flight (mid-task, unfinished)
## 4. Next step (the ONE first action; if the operator didn't say → `[NEEDS OWNER INPUT]` + options)
## 5. Decisions made (choice + why)
## 6. What to avoid (dead ends tried, constraints, do-not-touch)
## 7. Files touched + git state (real paths from this session's tool calls; last commit, branch, uncommitted?)
## 8. Open questions
```

## Step 3 — Tell the operator

One short block: `HANDOVER written → <absolute path>`. Next session opener is simply: **"Resume <project> — read HANDOVER.md first."** (That line may be shown in chat; do not put it or anything else on the clipboard.)

## Guardrails (mandatory)

- **Anti-hallucination:** every file listed must trace to a real tool call this session; never claim tests/deploys passed without the proving output line; don't invent next steps or agreement.
- **No secrets in the file** — reference `~/keys/<name>`.
- **Never push. Never auto-commit** (unless `$ARGUMENTS` has `--commit`: commit ONLY the HANDOVER file, conventional message, still never push).
- **No emojis.** Don't cross-reference other projects unless the operator did.
- SESSION-STATE.md stays the durable per-project resume file — if the session changed durable state, update it too; HANDOVER.md is the *this-session* baton on top of it.

## When NOT to use

Mid-session save-point → `/preserve`. Chat-only summary → `/compress`. Durable cross-project learning → `/remember`.
