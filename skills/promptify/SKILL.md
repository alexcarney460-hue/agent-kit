---
name: promptify
description: Rewrite a rough voice-to-text idea into a detailed, paste-ready prompt for a fresh Claude Code session
---

# /promptify — Rough idea → paste-ready prompt

Takes a rough voice-to-text idea and rewrites it into a structured, paste-ready prompt for a fresh Claude Code session.

## Input

$ARGUMENTS — the user's rough text. If empty, ask: **"Paste the rough idea you want me to promptify."** Then wait for input.

## Step 1 — Classify intent

Read the rough text and assign ONE primary intent:

| Intent | Trigger signals |
|---|---|
| **research** | "look into", "is it safe", "tell me if", "deep research on tool/skill/repo", "thoughts on" |
| **idea-validation** | "is there an app that", "build a [thing] to make money", "wrap [thing]", "side hustle", "passive income" |
| **setup** | "set up X account", "connect to X", "configure", "give me access to", "API key" |
| **resume** | Project name mentioned + "pull up" / "continue" / "resume" |
| **build** | "build me a", "I want to build", concrete app/feature name, file path given |
| **fix/diagnose** | "broken", "not working", "diagnostics", "fix", "is X working" |
| **audit** | "review", "check my", "go through and audit", "clean up" |

If two intents tie at < 70% confidence, ask with AskUserQuestion (max 3 options, Recommended first).

## Step 2 — Resolve project context (when applicable)

If the rough text mentions a project, try to resolve it against the user's active projects. Check the user's CLAUDE.md / primer.md / project folders for context. If matched, read the latest handoff or state file (first 60 lines) for: current state, what's next, file paths.

## Step 3 — Apply the right template

### Template: research / audit a tool

```
Audit the "<tool/skill/repo>" before I install it.

What to do:
1. Find the repo on GitHub — give me the exact URL.
2. Check: license, last commit date, open issues, install/clone count, dependencies.
3. Read the install script and main entry file. Flag any prompt-injection, supply-chain, or secret-exfiltration risk.
4. Skim Reddit, HN, X for real user reviews from the last 6 months.
5. Cross-check against my installed skills/MCPs — flag overlaps.

Constraints:
- Star count is not security. Look at the code.
- Don't install yet. Report first.
- If you can't find something, say "no source" — don't guess.

Output:
- Verdict at top: SAFE / CAUTION / AVOID + 3-bullet reasons
- What it does in plain English
- Install command, only if SAFE
```

### Template: idea validation

```
Validate this idea before I spend time on it: <idea>

What to do:
1. Find 5-10 existing competitors. Pricing, install counts, reviews.
2. Identify the wedge: what could I do differently?
3. Realistic 12-month revenue ceiling — show your math.
4. Build cost in weeks.
5. Top 3 risks (legal, technical, market).

Constraints:
- Don't invent constraints (NDAs, patents) unless you have a source.
- If ceiling < $5K/mo or build > 4 weeks solo, flag as "save for later."

Output:
- GO / SAVE-FOR-LATER / KILL verdict at top
- One-page summary
- If GO: what to build first this week (< $1K spend)
```

### Template: setup / connect

```
Set up <service> and prove it works end-to-end.

What to do:
1. Walk me through creating the account (or use the one I already created — ask first).
2. Save the API key/secret to ~/.secrets/<service>, 600 perms. Never hardcode.
3. Make one real test call to prove the happy path works.
4. If a paid plan is needed, narrate the price before charging.
5. Add a short entry to my notes/primer describing what this is and where the secret lives.

Constraints:
- Ask before sending any email, SMS, or making any financial commit.
- If the setup fails at the same step 3 times, stop and tell me — don't loop.

Output:
- Confirmation: account live, secret saved, test call succeeded
- Next step (only if 70%+ confident I'd want it)
```

### Template: resume project

```
Resume <project name>.

Read first:
- <resolved project path>/SESSION-STATE.md (or equivalent)
- Latest HANDOFF-<date>.md if one exists
- Standing rules (CLAUDE.md, primer.md auto-load)

Then:
1. Tell me in 3 bullets: what's done, what's in flight, what's next.
2. Confirm the dev/build/test commands still work (run them).
3. Ask me ONE question: what's the goal of this session?

Constraints:
- Do NOT re-architect or redo finished work without my explicit OK.
- If a state doc conflicts with current code, trust the code and flag the doc.
```

### Template: build

```
Build: <goal in one line>

Context:
- Project: <resolved path or "new — propose folder location">
- Prior state: <handoff file ref or "starting fresh">

What to do:
1. <first concrete step from rough text>
2. <second step>
3. Test it works (run it, don't assume).
4. Commit at logical checkpoints with descriptive messages.
5. Report progress in plain English at each step.

Constraints:
- Take initiative: inspect, plan, build, test, fix, summarize.
- For 3+ independent parts, spawn parallel sub-agents.
- Ask before destructive ops (delete, force push, send money/messages).

Success criteria:
- <measurable "done" definition>
```

### Template: fix / diagnose

```
Diagnose: <what's broken in plain English>.

What to do:
1. Reproduce the problem with logs/output as evidence — don't guess.
2. Try up to 3 fixes before asking me.
3. After each fix, verify with a fresh test.
4. Once fixed, write the root cause in one sentence and the fix in one sentence.

Constraints:
- If the fix touches shared infrastructure (DNS, system settings, hardware), confirm with me first.
- If you can't reproduce it, say so — don't invent a cause.

Output:
- Root cause (1 line)
- Fix applied (1 line)
- How to prevent it next time (1 line)
```

### Template: audit / clean up

```
Audit <thing> and propose changes.

What to do:
1. Read <thing> end-to-end.
2. Flag: outdated content, contradictions, redundancy, unclear sections.
3. Propose specific edits with before/after diffs.
4. Do NOT edit yet — show me the diff first.

Constraints:
- Preserve my voice and intent — don't rewrite for "style."
- Don't delete anything load-bearing without explaining what would break.

Output:
- Summary: X issues found
- Per-issue: location, problem, proposed fix
```

## Step 4 — Output format

Wrap the response exactly like this:

```
**Intent:** <classified>
**Project:** <resolved name + path | "none — generic prompt">

Paste this into a fresh session:

═══════════════════════════════════════════════════════════════
<the rewritten prompt — filled-in template>
═══════════════════════════════════════════════════════════════

**What I added:** <1-2 lines on what scaffolding I added vs the raw input>
```

## Guardrails (mandatory)

- **Preserve the user's intent.** Don't add features, projects, or constraints they didn't mention.
- **Don't fabricate constraints.** Never inject NDAs, contracts, regulations, or named people unless they're in the user's standing notes.
- **Lean over bloated.** Target 150–350 words in the rewritten prompt. Fresh sessions auto-load CLAUDE.md — don't restate that context.
- **If the rough text is already a polished prompt**, tighten it — don't pad it longer.
- **Secrets in the rough text** (API keys, passwords): warn before including. Suggest the rewritten prompt point to ~/.secrets/<service> instead.
- **Always include** in the rewritten prompt: "If you can't find a source for something, say so. Don't guess."

## Common rough-text patterns

- "rewrite this prompt:" → process the text AFTER the colon
- "deep research on X" → classify as research, add web search to the toolset, end with "report back — don't act"
- "make me money" → classify as idea-validation, never build (validate first)
- Long voice-to-text rambles → extract the load-bearing sentence; cut filler before applying a template
