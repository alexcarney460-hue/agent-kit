#!/usr/bin/env python3
"""mem_gate — poisoning-aware write gate for the hivemind memory store.

Memories are increasingly distilled from UNTRUSTED text (inbox email, web
content, meeting transcripts, chat sessions). A poisoned memory persists and
steers every future session — the OWASP ASI06 memory-poisoning pattern. This
module screens text BEFORE hm_write.py commits it:

    scan(text, context="") -> GateResult(verdict, findings, redacted_text)

Detectors (regex/heuristic only — no LLM, sub-50ms):
    injection        imperative agent-steering patterns, jailbreak stems,
                     pseudo-tags (<system, </instructions, [INST], ...)   [high]
    unicode-smuggle  zero-width / bidi-override codepoints                 [high]
    base64-blob      >=200-char base64 runs (payload smuggling)            [medium]
    link-mismatch    markdown link text naming a different domain          [medium]
    secret-*         AWS AKIA / GitHub ghp_/pat / Slack xox / Bearer /
                     PEM private key / AGE-SECRET-KEY / JWT /
                     high-entropy run adjacent to key|token|secret|password [high]
    pii-*            SSN (validity-checked), credit card (Luhn),
                     bank routing+account combos (ABA checksum)            [high]
    size-anomaly     rule+context > 8,000 chars (stuffing/exfil signal)    [high]

Any HIGH finding -> verdict "quarantine": hm_write diverts the row into the
`quarantine` table instead of `learnings`. Medium-only -> "allow" but findings
are reported. Secrets/PII are masked in redacted_text, in stored quarantine
rows, AND in finding excerpts — the raw secret never lands anywhere.

CLI:
    mem_gate.py scan "text" [--context "..."]   run the gate, print findings
    mem_gate.py review                          list unreviewed quarantine rows
    mem_gate.py release <id>                    write row to learnings via
                                                hm_write --force, mark reviewed
    mem_gate.py reject <id>                     mark reviewed without insert

DB: the same ~/hivemind.db hm_write uses (override: HIVEMIND_DB env, resolved
at call time). Every write here is one short WAL transaction with retry — it
never blocks a concurrent session's writes.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import memdir
from collections import Counter
from dataclasses import dataclass, field

HERE = memdir.tools_dir()
HM_WRITE = os.path.join(HERE, "hm_write.py")
MAX_LEN = 8000
ENTROPY_MIN_BITS = 3.5


@dataclass(frozen=True)
class GateResult:
    verdict: str                       # "allow" | "quarantine"
    findings: list = field(default_factory=list)   # [{detector, severity, excerpt}]
    redacted_text: str = ""            # `text` arg with secrets/PII masked


# --------------------------------------------------------------------------- #
# Detector patterns
# --------------------------------------------------------------------------- #
_INJ_HIGH = [re.compile(p, re.I) for p in (
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|prompts?|rules?|directives?|context)",
    r"disregard\s+your\b",
    r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)?\s*(?:instructions?|prompts?|rules?|guidelines?|training|directives?)",
    r"forget\s+(?:all\s+|everything\s+)?(?:previous|prior|above)\b",
    r"forget\s+your\s+(?:instructions?|rules?|training|guidelines?)",
    r"you\s+must\s+now\b",
    r"you\s+are\s+now\s+(?:a\b|an\b|in\b|free\b|unrestricted|dan\b)",
    r"new\s+(?:system\s+)?instructions?\s*:",
    r"system\s*prompt",
    r"\bact\s+as\s+(?:if\s+you\s+(?:are|were)|an?\s+(?:unrestricted|uncensored|jailbroken))",
    r"pretend\s+(?:that\s+)?you\s+(?:are|were|have\s+no|can)",
    r"\bdo\s+anything\s+now\b",
    r"\bdan\s+mode\b",
    r"developer\s+mode\s+(?:enabled|on|activated)",
    r"\bjailbr(?:eak|oken)\b",
    r"override\s+(?:your|all|the)\s+(?:safety|previous|prior|system|guardrails?)",
    r"bypass\s+(?:your\s+|the\s+|all\s+)?(?:safety|security|filters?|restrictions?|guardrails?)",
    r"do\s+not\s+(?:tell|inform|alert|notify)\s+(?:the\s+)?(?:user|human|operator)",
    r"without\s+(?:telling|informing|alerting|notifying)\s+(?:the\s+)?(?:user|human|operator)",
    r"(?:send|export|upload|post|transmit|forward)\s+(?:all\s+|the\s+|your\s+)?(?:keys?|credentials?|secrets?|passwords?|tokens?|data)\s+to\b",
    r"\bexfiltrat\w*",
    r"<\s*/?\s*(?:system|sys|instructions?|insructions?|intructions?|admin|prompt|assistant|user|tool_call|function_call)\b",
    r"\[/?INST\]|<\|im_(?:start|end)\|>|<<SYS>>",
    r"#+\s*(?:new\s+)?(?:system|admin)\s+(?:prompt|message|instructions?)",
    r"this\s+is\s+(?:your|the)\s+(?:new\s+)?(?:system|admin|root)\s+(?:message|prompt|instruction)",
    r"your\s+(?:true|real|actual|new)\s+(?:purpose|goal|mission|instructions?)\s+(?:is|are)\b",
    r"ignore\s+(?:the\s+)?(?:safety|content)\s+(?:policy|policies|guidelines|filters?)",
    r"\brole-?play\s+as\b",
)]

_INJ_MED = [re.compile(p, re.I) for p in (
    r"respond\s+only\s+with\b",
    r"admin\s+override\b",
    r"\bsecretly\b",
    r"do\s+not\s+mention\b",
)]

_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")

# zero-width & bidi controls; ZWJ/ZWNJ handled separately (emoji use them)
_ZW = re.compile("[\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_ZWJ = re.compile("[\u200c\u200d]")

_MD_LINK = re.compile(r"\[([^\]]{1,200})\]\((https?://[^)\s]{1,300})\)")
_DOMAIN = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)", re.I)

_SECRETS = [
    ("secret-aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("secret-github", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("secret-slack", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}\b")),
    ("secret-bearer", re.compile(r"\b(?:authorization|bearer)\b\s*[:=]?\s*(?:bearer\s+)?([A-Za-z0-9._~+/-]{20,}={0,2})", re.I)),
    ("secret-pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]{0,10000}?-----END [A-Z ]*PRIVATE KEY-----)?")),
    ("secret-age", re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]{8,}\b")),
    ("secret-jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")),
]

_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_SECRET_WORD = re.compile(r"\b(?:api[_-]?key|key|token|secret|password|passwd|pwd|credential)s?\b", re.I)

_SSN = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
_CC = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_ROUTING = re.compile(r"\b\d{9}\b")
_ROUTING_CTX = re.compile(r"\b(?:routing|aba|rtn)\b", re.I)
_ACCT_CTX = re.compile(r"\bacc(?:oun)?t\b", re.I)
_ACCT_NUM = re.compile(r"\b\d{6,17}\b")


# --------------------------------------------------------------------------- #
# Detector helpers
# --------------------------------------------------------------------------- #
def _shannon(s: str) -> float:
    n = len(s)
    if not n:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _aba_checksum(d: str) -> bool:
    w = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    return sum(int(c) * m for c, m in zip(d, w)) % 10 == 0


def _ssn_valid(area: str, group: str, serial: str) -> bool:
    a = int(area)
    return a not in (0, 666) and a < 900 and int(group) != 0 and int(serial) != 0


def _mask(secret: str, detector: str) -> str:
    return f"{secret[:4]}…[{detector}: {len(secret)} chars masked]"


def _finding(detector: str, severity: str, excerpt: str) -> dict:
    return {"detector": detector, "severity": severity, "excerpt": excerpt[:120]}


def _redact_spans(text: str, spans: list) -> str:
    """Replace (start, end, detector) spans with placeholders; merges overlaps."""
    if not spans:
        return text
    ordered = sorted(spans, key=lambda s: (s[0], -s[1]))
    merged: list[list] = []
    for start, end, det in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, det])
    out, pos = [], 0
    for start, end, det in merged:
        out.append(text[pos:start])
        out.append(f"[REDACTED:{det}]")
        pos = end
    out.append(text[pos:])
    return "".join(out)


# --------------------------------------------------------------------------- #
# Core scan
# --------------------------------------------------------------------------- #
def _scan_one(text: str) -> tuple[list, list]:
    """Run every detector over one string. Returns (findings, redaction_spans)."""
    findings: list = []
    spans: list = []

    for pat in _INJ_HIGH:
        m = pat.search(text)
        if m:
            findings.append(_finding("injection", "high", m.group(0)))
    for pat in _INJ_MED:
        m = pat.search(text)
        if m:
            findings.append(_finding("injection", "medium", m.group(0)))

    zw = _ZW.findall(text)
    zw += [c for m in _ZWJ.finditer(text)
           for c in m.group(0)
           if (m.start() == 0 or ord(text[m.start() - 1]) < 128)
           and (m.end() >= len(text) or ord(text[m.end()]) < 128)]
    if zw:
        codes = ",".join(sorted({f"U+{ord(c):04X}" for c in zw}))
        findings.append(_finding("unicode-smuggle", "high", f"{len(zw)} hidden codepoint(s): {codes}"))

    m = _BASE64_BLOB.search(text)
    if m:
        findings.append(_finding("base64-blob", "medium",
                                 f"{m.group(0)[:40]}… ({len(m.group(0))} chars)"))

    for m in _MD_LINK.finditer(text):
        text_doms = {".".join(d.lower().split(".")[-2:]) for d in _DOMAIN.findall(m.group(1))}
        href = _DOMAIN.search(m.group(2))
        if text_doms and href:
            href_dom = ".".join(href.group(1).lower().split(".")[-2:])
            if href_dom not in text_doms:
                findings.append(_finding("link-mismatch", "medium",
                                         f"text says {sorted(text_doms)} but href is {href_dom}"))

    for det, pat in _SECRETS:
        for m in pat.finditer(text):
            start, end = m.span(1) if m.lastindex else m.span(0)
            spans.append((start, end, det))
            findings.append(_finding(det, "high", _mask(text[start:end], det)))

    covered = [(s, e) for s, e, _ in spans]
    for m in _LONG_RUN.finditer(text):
        if any(m.start() < e and m.end() > s for s, e in covered):
            continue                      # already claimed by a specific detector
        run = m.group(0)
        # the run's own prefix counts: "api_key=Xj29..." is one charset run,
        # so the keyword may sit inside the match rather than before it
        window = text[max(0, m.start() - 60):m.start()] + run[:24]
        if (_SECRET_WORD.search(window)
                and any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
                and _shannon(run) >= ENTROPY_MIN_BITS):
            spans.append((m.start(), m.end(), "secret-entropy"))
            findings.append(_finding("secret-entropy", "high", _mask(run, "secret-entropy")))

    for m in _SSN.finditer(text):
        if _ssn_valid(*m.groups()):
            spans.append((m.start(), m.end(), "pii-ssn"))
            findings.append(_finding("pii-ssn", "high", _mask(m.group(0), "pii-ssn")))

    for m in _CC.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and digits[0] in "3456" and _luhn(digits):
            spans.append((m.start(), m.end(), "pii-creditcard"))
            findings.append(_finding("pii-creditcard", "high", _mask(digits, "pii-creditcard")))

    if _ROUTING_CTX.search(text) and _ACCT_CTX.search(text):
        for m in _ROUTING.finditer(text):
            if not _aba_checksum(m.group(0)):
                continue
            others = [a for a in _ACCT_NUM.finditer(text) if a.span() != m.span()]
            if others:
                spans.append((m.start(), m.end(), "pii-bankacct"))
                for a in others:
                    spans.append((a.start(), a.end(), "pii-bankacct"))
                findings.append(_finding("pii-bankacct", "high",
                                         _mask(m.group(0), "pii-bankacct") + " + account number(s)"))
                break

    return findings, spans


def scan(text: str, context: str = "") -> GateResult:
    """Screen a prospective memory write. Never raises on ordinary input."""
    text = text if isinstance(text, str) else str(text)
    context = context if isinstance(context, str) else str(context or "")
    findings, spans = _scan_one(text)
    if context:
        ctx_findings, _ = _scan_one(context)
        findings = findings + ctx_findings
    total = len(text) + len(context)
    if total > MAX_LEN:
        findings = findings + [_finding("size-anomaly", "high",
                                        f"rule+context is {total} chars (max {MAX_LEN})")]
    verdict = "quarantine" if any(f["severity"] == "high" for f in findings) else "allow"
    return GateResult(verdict=verdict, findings=findings,
                      redacted_text=_redact_spans(text, spans))


def redact(text: str) -> str:
    """Mask secrets/PII in a string without producing a verdict."""
    if not text:
        return text
    _, spans = _scan_one(text)
    return _redact_spans(text, spans)


# --------------------------------------------------------------------------- #
# Quarantine store (same ~/hivemind.db as hm_write; short WAL writes only)
# --------------------------------------------------------------------------- #
def db_path() -> str:
    return memdir.hivemind_db()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b
    return conn


def with_retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < tries - 1:
                time.sleep(0.05 * (2 ** i) + random.random() * 0.05)
                continue
            raise


def ensure_quarantine(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quarantine ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, rule TEXT, "
        "context TEXT, findings_json TEXT, created_at TEXT, "
        "reviewed INTEGER DEFAULT 0)")
    conn.commit()


def quarantine_insert(category: str, rule: str, context: str | None, findings: list) -> int:
    """Store a blocked write for review. Secrets/PII are masked before storage."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        ensure_quarantine(conn)

        def ins():
            cur = conn.execute(
                "INSERT INTO quarantine (category, rule, context, findings_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (category, redact(rule), redact(context) if context else context,
                 json.dumps(findings), now))
            conn.commit()
            return cur.lastrowid
        return with_retry(ins)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_scan(args) -> int:
    result = scan(args.text, args.context or "")
    print(f"verdict: {result.verdict}")
    for f in result.findings:
        print(f"  [{f['severity']:6s}] {f['detector']}: {f['excerpt']}")
    if result.redacted_text != args.text:
        print(f"redacted: {result.redacted_text}")
    return 2 if result.verdict == "quarantine" else 0


def cmd_review(args) -> int:
    conn = connect()
    try:
        ensure_quarantine(conn)
        rows = with_retry(lambda: conn.execute(
            "SELECT id, created_at, findings_json, rule FROM quarantine "
            "WHERE reviewed=0 ORDER BY id").fetchall())
    finally:
        conn.close()
    if not rows:
        print("quarantine empty — nothing awaiting review")
        return 0
    for rid, ts, fj, rule in rows:
        try:
            dets = ",".join(sorted({f["detector"] for f in json.loads(fj)}))
        except Exception:
            dets = "?"
        print(f"{rid:5d}  {(ts or '?')[:10]}  [{dets}]  {(rule or '')[:120]}")
    print(f"\n{len(rows)} row(s). release <id> to insert into learnings, reject <id> to discard.")
    return 0


def _fetch_row(conn, qid: int):
    return conn.execute(
        "SELECT category, rule, context, reviewed FROM quarantine WHERE id=?",
        (qid,)).fetchone()


def cmd_release(args) -> int:
    conn = connect()
    try:
        ensure_quarantine(conn)
        row = _fetch_row(conn, args.id)
    finally:
        conn.close()
    if row is None:
        print(f"no quarantine row with id={args.id}")
        return 2
    category, rule, context, reviewed = row
    if reviewed:
        print(f"quarantine row {args.id} is already reviewed — nothing done")
        return 2
    cmd = [sys.executable, HM_WRITE, "--force", "--source-session", "quarantine-release"]
    if context:
        cmd += ["--context", context]
    cmd += ["--", category, rule]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not ("SAVED" in r.stdout or "DEDUP" in r.stdout):
        print(f"release FAILED (hm_write rc={r.returncode}) — row {args.id} left unreviewed")
        return 1
    conn = connect()
    try:
        with_retry(lambda: (conn.execute(
            "UPDATE quarantine SET reviewed=1 WHERE id=?", (args.id,)), conn.commit()))
    finally:
        conn.close()
    print(f"released quarantine row {args.id} into learnings; marked reviewed")
    return 0


def cmd_reject(args) -> int:
    conn = connect()
    try:
        ensure_quarantine(conn)

        def upd():
            cur = conn.execute(
                "UPDATE quarantine SET reviewed=1 WHERE id=? AND reviewed=0", (args.id,))
            conn.commit()
            return cur.rowcount
        n = with_retry(upd)
    finally:
        conn.close()
    if n == 0:
        print(f"no unreviewed quarantine row with id={args.id}")
        return 2
    print(f"rejected quarantine row {args.id}; marked reviewed (no learnings insert)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="run the gate over a piece of text")
    p.add_argument("text")
    p.add_argument("--context", default="")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("review", help="list unreviewed quarantine rows")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("release", help="insert a quarantined row into learnings (gate bypassed)")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser("reject", help="mark a quarantined row reviewed without inserting")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_reject)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
