---
name: benchmark-checklist
description: Use when you are about to run or plan a benchmark, or measured a speed, latency, throughput or memory number and are about to report it, put it in a task card or report, or choose between servers, models, configs or libraries because of it. Also when someone asks for a quick headline speedup or "is X faster than Y".
---

# Benchmark checklist

Adapted from pstack by Lauren Tan (MIT, see LICENSE in this folder).

## Overview

A number reads the same whether or not it measured the work. Answer each question below with evidence from a run, not from reading the code, before the number goes on a card or into a decision.

## Before you run

- Write the sentence you expect to ship ("the candidate reads prompts 1.6x faster at one stream on the 40-row agent set"). The questions test that sentence.
- Read the measurement script: what it times, what it counts, what it ignores.
- Check the box is quiet (`uptime`, `nproc`, `nvidia-smi` on a GPU box). If it is busy and the other job cannot stop, move the run to a free machine (move work off a machine that can't do it) or interleave the two sides so both see the same noise, and say so.

## The questions

1. **Why not double?** Name the limiter: a core, the GPU, memory bandwidth, a lock, the disk, the network, or the load generator itself. Get it from a profile or counters in a run you do not report. If you cannot say why the number is not twice as good, you do not know what you measured.
2. **Was each side tuned?** Same flags, warmup, batching, cache state, context limits, versions and data as production. A side left on defaults means you compared configurations, not implementations. Tune it and rerun before picking a winner. If you cannot, there is no winner.
3. **Did it break limits?** Do the arithmetic against disk, network, core and GPU limits. A result past a limit measured something other than the work: a cache, a no-op or a bug. Removing a piece that takes 10% of the run can make it at most about 11% faster.
4. **Did it error?** Count non-success responses and check outputs are correct, not just present. Failed rows distort rates and medians: a fast rejection whose tokens still count inflates a rate, a slow timeout deflates it. Recompute both sides on the rows both answered.
5. **Does it reproduce?** At least 5 runs per side, alternating A, B, A, B. Report median and range. A gap smaller than the run-to-run spread is no measurable difference. When the call is close, use a rank-sum test or the harness's own statistics.
6. **Does it matter end to end?** Measure the path a user waits on, with realistic data sizes and the concurrency production sees (a one-stream result does not pick a server for many streams), and give the micro result as a share of it. A helper that takes 1% of a request can make the request at most 1% faster, however fast the helper gets.
7. **Did the work happen inside the timed region?** Confirm requests reached the server, rows were written and outputs were used between the timer's start and stop. Lazy code and timeouts produce numbers for work that never ran.

## Report

- The first words are the verdict: faster, slower, no measurable difference, or inconclusive.
- Then the number with unit, run count, range and limiter: "p50 41 ms to 33 ms, median of 7 runs per side, range 32 to 35 ms after, bound by JSON parsing on one core."
- The verdict is **inconclusive** when there was one run per side, a side was untuned, the limiter is unknown, the result is past a limit, or questions 4 and 7 were not checked. Name the gap and the exact rerun that would settle it.
- Keep runs, range, limiter evidence and commands in a file next to the result, and link it from the card.

## Pressure does not change the verdict

| Pressure | Answer |
|---|---|
| "Just give me the headline" | The verdict word is the headline. "Inconclusive: one run each, candidate on defaults" is a headline. |
| "We can't rerun" | Then it stays inconclusive. Write the rerun plan instead of a multiplier. |
| "It's only a quick ballpark" | For a ballpark someone asked for, one run is enough: check 4 and 7, skip the rest unless the run looks wrong, and give the number as "ballpark, one run". The verdict word stays inconclusive. A choice between options (open a lane, switch servers, promote a model) is never a ballpark. |
| "I labelled it preliminary" | "Preliminary: 1.5x faster" still gets quoted as 1.5x. Lead with the verdict word. |
