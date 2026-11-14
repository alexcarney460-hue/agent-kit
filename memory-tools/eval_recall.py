#!/usr/bin/env python3
"""eval_recall — private retrieval benchmark from OUR OWN corpus.

Public memory benchmarks are gamed and vendor-run; the only trustworthy number
is Recall@k against questions whose gold answers we control. Question kinds:

  direct       question written from a record, natural phrasing
  paraphrase   same, but sharing as little vocabulary as possible (the hard set
               — this is where lexical retrieval dies and hybrid must win)
  superseded   asks about a topic whose old row was expired by consolidation;
               gold = the CANONICAL row (recall must surface current truth)
  abstention   asks about things NOT in memory; reported as false-confidence
               rate, excluded from Recall@k

Usage:
  eval_recall.py --generate          # sample corpus, LLM writes questions
  eval_recall.py --run --label X     # score current retriever, append history
  eval_recall.py --run --no-vector --no-ppr --label baseline
Results append to eval-history.jsonl; questions live in eval-questions.json.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import re
import sqlite3
import model_cli
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memdir
from pathlib import Path

HM = memdir.hivemind_db()
CM = os.path.join(memdir.home(), ".claude-mem", "claude-mem.db")
MEMDIR = memdir.memory_dir()
QFILE = os.path.join(memdir.tools_dir(), "eval-questions.json")
HIST = os.path.join(memdir.tools_dir(), "eval-history.jsonl")

GEN_PROMPT = """You are building a retrieval benchmark. For EACH record below, write ONE question that this record (and ideally only it) answers. Alternate style: even-numbered records get a natural DIRECT question; odd-numbered get a PARAPHRASE question that shares AS FEW content words with the record as possible (use synonyms, role descriptions instead of names, etc.).

Records:
%s

Reply ONLY a JSON array, one object per record, same order:
[{"n": 1, "kind": "direct"|"paraphrase", "question": "..."}, ...]"""

ABSTENTION = [
    "what is the office wifi password",
    "which kubernetes cluster runs the checkout service",
    "what did the dentist say at the last appointment",
    "what is the AWS account id for the production VPC",
    "who won the office fantasy football league",
]


def lenient(conn):
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    return conn


def sample_records(n_hm=18, n_cm=14, n_file=8, n_sup=5, seed=3367):
    from contextlib import closing
    rng = random.Random(seed)
    out = []
    if os.path.isfile(HM):
        with closing(lenient(sqlite3.connect(Path(HM).resolve().as_uri() + "?mode=ro", uri=True))) as c:
            rows = c.execute("SELECT id, rule, COALESCE(context,'') FROM learnings "
                             "WHERE active=1 AND length(rule) > 60").fetchall()
            for rid, rule, ctx in rng.sample(rows, min(n_hm, len(rows))):
                out.append({"store": "hivemind", "ref": str(rid), "text": f"{rule} {ctx}"[:600]})
            rows = c.execute("SELECT l.rule, l.superseded_by FROM learnings l "
                             "JOIN learnings c ON c.id=l.superseded_by "
                             "WHERE l.superseded_by IS NOT NULL AND c.active=1").fetchall()
            for rule, canon_id in rng.sample(rows, min(n_sup, len(rows))):
                out.append({"store": "hivemind", "ref": str(canon_id),
                            "text": rule[:600], "kind_override": "superseded"})
    if os.path.isfile(CM):
        with closing(lenient(sqlite3.connect(Path(CM).resolve().as_uri() + "?mode=ro", uri=True))) as c:
            rows = c.execute("SELECT id, title, COALESCE(facts, SUBSTR(COALESCE(text,''),1,400)) "
                             "FROM observations WHERE length(COALESCE(title,'')) > 25").fetchall()
            for oid, title, facts in rng.sample(rows, min(n_cm, len(rows))):
                out.append({"store": "claudemem", "ref": str(oid), "text": f"{title} {facts}"[:600]})
    files = [f for f in os.listdir(MEMDIR) if f.endswith(".md") and f != "MEMORY.md"] if os.path.isdir(MEMDIR) else []
    for fn in rng.sample(files, min(n_file, len(files))):
        text = Path(MEMDIR, fn).read_text(encoding="utf-8", errors="replace")
        out.append({"store": "file", "ref": fn, "text": text[:600]})
    return out


def generate(seed=3367, qfile=QFILE):
    if not memdir.cloud_ok():
        print("Question generation requires cloud opt-in.")
        return
    records = sample_records(seed=seed)
    questions = []
    for i in range(0, len(records), 15):
        batch = records[i:i + 15]
        lines = "\n".join(f"{j + 1}. [{r['store']}:{r['ref']}] {r['text'][:400]}"
                          for j, r in enumerate(batch))
        output = model_cli.run_model(GEN_PROMPT % lines, 300)
        m = re.search(r"\[.*\]", output or "", re.S)
        if not m:
            sys.stderr.write(f"[gen] batch {i // 15} returned no JSON — skipped\n")
            continue
        try:
            arr = json.loads(m.group(0))
        except Exception:
            continue
        for item in arr:
            n = item.get("n")
            if not isinstance(n, int) or not (1 <= n <= len(batch)):
                continue
            r = batch[n - 1]
            questions.append({
                "question": str(item.get("question", ""))[:300],
                "gold": [[r["store"], r["ref"]]],
                "kind": r.get("kind_override") or item.get("kind", "direct"),
            })
    for q in ABSTENTION:
        questions.append({"question": q, "gold": [], "kind": "abstention"})
    questions = [q for q in questions if q["question"]]
    with open(qfile, "w", encoding="utf-8") as f:
        json.dump(questions, f, indent=1)
    kinds = {}
    for q in questions:
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
    print(f"wrote {len(questions)} questions: {kinds}")


def run(label: str, use_vector: bool, use_ppr: bool, k: int = 5, qfile: str = QFILE):
    import mem_recall
    scored = {"direct": [], "paraphrase": [], "superseded": []}
    false_conf = 0
    abst_n = 0
    questions = json.load(open(qfile, encoding="utf-8"))
    for q in questions:
        ranked = mem_recall.fused_search(q["question"], k=25,
                                         use_vector=use_vector, use_ppr=use_ppr)
        top = [(s, str(r)) for s, r, _ in ranked[:k]]
        if q["kind"] == "abstention":
            abst_n += 1
            if ranked and ranked[0][2] >= mem_recall.CONFIDENT_FUSED:
                false_conf += 1
            continue
        gold = {(s, str(r)) for s, r in q["gold"]}
        hit_rank = next((i + 1 for i, t in enumerate(top) if t in gold), None)
        scored[q["kind"]].append(hit_rank)
    result = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
              "label": label, "vector": use_vector, "ppr": use_ppr, "k": k}
    all_ranks = []
    for kind, ranks in scored.items():
        if not ranks:
            continue
        all_ranks += ranks
        result[f"recall@{k}_{kind}"] = round(sum(1 for r in ranks if r) / len(ranks), 3)
        result[f"n_{kind}"] = len(ranks)
    result[f"recall@{k}_all"] = round(sum(1 for r in all_ranks if r) / max(1, len(all_ranks)), 3)
    result["mrr"] = round(sum(1 / r for r in all_ranks if r) / max(1, len(all_ranks)), 3)
    result["false_confidence"] = round(false_conf / max(1, abst_n), 3)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")
    print(json.dumps(result, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--label", default="unlabeled")
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--no-ppr", action="store_true")
    ap.add_argument("--seed", type=int, default=3367)
    ap.add_argument("--qfile", default=QFILE)
    a = ap.parse_args()
    if a.dry_run:
        print("Dry run: no evaluation or model calls made.")
        return
    if a.generate:
        generate(seed=a.seed, qfile=a.qfile)
    if a.run:
        run(a.label, use_vector=not a.no_vector, use_ppr=not a.no_ppr, qfile=a.qfile)


if __name__ == "__main__":
    main()
