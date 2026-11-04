---
name: thermo-nuclear-code-quality-review
description: Use when asked for a thermo-nuclear, thermonuclear, harsh or deep code-quality or maintainability review, or when a lane branch is about to merge after several fix rounds and nobody has reviewed its structure as a whole.
---

# Thermo-nuclear code-quality review

Adapted from cursor-team-kit by Cursor (MIT, see LICENSE in this folder). Changed from upstream: report only, an 800-line threshold to match `~/.claude/rules/common/coding-style.md`, and correctness findings handed to the other reviewers.

## Overview

A demanding review of structure: abstraction, modularity, branching growth, file size. "It works" is not the bar. Look hard for a code-judo move: a reframing that keeps behaviour and deletes whole branches, helpers, modes or layers instead of rearranging them.

This is one lens. Correctness, money, authorization and security belong to the adversarial-reviewer agent and the cross-vendor audit; this review does not replace them.

## Report only

Do not edit the code. A restructuring is the lane owner's decision. Write each one as a proposal: what gets deleted or moved, where it goes, and a short sketch of the code after. Say whether it should block this merge or become its own task. Applying a restructure mid-lane widens a diff the other reviewers already checked.

## Presumptive blockers (the author must justify each)

- A file crosses 800 lines because of this change. Ask for decomposition first.
- New special-case branches in an existing flow (customer checks, one-off flags, nullable modes) where a table, policy object or typed model would remove them.
- The same condition written in several functions or files: a missing model.
- Thin wrappers, identity helpers, pass-through aliases.
- Muddy contracts: `any`, `unknown`, casts, `type: ignore`, optional parameters or loose dicts that hide the real invariant, silent fallbacks.
- Feature logic in a shared path or the wrong layer; a bespoke helper that duplicates a canonical one.
- Related updates that can leave state half-applied; independent work serialized for no reason.
- A refactor that moves complexity around without reducing what a reader must hold in their head.

## Questions for each change

- Is there a code-judo move that makes this much simpler?
- Does it improve or worsen the local architecture? Is the logic in the layer that owns the concept?
- Which repeated conditionals point at a missing model or helper?
- Is each abstraction earning its keep, or is it a wrapper?

## Output

1. **Verdict:** approve or request changes, on structural grounds. Passing tests and an unchanged golden do not approve. Approval here covers structure only and is never a merge approval: say so in the verdict line.
2. **Correctness, outside this lens:** one line each for any bug you tripped over, marked for the correctness reviewers. Keep them to one line so they don't crowd out the structure findings. If there are any, the verdict says the merge also waits on a correctness reviewer clearing each one.
3. **Structural findings,** in this order: structural regressions; missed dramatic simplification (show the after-shape); branching growth; boundary and type-contract problems; file size; modularity; legibility.

Prefer a few high-conviction findings to a long list. Skip cosmetic nits when structural issues exist. Be direct: if the change makes the code messier, say so.
