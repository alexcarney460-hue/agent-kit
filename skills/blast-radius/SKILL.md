---
name: blast-radius
description: Use when asked "what could this break" or "blast radius of X" about a code change, or asked whether a small code change is safe to merge when it touches a shared helper, a key or id format, a serializer, a flag, stored data or another system.
---

# Blast radius

Adapted from pstack by Lauren Tan (MIT, see LICENSE in this folder).

## Overview

Find what a change breaks somewhere else, before it ships. Listing the callers is not the job; grep does that in a second. The job is the breakage grep won't show.

A writeup that sounds right is worthless on its own: it reads as convincing whether or not it is true. Find the one fact the verdict depends on and prove it by running code.

## Proof levels

Take each fact the verdict depends on as far down this list as is cheap, and say where it stopped.

1. You said so. Worthless alone.
2. You pointed at the line: a real `file:line`, or the library's own source.
3. You walked the failure step by step and showed it can't (or does) reach.
4. You ran it: a script or test that calls the real code on real data and fails loudly if you are wrong.
5. You reproduced it in the running app.

## Steps

1. **Read the change.** The diff, the symbols it adds, changes and deletes, and what now behaves differently, including what the diff doesn't spell out. Pull history with `gh pr view <n>` and `git log -p --follow <file>`.
2. **Find the deciding fact.** Most risky-looking changes are safe, or unsafe, because of a single fact ("this only drops cache entries that are already dead"). Spend your time there, not on a long list of maybes.
3. **Look where grep stops.** Library source at the pinned version and any local patch. When things run (teardown, retries, async ordering). Data already written under the old behaviour: database rows, stored keys, files, caches. Wire formats, partner feeds, another language reading the same bytes, feature flags, code three hops downstream. If stored data exists, run the new code against it.
4. **Rate each risk.** A real chance and a real cost. Keep confirmed risks apart from checked-and-cleared ones. Cite real `file:line`; a search that finds nothing is an answer; never invent a caller or an API.
5. **Prove the deciding fact.** Write the script in a scratch directory (never inside the repo under review), run it, paste the output.
6. **Big or wide change:** ask a second model the same question and merge the answers. For example `codex exec --sandbox read-only "<brief>"`, or `claude -p "<brief>"` if Claude Code is installed. Different models catch different bugs.

## What to hand back, in this order

- **Verdict:** safe, not safe, or safe only if a named condition holds.
- **What it does,** including the part that isn't obvious.
- **The deciding fact:** stated, the proof level reached, and the proof pasted. Write "unproven" if you could not prove it.
- **Risks:** how it breaks, `file:line`, how likely, how bad, how to check.
- **Cleared:** what you checked and why it is fine, as its own list.
- **Before you merge:** the cheapest test or repro that catches the real bug, with the script's path.

Output pasted from real data can carry customer names, ids or amounts. Before it goes on a card, a PR or anywhere off this machine, cut it to what the verdict needs and redact the rest.

## Common mistakes

- Trusting passing tests: check whether each test would still pass if the changed function returned something else.
- Reasoning about the code but never loading the data it reads.
- A long risk list with no deciding fact: the reader cannot tell which item decides the merge.
