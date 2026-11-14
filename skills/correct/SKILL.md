---
name: correct
description: Use when the user or a reviewer corrects an agent for a mistake that has happened before in the same repo ("third time I've told an agent...", "stop doing X"), when a revert or review comment repeats an earlier one, or when a repo's AGENTS.md or CLAUDE.md keeps gaining "never do X" lines that get ignored.
---

# Correct

Adapted from pstack by Lauren Tan (MIT, see LICENSE in this folder).

## Overview

Assume every contributor is an agent that sees only the files it opened, copies the nearest example, and takes the shortest path that compiles. A prose rule fails silently when an agent skips it. Change the repo so the mistake can't be written, or fails loudly when it is.

## 1. Take a census of mistake classes

Read recent commits, reverts, review comments, the repo's agent file (AGENTS.md / CLAUDE.md), comments that explain workarounds, and our own record: the installed memory tools (`python "$HOME/.claude/memory-tools/mem_recall.py" "repo correction"`; use the Python that installed the kit, `python`, `py -3` or `python3`). Group what you find into classes and count each. A class counts once it has happened twice. A one-off gets named and no new check.

## 2. Pick the highest level that works, per class

1. **Architecture.** One owner per piece of state, one supported way per task. Hide internals so the wrong import fails. One source of truth instead of hand-synced lists. Delete old ways an agent would copy.
2. **Types, then a lint or CI check** whose error names the file, type or function to use instead. If the bad pattern is already common, fail only on additions.
3. **A behaviour test.** Fix or delete tests that would still pass if every function they call returned nothing.
4. **Docs or agent rules last,** only for judgement calls nothing can check.

Record why each higher level did not work.

## 3. Fix the named class now, card the rest

Fix the class the operator raised, and any others they asked for. For each other repeat class, do not change code: add an item to the project's task list (for example `tasks/todo.md`) with the evidence, the count, the level and the exact proposed check. This keeps "build only what's asked" while nothing found is lost.

## 4. Prove each check on a real past mistake

**The test.** A replay proves the check only if all of these hold, and you show each:
- It lives in a throwaway worktree (`../replay-<sha>`), never in your own checkout.
- Its files contain the real old bad lines, exactly as in `git show <bad-sha> -- <path>`, and the new check's code as on your fix branch. Read the files to confirm this; `git diff` can hide a mistake that is already committed.
- Running the same command CI runs inside it fails with the new check's own error, and every line the check reports is one of the replayed bad lines.
Anything else (a merge conflict, a missing file, an unrelated failure, extra reported lines, a clean pass) is an invalid replay, not the check firing.

**Usual ways to build it**, from the repo root, every write aimed at the replay:
1. `git worktree add --detach ../replay-<sha> <your-fix-branch>` (`--detach` because the fix branch is usually checked out already), then `git show <bad-sha> -- <path> | git -C ../replay-<sha> apply` (plain `git apply` changes nothing when it fails).
2. If that patch won't apply: `git worktree remove --force ../replay-<sha>`, then `git worktree add --detach ../replay-<sha> <old-fix-sha>^`, where `<old-fix-sha>` is the later commit that removed the mistake (its parent still contains it), and `git -C ../replay-<sha> checkout <your-fix-branch> -- <check files>`. If a check file also holds the old mistake, the overlay erases it: put the old bad hunk back into `../replay-<sha>/<that file>`, never into your checkout.
3. If the check only flags lines added since a base, choose the base so the diff from it to the replay adds the old bad lines: in way 1, commit the replayed mistake inside the replay and use that commit's parent; in way 2 the mistake is already committed, so use `<bad-sha>^`. Grandfathered additions since that base will also be reported; the test above then calls the replay invalid, so prefer way 1.
4. Afterwards `git worktree remove --force ../replay-<sha>` from the repo root, then show the check passes on your fix branch in your own checkout as usual.

A made-up probe is a fallback only when the history is gone, and you say so. Exceptions go on the offending line with a reason, an expiry date and the name of the human who approved it. Get that approval from the person before the exception lands, and never write it on their behalf; "approval pending" is not an exception, it is a failing check.

## 5. Keep the rule table

In the repo's agent file keep a table: rule, what enforces it, proof (commit or test). If the named class's rule already existed in prose and nothing enforced it, that is a repeat: enforce it in the same change and replace the prose with a pointer to the check. Other unenforced prose rules go on their class's card, not into this change. Drop a rule once its mistake can't happen.

## Reply

Each class with its evidence and count; the level picked and why a higher one didn't work; what was fixed now; what was carded (card keys); the pasted proof. Merging to main stays the operator's gate.

## Red flags

| Thought | Reality |
|---|---|
| "Reword the rule more strongly" | The rule existed and was ignored. That is the repeat. Go up a level. |
| "No time, add a line to AGENTS.md" | Prose takes the same time and fails silently. |
| "I proved it with a made-up example" | A synthetic probe tests your guess at the mistake. Replay the real one; a probe is allowed only when the history is gone, and you say so. |
| "Fix every class while I'm here" | Fix the named class; card the rest. |
