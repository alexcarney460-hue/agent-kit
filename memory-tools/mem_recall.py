#!/usr/bin/env python3
"""Search local memories using lexical, optional vector and graph signals."""
import argparse, os, sqlite3, sys, re, math, datetime, time, random, shutil, subprocess
import heapq
from contextlib import closing
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_policy
import memdir
import json
try:
    import sem
except Exception:  # layer absent -> lexical-only ranking, never fatal
    sem = None

HM = graph_policy.HIVEMIND_DB
CM = graph_policy.CLAUDEMEM_DB
MEMDIR = graph_policy.MEMDIR
def _protected_pattern():
    try:
        with open(os.path.join(memdir.tools_dir(), "protected-topics.json"), encoding="utf-8") as f:
            topics = json.load(f)
        if not isinstance(topics, list):
            return re.compile(r"(?!)")
        topics = [re.escape(item) for item in topics if isinstance(item, str) and item.strip()]
        return re.compile("|".join(topics) if topics else r"(?!)", re.I)
    except (OSError, ValueError):
        return re.compile(r"(?!)")

PROTECT_RE = _protected_pattern()

# how hard an entity link outranks a raw keyword hit (one keyword hit == 2.0)
ENTITY_BOOST = 6.0

STOP = {"and","the","for","with","that","this","from","into","are","was","our","your","you",
        "how","what","when","which","who","does","did","not","but","all","any","its","via"}
def terms(q):
    return [w for w in re.findall(r"[a-z0-9]{3,}", q.lower()) if w not in STOP]

def conn(path):
    c = sqlite3.connect(path, timeout=10)
    c.execute("PRAGMA busy_timeout=5000;")
    # Tolerate legacy non-UTF-8 bytes instead of crashing recall.
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
    """{expired_id(str): active_root_id(str)} following superseded_by chains
    (local3381). The expired text is the old vocabulary for a live fact; recall
    credits the root. Cycle-safe; fail-open to empty."""
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


def hivemind(q, tg, boosts, view=None):
    out = []
    if view is not None and not view.store_available("hivemind"):
        return out
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
            if view is not None and not view.is_eligible("hivemind", rid):
                continue
            blob = f"{rule} {ctx or ''}".lower()
            hits = sum(blob.count(t) for t in tg)
            extra = redirect_extra.get(rid, 0.0)
            eb = boosts.get(("hivemind", str(rid)), 0.0)
            if hits == 0 and extra == 0.0 and eb == 0.0 and not (prot and PROTECT_RE.search(blob)):
                continue
            eff = (cf or 0.5) * recency(la or cre) * (1 + math.log1p(th or 0))
            score = (hits + extra) * 2 + eff + (5 if prot else 0) + ENTITY_BOOST * eb
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

CM_FTS_POOL_LIMIT = 600
CM_LIKE_POOL_LIMIT = 400
CM_BODY_SQL = ("COALESCE(o.text,'') || ' ' || COALESCE(o.facts,'') "
               "|| ' ' || COALESCE(o.narrative,'')")
CM_FIELDS_SQL = (f"o.id, COALESCE(o.title,''), {CM_BODY_SQL}, "
                 "o.created_at_epoch, COALESCE(o.relevance_count,0)")
CM_NEWEST_SQL = "o.created_at_epoch DESC, o.id DESC"


def _cm_like(connection, query_terms):
    """Bounded relevance and recency pools when FTS5 is unavailable."""
    content = f"lower(COALESCE(o.title,'') || ' ' || {CM_BODY_SQL})"
    title_content = "lower(COALESCE(o.title,''))"
    body_content = f"lower({CM_BODY_SQL})"
    predicate = " OR ".join(f"{content} LIKE ?" for term in query_terms)
    relevance = " + ".join(
        f"((length({title_content}) - length(replace({title_content}, ?, ''))) / ? "
        f"+ (instr({body_content}, ?) > 0))"
        for term in query_terms)
    matches = tuple(f"%{term}%" for term in query_terms)
    weights = tuple(value for term in query_terms for value in (term, len(term), term))
    statement = f"SELECT {CM_FIELDS_SQL} FROM observations o WHERE {predicate} "
    relevant = retry(lambda: connection.execute(
        statement + f"ORDER BY {relevance} DESC, {CM_NEWEST_SQL} LIMIT ?",
        (*matches, *weights, CM_LIKE_POOL_LIMIT)).fetchall())
    recent = retry(lambda: connection.execute(
        statement + f"ORDER BY {CM_NEWEST_SQL} LIMIT ?",
        (*matches, CM_LIKE_POOL_LIMIT)).fetchall())
    return {row[0]: row for row in (*relevant, *recent)}


CM_FTS_TIE_MARGIN = 200   # ranked rows read past the pool, to settle equal ranks at its edge by newest
CM_TIME_INDEX = "idx_observations_created"


def _cm_rows(connection, ids):
    """{id: row} for the observations among `ids` that exist."""
    rows = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in retry(lambda: connection.execute(
                f"SELECT {CM_FIELDS_SQL} FROM observations o WHERE o.id IN ({marks})", chunk).fetchall()):
            rows[row[0]] = row
    return rows


class _Reversed:
    """Sorts in the reverse of its value's order: a descending key part for values that cannot be negated."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return other.value < self.value

    def __eq__(self, other):
        return self.value == other.value


def _cm_newest_key(row):
    """Sort key for CM_NEWEST_SQL (created_at_epoch DESC, id DESC) as SQLite orders it.

    The column should hold a number, but SQLite stores whatever it is given; descending, it puts bytes first,
    then text, then numbers, then no value."""
    value = row[3]
    if value is None:
        kind, part = 3, 0
    elif isinstance(value, (int, float)):
        kind, part = 2, -value
    elif isinstance(value, str):
        kind, part = 1, _Reversed(value.encode("utf-8", "surrogatepass"))
    else:
        kind, part = 0, _Reversed(bytes(value))
    return (kind, part, -row[0])


def _cm_fts_relevant(connection, match, limit):
    """Rows of the `limit` best-ranked matches, best first; equal rank goes to the newest.

    The ranking is read from the search index alone and only the winners' rows are fetched. One joined
    joined statement would fetch every matching row before sorting."""
    want = limit + CM_FTS_TIE_MARGIN
    while True:
        ranked = retry(lambda: connection.execute(
            "SELECT rowid, rank FROM observations_fts WHERE observations_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, want)).fetchall())
        rank = {row[0]: row[1] for row in ranked}
        best = sorted(_cm_rows(connection, list(rank)).values(), key=lambda row: (rank[row[0]], *_cm_newest_key(row)))
        # complete once no unread match can enter the pool: every match was read, or the last one read ranks
        # strictly below the pool's last place (an equal rank could still be newer)
        if len(ranked) < want or (len(best) >= limit and ranked[-1][1] != rank[best[limit - 1][0]]):
            return best[:limit]
        want *= 4


def _cm_fts_recent(connection, match, limit):
    """ids of the `limit` newest matches, newest first, by walking the time index and testing each entry
    against the matches. Left to itself the planner fetches every matching row and sorts (6 s)."""
    statement = ("SELECT o.id FROM observations o {hint}WHERE o.id IN "
                 "(SELECT rowid FROM observations_fts WHERE observations_fts MATCH ?) "
                 f"ORDER BY {CM_NEWEST_SQL} LIMIT ?")
    try:
        return [row[0] for row in retry(lambda: connection.execute(
            statement.format(hint=f"INDEXED BY {CM_TIME_INDEX} "), (match, limit)).fetchall())]
    except sqlite3.OperationalError as exc:
        if "no such index" not in str(exc):
            raise
    return [row[0] for row in retry(lambda: connection.execute(statement.format(hint=""), (match, limit)).fetchall())]


def _cm_lexical(connection, query_terms):
    """Union relevant and recent matches without starving either pool."""
    if not query_terms:
        return {}, "none"
    try:
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in query_terms)
        relevant = _cm_fts_relevant(connection, match, CM_FTS_POOL_LIMIT)
        rows = {row[0]: row for row in relevant}
        recent = _cm_fts_recent(connection, match, CM_FTS_POOL_LIMIT)
        fetched = _cm_rows(connection, [oid for oid in recent if oid not in rows])
        for oid in recent:
            if oid not in rows and oid in fetched:
                rows[oid] = fetched[oid]
        return rows, "fts5"
    except sqlite3.Error:
        return _cm_like(connection, query_terms), "like"

def suppressed_obs_ids():
    """Consolidation overlay (local3366): observation ids collapsed as duplicates.
    Lives in semantic.db so the plugin's DB stays untouched. FAIL-CLOSED: raises
    graph_policy.PolicyUnavailable when the overlay cannot be established —
    an unreadable overlay is never an empty exclusion set."""
    try:
        c = graph_policy.open_ro(graph_policy.SEM_DB)
    except sqlite3.Error as exc:
        raise graph_policy.PolicyUnavailable(f"overlay_db:{type(exc).__name__}") from exc
    try:
        return set(graph_policy.load_policy(c).suppressed)
    finally:
        c.close()

def claudemem(q, tg, boosts, view=None):
    """claude-mem candidates. `view` is the eligibility view; when it says the
    store is unavailable (privacy overlay unreadable) NOTHING is returned and
    the mode is 'unavailable' — distinct from 'no results'."""
    out, mode = [], "none"
    if view is None:
        try:
            view = _load_view()
        except graph_policy.GraphUnavailable as exc:
            view = graph_policy.fallback_view(exc.reason)
    if not view.store_available("claudemem"):
        return out, "unavailable"
    try:
        c = conn(CM)
        rows, mode = _cm_lexical(c, tg)
        for sid in list(rows):
            if not view.is_eligible("claudemem", sid):
                rows.pop(sid, None)
        lexical_ids = set(rows)
        # entity-linked observations, fetched even with zero lexical overlap —
        # the eligibility view applies to this path too, or collapsed dups
        # resurface on exactly the entity queries the layer was built for
        ids = [int(ref) for (store, ref) in boosts if store == "claudemem" and str(ref).isdigit()]
        ids = [i for i in ids if i not in lexical_ids and view.is_eligible("claudemem", i)]
        for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
            marks = ",".join("?" * len(chunk))
            r = retry(lambda: c.execute(
                f"SELECT {CM_FIELDS_SQL} "
                f"FROM observations o WHERE o.id IN ({marks})", chunk).fetchall())
            for x in r:
                if view.is_eligible("claudemem", x[0]):
                    rows[x[0]] = x
        for oid, title, text, ep, rel in rows.values():
            title_lower = (title or "").lower()
            body_lower = (text or "").lower()
            blob = f"{title_lower} {body_lower}"
            hits = sum(title_lower.count(term) + int(term in body_lower) for term in tg)
            eb = boosts.get(("claudemem", str(oid)), 0.0)
            prot = 1 if PROTECT_RE.search(blob) else 0
            score = hits * 2 + recency(ep) + (1 + math.log1p(rel)) + (4 if prot else 0) + ENTITY_BOOST * eb
            out.append((score, oid, prot, (title or "")[:140], oid not in lexical_ids and eb > 0))
        c.close()
    except Exception as e:
        sys.stderr.write(f"[claude-mem skipped: {e}]\n")
    out.sort(key=lambda r: -r[0])
    return out, mode

def files(q, tg, boosts, view=None):
    out = []
    if view is not None and not view.store_available("file"):
        return out
    try:
        for fn in os.listdir(MEMDIR):
            if not fn.endswith(".md"): continue
            if view is not None and not view.is_eligible("file", fn): continue
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

# Rank-based fusion sidesteps the incomparable per-store score formulas.
# ---------------------------------------------------------------------------
RRF_K = 60               # the reference scale: the first place of a list of weight w is worth w / (RRF_K + 1).
                         # Also the fade of the graph list (the plain reciprocal-rank formula)
LEXICAL_K = 1
VECTOR_K = 10
VECTOR_WEIGHT = 2.5
VECTOR_MIN_COS = 0.45
CONFIDENT_FUSED = 0.05   # eval metric: what counts as a "confident" fused top-1


def _place_credit(weight, k, rank):
    """What the record at 0-based `rank` of a list adds to its fused score.

    A first place is worth weight / (RRF_K + 1) whatever k is; k sets how fast later places fade, and k = RRF_K is
    the plain reciprocal-rank formula. Scores that include later places are lower than they were, so the reranker's
    skip rule (RERANK_MIN_TOP1, RERANK_MIN_LEAD) and CONFIDENT_FUSED no longer fire on the same records: without
    graph credit the bar is met only by a record first in both its keyword list and its vector list, and the lead is
    met more often."""
    if k == RRF_K:
        return weight / (RRF_K + rank + 1)
    return weight * (k + 1) / ((k + rank + 1) * (RRF_K + 1))


def fuse(lists, canonical=None):
    """Fused score per record from [(weight, k, [key, ...])], each list best first.

    `canonical` maps a key to the record it stands for, or to None for a record that no longer exists; a list
    credits each record once, at the first place that maps to it, and skipped entries still take up their places."""
    fused = {}
    for weight, k, keys in lists:
        credited = set()
        for rank, key in enumerate(keys):
            if canonical is not None:
                key = canonical(key)
            if key is None or key in credited:
                continue  # dead redirect / same canonical already credited by this list
            credited.add(key)
            fused[key] = fused.get(key, 0.0) + _place_credit(weight, k, rank)
    return fused


# Vector candidates per store (entries in each list). One list over every store let the largest store crowd
# the rest out. Per-store lists preserve candidates from smaller stores, as the lexical side already does.
VECTOR_STORE_TOP = {"file": 20, "hivemind": 20, "claudemem": 30}


def _vector_lists(query, view=None):
    """One ranked [(store, ref)] list per store by cosine, or None (Ollama/index absent = fail open).

    Each store's ranking is read from the top until its list is full, the matches fall below VECTOR_MIN_COS, or
    it runs out. On the way a superseded learning (store hm_redirect, or a hivemind row that expired after it
    was embedded) is replaced by its active root at its own score, each record is kept once, and records the
    view withholds are skipped: none of those uses up a place, however many there are."""
    try:
        import embed_index
        if not os.path.isfile(embed_index.DB):
            return None
        ec = embed_index.connect()
        try:
            ranked = embed_index.ranked_by_store(ec, query, [*VECTOR_STORE_TOP, "hm_redirect"])
        finally:
            ec.close()
        if not ranked:
            return None

        def tagged(store):
            return ((store, str(ref), score) for ref, score in ranked[store])

        sources = {store: tagged(store) for store in VECTOR_STORE_TOP}
        sources["hivemind"] = heapq.merge(tagged("hivemind"), tagged("hm_redirect"), key=lambda hit: -hit[2])
        redirect = view.hivemind_redirect if view is not None and view.store_available("hivemind") else {}

        def canonical(store, ref):  # the same mapping fusion applies
            if view is None:
                return (store, ref)
            if store == "hm_redirect":
                return ("hivemind", redirect[ref]) if redirect.get(ref) else None
            if store == "hivemind" and ref in redirect:
                return ("hivemind", redirect[ref])
            return (store, ref)

        lists = []
        for store, size in VECTOR_STORE_TOP.items():
            kept, seen = [], set()
            for s, r, score in sources[store]:
                if score < VECTOR_MIN_COS:
                    break  # best first: nothing after this can pass
                key = canonical(s, r)
                if key is None or key in seen:
                    continue
                seen.add(key)
                if view is None or view.is_eligible(*key):
                    kept.append(key)
                    if len(kept) == size:
                        break
            if kept:
                lists.append(kept)
        return lists or None
    except Exception as e:
        sys.stderr.write(f"[vector skipped: {e}]\n")
        return None


def _ppr_list(matched, top=100, view=None):
    """Ranked [(store, ref)] by Personalized PageRank from resolved entities,
    walked over the SAME eligibility view as every other signal."""
    try:
        import ppr
        seeds = {}
        for m in matched:
            eid = getattr(m, "entity_id", None)
            if eid is not None:
                seeds[eid] = 1.0
        hits = ppr.ranks(seeds, top=top, view=view)
        if not hits:
            return None
        return [(s, str(r)) for (s, r), _score in hits]
    except Exception as e:
        sys.stderr.write(f"[ppr skipped: {e}]\n")
        return None


def _load_view():
    return graph_policy.load_view(None, graph_policy.Sources())


def fused_search(query, k=25, use_vector=True, use_ppr=True, no_semantic=False,
                 return_parts=False, view=None):
    """RRF across per-store lexical lists + vector + PPR. Returns ranked
    [(store, ref, fused_score)]; every ref is a str.

    One eligibility view governs every list. Graph signals (entity boosts,
    PPR) are used only when the view is complete; the view is re-checked
    before returning and a revision change drops the graph contribution and
    re-fuses from the independently eligible lists (lexical + vector) under a
    fresh view. `no_semantic` never disables exclusions.

    The per-store searches are independent (each opens its own connections and
    only reads), so they run at the same time; results are fused in the same
    fixed order as before. Only one of them (the vector search) uses the
    embedding service, so they do not compete for its single model slot."""
    tg = terms(query)
    graph_state = "ok"
    if view is None:
        try:
            view = _load_view()
        except graph_policy.GraphUnavailable as exc:
            view = graph_policy.fallback_view(exc.reason)
            graph_state = f"unavailable:{exc.reason}"
    use_graph = sem is not None and not no_semantic and graph_state == "ok"
    for attempt in range(2):
        boosts, matched = ({}, [])
        if use_graph:
            try:
                boosts, matched = sem.recall_boosts(query, view=view)
            except Exception as e:
                sys.stderr.write(f"[semantic skipped: {e}]\n")
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="recall-store") as pool:
            hm_job = pool.submit(hivemind, query, tg, boosts, view=view)
            cm_job = pool.submit(claudemem, query, tg, boosts, view=view)
            fl_job = pool.submit(files, query, tg, boosts, view=view)
            v_job = pool.submit(_vector_lists, query, view) if use_vector else None
            hm = hm_job.result()
            cm, cm_mode = cm_job.result()
            fl = fl_job.result()
            v = v_job.result() if v_job else None
        lists = [
            (1.0, LEXICAL_K, [("hivemind", str(r[1])) for r in hm if r[6]]),  # grounded only
            (1.0, LEXICAL_K, [("claudemem", str(r[1])) for r in cm]),
            (1.0, LEXICAL_K, [("file", str(r[1])) for r in fl]),
        ]
        for per_store in (v or []) if use_vector else []:
            # vector-only hits obey the same view (hm_redirect rows are
            # resolved to their active root by canonical() below)
            lists.append((VECTOR_WEIGHT, VECTOR_K, [(s_, r_) for s_, r_ in per_store
                                                    if s_ == "hm_redirect" or view.is_eligible(s_, r_)]))
        if use_graph and use_ppr and matched:
            p = _ppr_list(matched, view=view)
            if p:
                # the graph list keeps the plain formula (its places were not measured); beside the faded lists a
                # deep graph place now counts for relatively more than a deep keyword or vector place
                lists.append((1.0, RRF_K, p))

        # graph/lexical reference to an expired learning resolve to the active root
        red = view.hivemind_redirect if view.store_available("hivemind") else {}

        def canonical(key):
            s, r = key
            if s == "hm_redirect":
                root = red.get(r)
                return ("hivemind", root) if root else None
            if s == "hivemind" and r in red:
                return ("hivemind", red[r])
            return key

        fused = fuse(lists, canonical)
        ranked = sorted(((s, r, sc) for (s, r), sc in fused.items() if view.is_eligible(s, r)),
                        key=lambda x: -x[2])[:k]
        # recheck immediately before emitting: a policy/alias/control change
        # since the view was built makes the graph contribution unusable
        ok, why = graph_policy.recheck(view)
        if ok or attempt == 1:
            if not ok:
                # the policy moved again during the rebuilt attempt: withhold
                # claude-mem for this emit rather than trust a stale view, and
                # let the printed eligibility summary say so
                graph_state = f"unavailable:{why}"
                ranked = [t for t in ranked if t[0] != "claudemem"]
                cm, cm_mode = [], "unavailable"
                import dataclasses as _dc
                view = _dc.replace(view, stores={**view.stores,
                                                 "claudemem": graph_policy.StoreState("unavailable", f"withheld:{why}")})
            break
        graph_state = f"unavailable:{why}"
        use_graph = False
        try:
            view = _load_view()
        except graph_policy.GraphUnavailable as exc:
            view = graph_policy.fallback_view(exc.reason)
    if return_parts:
        return ranked, {"hm": hm, "cm": cm, "cm_mode": cm_mode, "fl": fl,
                        "boosts": boosts, "matched": matched, "tg": tg,
                        "view": view, "graph_state": graph_state}
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


# ---------------------------------------------------------------------------

def bump_hm(ids):
    if not ids or not os.path.isfile(HM):
        return
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
    if not ids or not os.path.isfile(CM):
        return
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
    ap.add_argument("--no-semantic", action="store_true")
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--no-ppr", action="store_true")
    a = ap.parse_args()
    if not terms(a.query):
        print("no searchable terms")
        return
    ranked, parts = fused_search(a.query, k=max(10, a.k), use_vector=not a.no_vector,
                                 use_ppr=not a.no_ppr, no_semantic=a.no_semantic, return_parts=True)
    labels = _fused_labels(ranked, parts)
    stale = {}
    try:
        import mem_check
        from contextlib import closing
        ids = [ref for store, ref, _ in ranked[:a.k] if store == "hivemind"]
        if ids:
            with closing(sqlite3.connect(Path(HM).resolve().as_uri() + "?mode=ro", uri=True)) as check_conn:
                stale = {str(key): value for key, value in mem_check.stale_map(check_conn, ids).items()}
    except (OSError, sqlite3.Error, ValueError):
        pass
    print("=== memory recall ===")
    for store, ref, score in ranked[:a.k]:
        date = stale.get(str(ref)) if store == "hivemind" else None
        if store == "file":
            try:
                date = mem_check.stale_note(os.path.join(MEMDIR, ref))
            except (OSError, ValueError):
                pass
        note = f"  [unconfirmed since {date}]" if date else ""
        print(f"[{score:.3f}] {store}:{ref}  {labels[(store, ref)][1]}{note}")
    if not a.no_bump:
        bump_hm([r[1] for r in parts["hm"][:a.k]])
        bump_cm([r[1] for r in parts["cm"][:a.k]])

if __name__ == "__main__":
    main()
