#!/usr/bin/env python3
"""mem_recall — one-call unified retrieval across all memory stores.

Replaces "the agent must know which store + the exact incantation". Fans a query
to: hivemind learnings, claude-mem observations, file memories (+ gbrain if its
CLI is present), ranks by relevance x reinforced-confidence, PINS protected
hits to the top, and (read-side reinforcement loop) bumps times_applied/last_applied
on surfaced hivemind rows and relevance_count on surfaced observations.

It sits on the SEMANTIC LAYER (sem.py):
  * the query resolves to canonical entities via aliases, so "Jamie Alexandra
    Rivera" and "Jamie Rivera" hit the same records;
  * records linked to a resolved entity are retrievable with ZERO lexical overlap
    with the query — the thing pure string matching cannot do. Those are marked ⊕;
  * claude-mem is searched through its FTS5 index instead of a LIKE table scan
    (~4x the recall on the same query, and it was already built).
The layer is optional: if semantic.db is missing or broken, recall degrades to the
previous lexical behaviour and says so.

NON-DESTRUCTIVE: only SELECTs + counter increments. No content is changed/removed.

Usage: mem_recall.py "your query" [--k 8] [--no-bump] [--no-semantic]
"""
import argparse, os, sqlite3, sys, re, math, datetime, time, random, shutil, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import sem
except Exception:  # layer absent -> lexical-only, never fatal
    sem = None

from memdir import memory_dir  # noqa: E402 (path inserted above)

HM = os.path.expanduser("~/hivemind.db")
CM = os.path.expanduser("~/.claude-mem/claude-mem.db")
MEMDIR = memory_dir()
PROTECT_RE = re.compile(r"client-a|project-b|partner-name", re.I)  # EDIT: keywords whose memories get pinned/protected — your ventures, clients, partners

# how hard an entity link outranks a raw keyword hit (one keyword hit == 2.0)
ENTITY_BOOST = 6.0

STOP = {"and","the","for","with","that","this","from","into","are","was","our","your","you",
        "how","what","when","which","who","does","did","not","but","all","any","its","via"}
def terms(q):
    return [w for w in re.findall(r"[a-z0-9]{3,}", q.lower()) if w not in STOP]

def conn(path):
    c = sqlite3.connect(path, timeout=10)
    c.execute("PRAGMA busy_timeout=5000;")
    # tolerate legacy non-UTF-8 bytes in old rows instead of crashing recall
    c.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    return c

def retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < tries - 1:
                time.sleep(0.05 * (2 ** i) + random.random() * 0.05); continue
            raise

def recency(iso_or_epoch):
    try:
        if iso_or_epoch is None: return 0.5
        if isinstance(iso_or_epoch, (int, float)):
            dt = datetime.datetime.fromtimestamp(iso_or_epoch / (1000 if iso_or_epoch > 1e12 else 1))
        else:
            dt = datetime.datetime.fromisoformat(str(iso_or_epoch)[:19])
        age = (datetime.datetime.now() - dt).days
        return 0.5 ** (max(0, age) / 120.0)
    except Exception:
        return 0.5

def hm_redirects():
    """{expired_id(str): active_root_id(str)} following superseded_by chains.
    The expired text is the old vocabulary for a live fact; recall credits the
    root. Cycle-safe; fail-open to empty."""
    try:
        c = conn(HM)
        state = {r[0]: (r[1], r[2]) for r in retry(lambda: c.execute(
            "SELECT id, active, superseded_by FROM learnings").fetchall())}
        c.close()
        red = {}
        for rid, (active, sup) in state.items():
            if active or sup is None:
                continue
            root, seen = sup, set()
            while root in state and root not in seen:
                seen.add(root)
                a, nxt = state[root]
                if a:
                    red[str(rid)] = str(root)
                    break
                root = nxt
        return red
    except Exception:
        return {}


def hivemind(q, tg, boosts):
    out = []
    try:
        c = conn(HM)
        rows = retry(lambda: c.execute(
            "SELECT id,category,rule,context,confidence,times_applied,created_at,last_applied,protected "
            "FROM learnings WHERE active=1").fetchall())
        # redirect credit: query terms matching an EXPIRED row's text score for
        # its active root (old phrasing -> current truth), lightly discounted
        redirect_extra = {}
        red = hm_redirects()
        if red and tg:
            exp = retry(lambda: c.execute(
                "SELECT id, rule, COALESCE(context,'') FROM learnings "
                "WHERE active=0 AND superseded_by IS NOT NULL").fetchall())
            for xid, xrule, xctx in exp:
                root = red.get(str(xid))
                if not root:
                    continue
                xhits = sum(f"{xrule} {xctx}".lower().count(t) for t in tg)
                if xhits:
                    rk = int(root)
                    redirect_extra[rk] = max(redirect_extra.get(rk, 0.0), xhits * 0.8)
        for rid, cat, rule, ctx, cf, th, cre, la, prot in rows:
            blob = f"{rule} {ctx or ''}".lower()
            hits = sum(blob.count(t) for t in tg)
            extra = redirect_extra.get(rid, 0.0)
            eb = boosts.get(("hivemind", str(rid)), 0.0)
            if hits == 0 and extra == 0.0 and eb == 0.0 and not (prot and PROTECT_RE.search(blob)):
                continue
            wgt = (cf or 0.5) * recency(la or cre) * (1 + math.log1p(th or 0))
            score = (hits + extra) * 2 + wgt + (5 if prot else 0) + ENTITY_BOOST * eb
            # grounded = real evidence (keyword, redirect, or entity); protected
            # floaters are display-only and must not vote in RRF fusion
            out.append((score, rid, prot, cat, (rule or "")[:160],
                        hits == 0 and extra == 0 and eb > 0,
                        hits > 0 or extra > 0 or eb > 0))
        c.close()
    except Exception as e:
        sys.stderr.write(f"[hivemind skipped: {e}]\n")
    out.sort(key=lambda r: -r[0])
    return out

def _cm_lexical(c, tg):
    """FTS5 when the index exists, else the old LIKE scan. Returns {id: row}."""
    rows = {}
    try:
        match = " OR ".join(f'"{t}"' for t in tg)
        r = retry(lambda: c.execute(
            "SELECT o.id,o.title,COALESCE(o.text,''),o.created_at_epoch,COALESCE(o.relevance_count,0) "
            "FROM observations_fts f JOIN observations o ON o.id=f.rowid "
            "WHERE observations_fts MATCH ? LIMIT 600", (match,)).fetchall())
        for x in r:
            rows[x[0]] = x
        return rows, "fts5"
    except sqlite3.Error:
        like = " OR ".join(
            ["lower(title||' '||COALESCE(text,'')||' '||COALESCE(facts,'')||' '||COALESCE(narrative,'')) LIKE ?"] * len(tg))
        r = retry(lambda: c.execute(
            f"SELECT id,title,COALESCE(text,''),created_at_epoch,COALESCE(relevance_count,0) "
            f"FROM observations WHERE {like} LIMIT 400", [f"%{t}%" for t in tg]).fetchall())
        for x in r:
            rows[x[0]] = x
        return rows, "like"

def suppressed_obs_ids():
    """Consolidation overlay: observation ids collapsed as duplicates.
    Lives in semantic.db so the plugin's DB stays untouched. Fail-open to none."""
    try:
        c = conn(os.path.expanduser("~/.claude/memory-tools/semantic.db"))
        ids = {r[0] for r in c.execute("SELECT obs_id FROM suppressed_observation")}
        c.close()
        return ids
    except Exception:
        return set()

def claudemem(q, tg, boosts):
    out, mode = [], "none"
    try:
        c = conn(CM)
        rows, mode = _cm_lexical(c, tg)
        sup = suppressed_obs_ids()
        for sid in sup:
            rows.pop(sid, None)
        lexical_ids = set(rows)
        # entity-linked observations, fetched even with zero lexical overlap —
        # the suppression overlay applies to this path too, or collapsed dups
        # resurface on exactly the entity queries the layer was built for
        ids = [int(ref) for (store, ref) in boosts if store == "claudemem" and str(ref).isdigit()]
        ids = [i for i in ids if i not in lexical_ids and i not in sup]
        for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
            marks = ",".join("?" * len(chunk))
            r = retry(lambda: c.execute(
                f"SELECT id,title,COALESCE(text,''),created_at_epoch,COALESCE(relevance_count,0) "
                f"FROM observations WHERE id IN ({marks})", chunk).fetchall())
            for x in r:
                rows[x[0]] = x
        for oid, title, text, ep, rel in rows.values():
            blob = f"{title} {text}".lower()
            hits = sum(blob.count(t) for t in tg)
            eb = boosts.get(("claudemem", str(oid)), 0.0)
            prot = 1 if PROTECT_RE.search(blob) else 0
            score = hits * 2 + recency(ep) + (1 + math.log1p(rel)) + (4 if prot else 0) + ENTITY_BOOST * eb
            out.append((score, oid, prot, (title or "")[:140], oid not in lexical_ids and eb > 0))
        c.close()
    except Exception as e:
        sys.stderr.write(f"[claude-mem skipped: {e}]\n")
    out.sort(key=lambda r: -r[0])
    return out, mode

def files(q, tg, boosts):
    out = []
    try:
        for fn in os.listdir(MEMDIR):
            if not fn.endswith(".md"): continue
            p = os.path.join(MEMDIR, fn)
            txt = open(p, encoding="utf-8", errors="ignore").read().lower()
            hits = sum(txt.count(t) for t in tg)
            eb = boosts.get(("file", fn), 0.0)
            if hits == 0 and eb == 0.0: continue
            prot = 1 if PROTECT_RE.search(fn + " " + txt[:400]) else 0
            out.append((hits + (5 if prot else 0) + ENTITY_BOOST * eb, fn, prot, hits, hits == 0 and eb > 0))
    except Exception as e:
        sys.stderr.write(f"[files skipped: {e}]\n")
    out.sort(key=lambda r: -r[0])
    return out

# ---------------------------------------------------------------------------
# Hybrid fusion: lexical + vector + graph signals merged by RRF.
# Rank-based fusion sidesteps the incomparable per-store score formulas.
# ---------------------------------------------------------------------------
RRF_K = 60
VECTOR_WEIGHT = 2.5      # one cross-store list vs three lexical; paraphrase gold
                         # is usually vector-ONLY, so it must survive being a
                         # single-signal item (see eval-history.jsonl v1-v3)
VECTOR_MIN_COS = 0.50    # CALIBRATED PER MODEL (qwen3-embedding:0.6b): real
                         # matches ~0.52-0.59, junk ~0.31-0.51. Re-measure with
                         # eval_recall.py if the embed model changes (nomic ran
                         # ~0.66+ real / ~0.59-0.67 junk — a 0.58 floor there)
CONFIDENT_FUSED = 0.05   # eval metric: what counts as a "confident" fused top-1


def _vector_list(query, top=50):
    """Ranked [(store, ref)] by cosine, or None (Ollama/index absent = fail open)."""
    try:
        import embed_index
        ec = embed_index.connect()
        hits = embed_index.search(ec, query, k=top)
        ec.close()
        if not hits:
            return None
        return [(s, str(r)) for s, r, score in hits if score >= VECTOR_MIN_COS]
    except Exception as e:
        sys.stderr.write(f"[vector skipped: {e}]\n")
        return None


def _ppr_list(matched, top=100):
    """Ranked [(store, ref)] by Personalized PageRank from resolved entities."""
    try:
        import ppr
        seeds = {}
        for m in matched:
            eid = getattr(m, "entity_id", None)
            if eid is not None:
                seeds[eid] = 1.0
        hits = ppr.ranks(seeds, top=top)
        if not hits:
            return None
        return [(s, str(r)) for (s, r), _score in hits]
    except Exception as e:
        sys.stderr.write(f"[ppr skipped: {e}]\n")
        return None


def fused_search(query, k=25, use_vector=True, use_ppr=True, no_semantic=False,
                 return_parts=False):
    """RRF across per-store lexical lists + vector + PPR. Returns ranked
    [(store, ref, fused_score)]; every ref is a str."""
    tg = terms(query)
    boosts, matched = ({}, [])
    if sem is not None and not no_semantic:
        try:
            boosts, matched = sem.recall_boosts(query)
        except Exception as e:
            sys.stderr.write(f"[semantic skipped: {e}]\n")
    hm = hivemind(query, tg, boosts)
    cm, cm_mode = claudemem(query, tg, boosts)
    fl = files(query, tg, boosts)
    lists = [
        (1.0, [("hivemind", str(r[1])) for r in hm if r[6]]),  # grounded only
        (1.0, [("claudemem", str(r[1])) for r in cm]),
        (1.0, [("file", str(r[1])) for r in fl]),
    ]
    if use_vector:
        v = _vector_list(query)
        if v:
            lists.append((VECTOR_WEIGHT, v))
    if use_ppr and matched:
        p = _ppr_list(matched)
        if p:
            lists.append((1.0, p))
    # supersession mapping: vector hits on hm_redirect rows and any
    # graph/lexical reference to an expired learning resolve to the active root
    red = hm_redirects()

    def canonical(key):
        s, r = key
        if s == "hm_redirect":
            root = red.get(r)
            return ("hivemind", root) if root else None
        if s == "hivemind" and r in red:
            return ("hivemind", red[r])
        return key

    fused = {}
    for weight, lst in lists:
        seen_keys = set()
        for rank, key in enumerate(lst):
            key = canonical(key)
            if key is None or key in seen_keys:
                continue  # dead redirect / same canonical already credited by this list
            seen_keys.add(key)
            fused[key] = fused.get(key, 0.0) + weight / (RRF_K + rank + 1)
    sup = {("claudemem", str(i)) for i in suppressed_obs_ids()}
    ranked = sorted(((s, r, sc) for (s, r), sc in fused.items() if (s, r) not in sup),
                    key=lambda x: -x[2])[:k]
    if return_parts:
        return ranked, {"hm": hm, "cm": cm, "cm_mode": cm_mode, "fl": fl,
                        "boosts": boosts, "matched": matched, "tg": tg}
    return ranked


def _fused_labels(ranked, parts):
    """Display labels for fused rows; lazily fetch ones only vector/PPR found."""
    labels = {}
    for s_, rid, prot, cat, rule, _e, _g in parts["hm"]:
        labels[("hivemind", str(rid))] = (prot, f"[{cat}] {rule[:110]}")
    for s_, oid, prot, title, _e in parts["cm"]:
        labels[("claudemem", str(oid))] = (prot, title[:110])
    for s_, fn, prot, _h, _e in parts["fl"]:
        labels[("file", str(fn))] = (prot, fn)
    missing_hm = [r for s, r, _ in ranked if s == "hivemind" and ("hivemind", r) not in labels]
    missing_cm = [r for s, r, _ in ranked if s == "claudemem" and ("claudemem", r) not in labels]
    try:
        if missing_hm:
            c = conn(HM)
            for rid, rule, cat, prot in c.execute(
                    f"SELECT id, rule, category, protected FROM learnings "
                    f"WHERE id IN ({','.join('?' * len(missing_hm))})", missing_hm):
                labels[("hivemind", str(rid))] = (prot, f"[{cat}] {(rule or '')[:110]}")
            c.close()
        if missing_cm:
            c = conn(CM)
            for oid, title in c.execute(
                    f"SELECT id, COALESCE(title,'') FROM observations "
                    f"WHERE id IN ({','.join('?' * len(missing_cm))})", missing_cm):
                labels[("claudemem", str(oid))] = (1 if PROTECT_RE.search(title) else 0, title[:110])
            c.close()
    except Exception:
        pass
    for s, r, _sc in ranked:
        labels.setdefault((s, r), (0, r))
    return labels


# query-keyword -> gbrain source id (isolated sources need explicit --source routing)
GB_SOURCES = {
    # OPTIONAL: map query keywords -> your local gbrain source ids, e.g.
    # "myproject": "myproject-brain",
}
def _gbrain_bin():
    return (shutil.which("gbrain") or
            next((p for p in (os.path.expanduser("~/.bun/bin/gbrain"), "/opt/homebrew/bin/gbrain")
                  if os.path.exists(p)), None))

GB_LOCK_HELP = ("PGLite lock squatted (stale `gbrain serve`?) — recover: "
                "pkill -f 'gbrain serve'; rm -f ~/.gbrain/.locks/*  "
                "(a small watchdog cron that reaps orphans is worth setting up)")

def gbrain(q, tg, slugs=()):
    """Returns (hits_text_or_None, error_text_or_None).

    Source routing now prefers entity slugs resolved by the semantic layer and
    falls back to the legacy keyword map.
    """
    exe = _gbrain_bin()
    if not exe: return None, None
    srcs = ["gstack-brain"]
    for key in list(slugs) + list(tg):
        s = GB_SOURCES.get(key)
        if s and s not in srcs:
            srcs.append(s); break
    hits, err = [], None
    for src in srcs[:3]:
        try:
            r = subprocess.run([exe, "query", q, "--source", src],
                               capture_output=True, text=True, timeout=35)
            if "Timed out waiting for PGLite lock" in (r.stdout + r.stderr):
                err = GB_LOCK_HELP
                break  # lock is held — every remaining source would burn 30s the same way
            for line in r.stdout.splitlines():
                m = re.match(r"\s*\[([0-9.]+)\]\s+(\S+)\s+--\s+(.*)", line)
                if m and not m.group(2).startswith("_archive/"):  # superseded memories stay out of recall
                    hits.append((float(m.group(1)), src, m.group(2), m.group(3)[:90]))
        except subprocess.TimeoutExpired:
            err = "gbrain query hung >35s (lock squat or Ollama down?)"
            break
        except Exception:
            continue
    hits.sort(reverse=True)
    if not hits: return None, err
    return "\n".join(f" [{s:.2f}] ({src}) {path} — {title}" for s, src, path, title in hits[:6]), err

def bump_hm(ids):
    if not ids: return
    try:
        c = conn(HM); now = datetime.datetime.now().isoformat(timespec="seconds")
        retry(lambda: c.executemany(
            "UPDATE learnings SET times_applied=times_applied+1, last_applied=? WHERE id=?",
            [(now, i) for i in ids]))
        c.commit(); c.close()
    except Exception as e:
        sys.stderr.write(f"[hm bump skipped: {e}]\n")

def bump_cm(ids):
    if not ids: return
    try:
        c = conn(CM)
        retry(lambda: c.executemany(
            "UPDATE observations SET relevance_count=COALESCE(relevance_count,0)+1 WHERE id=?",
            [(i,) for i in ids]))
        c.commit(); c.close()
    except Exception as e:
        sys.stderr.write(f"[cm bump skipped: {e}]\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--no-bump", action="store_true")
    ap.add_argument("--no-semantic", action="store_true", help="disable the entity layer (lexical only)")
    ap.add_argument("--no-vector", action="store_true", help="disable the embedding signal")
    ap.add_argument("--no-ppr", action="store_true", help="disable the PageRank signal")
    a = ap.parse_args()
    tg = terms(a.query)
    if not tg:
        print("no searchable terms"); return

    ranked, parts = fused_search(a.query, k=max(10, a.k), use_vector=not a.no_vector,
                                 use_ppr=not a.no_ppr, no_semantic=a.no_semantic,
                                 return_parts=True)
    boosts, matched, cm_mode = parts["boosts"], parts["matched"], parts["cm_mode"]
    hm = parts["hm"][:a.k]
    cm = parts["cm"][:a.k]
    fl = parts["fl"][:a.k]
    gb, gb_err = gbrain(a.query, tg, [m.slug for m in matched])

    def mark(p): return "🔒" if p else "  "
    def via(entity_only): return "⊕" if entity_only else " "

    print(f"=== mem_recall: {a.query!r}  (k={a.k}) ===")

    if ranked:
        labels = _fused_labels(ranked, parts)
        print(f"\n— TOP ACROSS STORES (RRF: lexical+entity"
              f"{'' if a.no_vector else '+vector'}{'' if a.no_ppr else '+ppr'}) —")
        for s, r, sc in ranked:
            prot, label = labels[(s, r)]
            print(f" {mark(prot)}[{sc:.3f}] {s}:{r}  {label}")

    if matched:
        print(f"\n— ENTITIES ({len(matched)}) —")
        for m in matched[:6]:
            print(f" {mark(m.protected)}{m.kind:>9}: {m.name}")
            if m.summary:
                print(f"              {m.summary}")
    elif sem is None:
        print("\n— ENTITIES: semantic layer not installed (lexical only) —")
    elif a.no_semantic:
        print("\n— ENTITIES: disabled via --no-semantic —")
    else:
        print("\n— ENTITIES: query resolved to none (add aliases with sem.py alias) —")

    print(f"\n— HIVEMIND learnings ({len(hm)}) —")
    for s, rid, prot, cat, rule, eonly, _g in hm:
        print(f" {mark(prot)}{via(eonly)}[{cat}] {rule}   (score {s:.1f}, id {rid})")
    print(f"\n— CLAUDE-MEM observations ({len(cm)}, via {cm_mode}) —")
    for s, oid, prot, title, eonly in cm:
        print(f" {mark(prot)}{via(eonly)}{title}   (score {s:.1f}, obs {oid})")
    print(f"\n— FILE memories ({len(fl)}) —")
    for s, fn, prot, hits, eonly in fl:
        print(f" {mark(prot)}{via(eonly)}{fn}   ({hits} hits)")

    # Actual files on disk: the drafts, briefings, receipts and exports that
    # memory used to describe but never point at.
    arts = sorted(((w, ref) for (store, ref), w in boosts.items() if store == "artifact"),
                  reverse=True)[:a.k]
    if arts:
        home = os.path.expanduser("~")
        print(f"\n— ARTIFACTS on disk ({len(arts)}) —")
        for w, ref in arts:
            print(f"    [{w:.2f}] {ref.replace(home, '~')}")
    if gb:
        print(f"\n— GBRAIN semantic (hybrid query over project sources) —\n{gb}")
    elif _gbrain_bin() is None:
        print("\n— GBRAIN: CLI not installed —")
    elif gb_err:
        print(f"\n— ⚠ GBRAIN UNAVAILABLE: {gb_err} —")
    else:
        print("\n— GBRAIN: no semantic hits for this query —")

    eonly_n = sum(1 for r in hm if r[5]) + sum(1 for r in cm if r[4]) + sum(1 for r in fl if r[4])
    if eonly_n:
        print(f"\n[⊕ = {eonly_n} record(s) retrieved by ENTITY with no keyword overlap]")

    if not a.no_bump:
        bump_hm([r[1] for r in hm]); bump_cm([r[1] for r in cm])
        print(f"[reinforced {len(hm)} learnings + {len(cm)} observations]")

if __name__ == "__main__":
    main()
