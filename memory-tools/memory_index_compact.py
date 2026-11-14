"""Compact the always-loaded memory index MEMORY.md until it fits a byte budget.

Entries (`- [Title](name.md) — hook`) are cut or moved to the topic index files
(`index-*.md`), one entry per step, oldest first. See TASK.md and TASK-r2.md in this folder for the rules.

Every line of MEMORY.md that is not an entry is kept byte for byte, with ONE stated exception: the numbers in
`[N memories](index-x.md)` links after the `## By topic` heading are rewritten for each topic file whose count
changed. (An entry line that is cut keeps its own line ending.) Standard library only; nothing is written
unless `apply` is true.

Limits, stated on purpose:
(a) Freshness is judged by the dates written in the line (title, hook, file name) and by the modification time of
    the note file. A line added today for an old undated note, or a restatement written as "Nov 10" or "11-9"
    without touching the note, is not seen as fresh: write dates as MM-DD and touch the note.
(b) A kind is the exact capital word (4+ letters) at the very start of the hook; variants (RULES, lower case, a
    date first, **STANDING**) are read as dated entries.
(c) Another writer's edit that lands between the final comparison and the replace can be lost. A backup of every
    run is kept, and the quiet check plus a night-time run make this unlikely.
(d) Counts are healed only by a run that changes something.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import memdir
import re
import shutil
import sys
import tempfile
import time
from typing import (Callable, Dict, FrozenSet, List, Mapping, NamedTuple, NoReturn, Optional,
                    Sequence, Tuple, Union)

MEMORY_FILE = 'MEMORY.md'
DEFAULT_QUIET_SECONDS = 90  # command line only; compact() defaults to 0 (check off)
DEFAULT_BUDGET = 20000  # leave room below the memory reader's limit
MAX_ATTEMPTS = 3
CUT_LIMIT = 44
LINKS_PER_LINE = 3
KEEP_KINDS = frozenset(('STANDING', 'RULE', 'CONTROLLING', 'FEEDBACK'))
DECISION_KIND = 'DECISION'

ENTRY_RE = re.compile(r'^- \[(?P<title>[^\]]*)\]\((?P<name>[^()]*\.md)\) — (?P<hook>.*)$')
ANY_LINK_RE = re.compile(r'\[[^\]]*\]\([^)]*\)')
LINK_NAME_RE = re.compile(r'\]\(([^()]+\.md)\)')
HEADING_RE = re.compile(r'^## By topic')
KIND_RE = re.compile(r'[A-Z]{4,}(?![A-Za-z])')
KEEP_RE = re.compile(r'\bKEEP\b')  # write it as `KEEP:`; KEEP-X, (KEEP) and KEEP. count too, KEEP_X, _KEEP and KEEP2 do not
DATE_RE = re.compile(r'(?<!\d)(\d{2})[-/](\d{2})(?!\d)')
NAME_DATE_RE = re.compile(r'(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)')
COUNT_RE = re.compile(r'\[(\d+) memories\]\(([^()]+\.md)\)')
DESCRIPTION_RE = re.compile(r'^(description:[^\n]*?memories \()\d+(\))', re.M)
LABEL_RE = re.compile(r'^- ([^:\[\]\n]+): \[', re.M)

# Topic choice: the first rule whose word matches the lower-cased name + title + hook.
ROUTES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ('index-memory.md', ('memory', 'recall', 'backup')),
    ('index-tools.md', ('agent', 'model', 'tools')),
)
GOTCHAS_FILE = 'index-gotchas.md'
FALLBACK_FILE = 'index-infra.md'
# Route words short enough to appear inside other words; these match as whole words only.
WHOLE_WORDS = frozenset(('api', 'app', 'bot'))

Link = Tuple[str, str]  # (title, name.md)
NoteStamp = Union[int, str]  # modification time in nanoseconds, MISSING, or ERROR
MISSING = 'missing'  # a confirmed missing note file (FileNotFoundError / NotADirectoryError)
ERROR = 'error'  # a note file that could not be examined: its entry is protected


class Entry(NamedTuple):
    index: int
    line: str
    title: str
    name: str
    hook: str
    hook_start: int
    kind: str
    age_days: Optional[int]  # None = protected: no date and no note file, or a note that cannot be examined
    note: NoteStamp = MISSING  # the state of the note file when the entry was read

    @property
    def keep_always(self) -> bool:
        return self.kind in KEEP_KINDS

    @property
    def exempt(self) -> bool:
        """The capital word KEEP, a word of its own, in the lead of the hook (the part before the first `: `)."""
        return KEEP_RE.search(self.hook.split(': ', 1)[0]) is not None

    @property
    def group(self) -> str:
        """'rule' (a KEEP-ALWAYS kind), 'decision', or 'dated' (everything else)."""
        if self.kind in KEEP_KINDS:
            return 'rule'
        return 'decision' if self.kind == DECISION_KIND else 'dated'


class Step(NamedTuple):
    move: bool
    groups: FrozenSet[str]
    older_than: int


# Decisions remain in force until replaced. Cut them with rules and move them only after older dated entries.
_KEPT = frozenset(('rule', 'decision'))
# A rule (KEEP-ALWAYS kind) can only be cut; when the steps run out the tool
# stops with fits false and a human decides.
STEPS = (Step(True, frozenset(('dated',)), 7), Step(False, _KEPT, 7), Step(True, frozenset(('dated',)), 2),
         Step(False, _KEPT, 2), Step(True, frozenset(('decision',)), 2))


class State(NamedTuple):
    moved: Tuple[Entry, ...] = ()
    cut: Tuple[Entry, ...] = ()
    hooks: Tuple[Tuple[int, str], ...] = ()  # (line index, hook after the cut)


class Layout(NamedTuple):
    lines: Tuple[str, ...]
    heading: int
    entries: Tuple[Entry, ...]
    owners: Mapping[str, str]  # link name -> first topic file whose BODY holds it
    today: dt.date = dt.date.min


class Rendered(NamedTuple):
    changed: Mapping[str, str]  # file name -> new text, only files whose text differs
    memory: str
    counts: Mapping[str, int]
    moved: Tuple[dict, ...]
    already: Tuple[str, ...]


class Placement(NamedTuple):
    appended: Mapping[str, List[Link]]  # topic file -> links to append
    info: Tuple[dict, ...]  # the `moved` report items
    already: Tuple[str, ...]  # names that were only removed (the link is already in a topic body)
    holders: FrozenSet[str]  # the topic files that already held those links


class Plan(NamedTuple):
    changed: Mapping[str, str]
    report: dict
    notes: Mapping[str, NoteStamp]  # note file name -> state at plan time


class StaleError(Exception):
    """A file changed between the read and the replace."""

    def __init__(self, written: FrozenSet[str]) -> None:
        super().__init__('a file changed while compacting')
        self.written = written


# ---- locations ------------------------------------------------------------------

def default_memory_dir() -> Path:
    return Path(memdir.memory_dir())


def default_backup_dir(today: dt.date) -> Path:
    return Path.home() / '.claude' / 'recovery' / ('memory-index-' + today.isoformat())


# ---- parsing --------------------------------------------------------------------

def split_lines(text: str) -> List[str]:
    return re.findall(r'[^\n]*\n|[^\n]+', text)


def split_eol(line: str) -> Tuple[str, str]:
    for eol in ('\r\n', '\n'):
        if line.endswith(eol):
            return line[:-len(eol)], eol
    return line, ''


def resolve_date(month: int, day: int, today: dt.date) -> Optional[dt.date]:
    try:
        found = dt.date(today.year, month, day)
    except ValueError:
        return None
    if (found - today).days <= 30:
        return found
    try:
        return found.replace(year=today.year - 1)
    except ValueError:
        return None


def text_dates(text: str, today: dt.date) -> List[dt.date]:
    found = (resolve_date(int(m.group(1)), int(m.group(2)), today) for m in DATE_RE.finditer(text))
    return [day for day in found if day is not None]


def name_dates(name: str) -> List[dt.date]:
    """A `20YYMMDD` in the file name carries its own year."""
    days: List[dt.date] = []
    for match in NAME_DATE_RE.finditer(name):
        try:
            days.append(dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    return days


def entry_date(title: str, hook: str, name: str, today: dt.date) -> Optional[dt.date]:
    """the latest valid date anywhere in the title, the hook (no length limit) or the file name."""
    days = text_dates(title, today) + text_dates(hook, today) + name_dates(name)
    return max(days) if days else None


def note_stamp(root: Optional[Path], name: str) -> NoteStamp:
    """The state of the note file `<root>/<name>`: its modification time in nanoseconds, MISSING for a
    confirmed missing file, ERROR for anything else that stops `stat` (or a time that cannot be dated)."""
    if root is None:
        return MISSING
    try:
        stamp = (root / name).stat().st_mtime_ns
        dt.date.fromtimestamp(stamp // 10 ** 9)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except (OSError, ValueError, OverflowError):
        return ERROR
    return stamp


def note_stamps(root: Path, names: Sequence[str]) -> Dict[str, NoteStamp]:
    return {name: note_stamp(root, name) for name in names}


def stamp_date(stamp: NoteStamp) -> Optional[dt.date]:
    """the local date of a modification time; None for MISSING and ERROR."""
    return dt.date.fromtimestamp(stamp // 10 ** 9) if isinstance(stamp, int) else None


def age_in_days(written: Optional[dt.date], modified: Optional[dt.date], today: dt.date) -> Optional[int]:
    """the smaller of the two ages; None (no date, no file) means the entry is protected."""
    ages = [(today - day).days for day in (written, modified) if day is not None]
    return min(ages) if ages else None


def hook_kind(hook: str) -> str:
    match = KIND_RE.match(hook)
    return match.group(0) if match else ''


def parse_entry(line: str, today: dt.date, index: int = 0, root: Optional[Path] = None) -> Optional[Entry]:
    text, _ = split_eol(line)
    match = ENTRY_RE.match(text)
    if match is None or len(ANY_LINK_RE.findall(text)) != 1 or text.count('](') != 1:  # exactly one link
        return None
    title, name, hook = match.group('title'), match.group('name'), match.group('hook')
    written, stamp = entry_date(title, hook, name, today), note_stamp(root, name)
    age = None if stamp == ERROR else age_in_days(written, stamp_date(stamp), today)  # unexamined = protected
    return Entry(index, line, title, name, hook, match.start('hook'), hook_kind(hook), age, stamp)


class NoTopicSection(ValueError):
    """The index has no '## By topic' section, which is how Claude Code writes it by default.
    There is nowhere to move old entries to, so the index is left exactly as it is."""


def find_heading(lines: Sequence[str]) -> int:
    for number, line in enumerate(lines):
        if HEADING_RE.match(line):
            return number
    raise NoTopicSection("MEMORY.md has no '## By topic' heading")


def make_layout(memory: str, topics: Mapping[str, str], today: dt.date, root: Optional[Path] = None) -> Layout:
    lines = tuple(split_lines(memory))
    heading = find_heading(lines)
    entries = tuple(e for e in (parse_entry(line, today, n, root) for n, line in enumerate(lines[:heading]))
                    if e is not None)
    owners: Dict[str, str] = {}
    for name in sorted(topics):
        for link in LINK_NAME_RE.findall(split_front_matter(topics[name])[1]):  # bodies only
            owners.setdefault(link, name)
    return Layout(lines, heading, entries, owners, today)


# ---- cut, route ------------------------------------------------------------------

def cut_hook(entry: Entry, current: str, today: dt.date) -> str:
    """The lead up to the first `: ` (at most 44 characters, starting with the kind word) unless that would
    lower the entry's latest date. Otherwise `KIND MM-DD` with that date, but only when the date is carried by the
    hook alone: a date that the title or a 20YYMMDD in the file name also carries stays in the line, and
    writing it again as a floating MM-DD could change its year. Then, or without any date, the kind alone."""
    lead = current.split(': ', 1)[0]
    kept = entry_date(entry.title, current, entry.name, today)
    if (len(lead) <= CUT_LIMIT and lead.startswith(entry.kind)
            and entry_date(entry.title, lead, entry.name, today) == kept):
        return lead
    if kept is None or kept in text_dates(entry.title, today) + name_dates(entry.name):
        return entry.kind
    return '{} {}'.format(entry.kind, kept.strftime('%m-%d'))


def has_word(text: str, word: str) -> bool:
    if word not in WHOLE_WORDS:
        return word in text
    return re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])', text) is not None


def route(entry: Entry) -> str:
    if entry.kind == 'GOTCHA':
        return GOTCHAS_FILE
    text = ' '.join((entry.name, entry.title, entry.hook)).lower()
    for target, words in ROUTES:
        if any(has_word(text, word) for word in words):
            return target
    return FALLBACK_FILE


# ---- topic files -----------------------------------------------------------------

def split_front_matter(text: str) -> Tuple[str, str]:
    lines = split_lines(text)
    if lines and split_eol(lines[0])[0] == '---':
        for number in range(1, len(lines)):
            if split_eol(lines[number])[0] == '---':
                return ''.join(lines[:number + 1]), ''.join(lines[number + 1:])
    return '', text


def topic_label(text: str) -> str:
    labels = LABEL_RE.findall(split_front_matter(text)[1])
    return max(dict.fromkeys(labels), key=labels.count) if labels else ''


def default_label(topic_file: str) -> str:
    return topic_file[len('index-'):-len('.md')].replace('-', ' ').capitalize()


def append_links(text: str, links: Sequence[Link], label: str) -> str:
    name = topic_label(text) or label
    lines = ''.join('- {}: {}\n'.format(name, ' · '.join('[{}]({})'.format(t, n) for t, n in group))
                    for group in (links[i:i + LINKS_PER_LINE] for i in range(0, len(links), LINKS_PER_LINE)))
    joiner = '' if not text or text.endswith('\n') else '\n'
    return text + joiner + lines


def count_links(text: str) -> int:
    return len(LINK_NAME_RE.findall(split_front_matter(text)[1]))


def set_description_count(text: str, count: int) -> str:
    head, body = split_front_matter(text)
    return DESCRIPTION_RE.sub(lambda m: m.group(1) + str(count) + m.group(2), head, count=1) + body


# ---- planning --------------------------------------------------------------------

def oldest_key(entry: Entry) -> Tuple[int, int]:
    return (-(entry.age_days or 0), entry.index)


def is_older(entry: Entry, days: int) -> bool:
    """an entry with no known age is never older than anything (it is protected)."""
    return entry.age_days is not None and entry.age_days > days


def next_action(layout: Layout, state: State) -> Optional[Tuple[Step, Entry]]:
    gone = {e.index for e in state.moved}
    hooks = dict(state.hooks)

    def cuttable(e: Entry) -> bool:
        current = hooks.get(e.index, e.hook)
        return cut_hook(e, current, layout.today) != current

    for step in STEPS:
        pool = [e for e in layout.entries
                if e.index not in gone and not e.exempt and e.group in step.groups and is_older(e, step.older_than)
                and (step.move or cuttable(e))]
        if pool:
            return step, min(pool, key=oldest_key)
    return None


def advance(layout: Layout, state: State, step: Step, entry: Entry) -> State:
    if step.move:
        return state._replace(moved=state.moved + (entry,))
    current = dict(state.hooks).get(entry.index, entry.hook)
    kept = tuple(pair for pair in state.hooks if pair[0] != entry.index)
    return state._replace(cut=state.cut + (entry,),
                          hooks=kept + ((entry.index, cut_hook(entry, current, layout.today)),))


def place_moves(files: Mapping[str, str], layout: Layout, moved: Sequence[Entry]) -> Placement:
    owners = dict(layout.owners)
    appended: Dict[str, List[Link]] = {}
    info: List[dict] = []
    already: List[str] = []
    holders: List[str] = []
    for entry in moved:
        if entry.name in owners:
            target = owners[entry.name]
            already.append(entry.name)
            holders.append(target)
        else:
            target = route(entry)
            if target not in files:
                raise ValueError('topic file {} does not exist; cannot move {}'.format(target, entry.name))
            owners[entry.name] = target
            appended.setdefault(target, []).append((entry.title, entry.name))
        info.append({'name': entry.name, 'to': target, 'kind': entry.kind, 'age_days': entry.age_days})
    return Placement(appended, tuple(info), tuple(already), frozenset(holders))


def rewrite_topics(files: Mapping[str, str], appended: Mapping[str, List[Link]],
                   recount: FrozenSet[str]) -> Tuple[Dict[str, str], Dict[str, int]]:
    """Append the new links, then recompute the count of every file that got links or is named in `recount`."""
    texts: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    for name in sorted(set(appended) | {d for d in recount if d in files}):
        text = append_links(files[name], appended[name], default_label(name)) if name in appended else files[name]
        counts[name] = count_links(text)
        texts[name] = set_description_count(text, counts[name])
    return texts, counts


def fix_counts(line: str, counts: Mapping[str, int]) -> str:
    return COUNT_RE.sub(lambda m: '[{} memories]({})'.format(counts[m.group(2)], m.group(2))
                        if m.group(2) in counts else m.group(0), line)


def rewrite_memory(layout: Layout, state: State, counts: Mapping[str, int]) -> str:
    gone = {e.index for e in state.moved}
    hooks = dict(state.hooks)
    by_index = {e.index: e for e in layout.entries}
    out: List[str] = []
    for number, line in enumerate(layout.lines):
        if number in gone:
            continue
        if number in hooks:
            out.append(line[:by_index[number].hook_start] + hooks[number] + split_eol(line)[1])
        else:
            out.append(fix_counts(line, counts) if number >= layout.heading else line)
    return ''.join(out)


def render(files: Mapping[str, str], layout: Layout, state: State, dirty: FrozenSet[str]) -> Rendered:
    placed = place_moves(files, layout, state.moved)
    topics, counts = rewrite_topics(files, placed.appended, dirty | placed.holders)  # holders heal too
    memory = rewrite_memory(layout, state, counts)
    changed = {name: text for name, text in topics.items() if text != files[name]}
    if memory != files[MEMORY_FILE]:
        changed[MEMORY_FILE] = memory
    return Rendered(changed, memory, counts, placed.info, placed.already)


def check_no_loss(files: Mapping[str, str], changed: Mapping[str, str]) -> None:
    def names(texts: Mapping[str, str]) -> set:
        return {link for text in texts.values() for link in LINK_NAME_RE.findall(text)}
    before, after = names(files), names({**files, **changed})
    if before != after:
        raise ValueError('memory link check failed: lost {}, unexpected {}'.format(
            sorted(before - after), sorted(after - before)))


def nbytes(text: str) -> int:
    return len(text.encode('utf-8'))


def build_plan(raw: Mapping[str, bytes], today: dt.date, budget: int, dirty: FrozenSet[str],
               root: Optional[Path] = None) -> Plan:
    files = {name: data.decode('utf-8') for name, data in raw.items()}
    layout = make_layout(files[MEMORY_FILE], {k: v for k, v in files.items() if k != MEMORY_FILE}, today, root)
    state = State()
    while True:
        rendered = render(files, layout, state, dirty)
        action = None if nbytes(rendered.memory) <= budget else next_action(layout, state)
        if action is None:
            break
        state = advance(layout, state, *action)
    check_no_loss(files, rendered.changed)
    size = nbytes(rendered.memory)
    report = {'fits': size <= budget, 'bytes_before': nbytes(files[MEMORY_FILE]), 'bytes_after': size,
              'budget': budget, 'moved': list(rendered.moved),
              'cut': [{'name': e.name, 'kind': e.kind, 'age_days': e.age_days} for e in state.cut],
              'already_linked': list(rendered.already), 'topic_counts': dict(rendered.counts),
              'applied': False, 'exempt': [e.name for e in layout.entries if e.exempt]}
    notes: Dict[str, NoteStamp] = {}
    for item in layout.entries:
        notes.setdefault(item.name, item.note)
    return Plan(rendered.changed, report, notes)


# ---- files -----------------------------------------------------------------------

def index_names(root: Path) -> List[str]:
    return [MEMORY_FILE] + sorted(p.name for p in root.glob('index-*.md'))


def read_files(root: Path) -> Dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in index_names(root)}


def unchanged(root: Path, expected: Mapping[str, bytes], notes: Mapping[str, NoteStamp], target: str) -> bool:
    """The comparison before a replace: the index files (no new or missing one) and the state of every note file
    are what the plan was made from. The file about to be replaced is read last."""
    try:
        names = index_names(root)
        if set(names) != set(expected):
            return False
        if any((root / name).read_bytes() != expected[name] for name in names if name != target):
            return False
        if note_stamps(root, list(notes)) != dict(notes):
            return False
        return (root / target).read_bytes() == expected[target]
    except FileNotFoundError:
        return False


def save_backups(backup_dir: Path, raw: Mapping[str, bytes], names: Sequence[str]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime('%H%M%S')
    for name in names:
        target = backup_dir / '{}.before-{}'.format(name, stamp)
        extra = 1
        while target.exists():
            extra += 1
            target = backup_dir / '{}.before-{}-{}'.format(name, stamp, extra)
        target.write_bytes(raw[name])


def discard(temp: str) -> None:
    try:
        os.unlink(temp)
    except FileNotFoundError:
        pass


def write_temp(path: Path, data: bytes) -> str:
    """write and fsync the temporary file (same folder as `path`); return its name."""
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(handle, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(str(path), temp)
    except BaseException:
        discard(temp)
        raise
    return temp


def write_plan(root: Path, raw: Mapping[str, bytes], changed: Mapping[str, str], backup_dir: Path,
               before_replace: Optional[Callable[[Path], None]], notes: Mapping[str, NoteStamp]) -> None:
    order = sorted(name for name in changed if name != MEMORY_FILE)
    order += [MEMORY_FILE] if MEMORY_FILE in changed else []
    save_backups(backup_dir, raw, order)
    expected = dict(raw)
    written: List[str] = []
    for name in order:
        path, data = root / name, changed[name].encode('utf-8')
        temp = write_temp(path, data)                  # temp file first, then the hook, then the compare ...
        try:
            if before_replace is not None:
                before_replace(path)
            if not unchanged(root, expected, notes, name):
                raise StaleError(frozenset(written))
            os.replace(temp, path)                     # ... and the replace at once
        except BaseException:
            discard(temp)
            raise
        expected[name] = data
        written.append(name)


def quiet_skip(root: Path, quiet_seconds: int) -> Optional[str]:
    """a reason to write nothing when MEMORY.md was modified less than `quiet_seconds` ago (0 = off)."""
    if quiet_seconds <= 0:
        return None
    age = time.time() - (root / MEMORY_FILE).stat().st_mtime
    return 'MEMORY.md changed {} s ago'.format(int(max(age, 0))) if age < quiet_seconds else None


def skipped_report(root: Path, budget: int, reason: str) -> dict:
    """The report of a run that was skipped before anything was parsed."""
    size = (root / MEMORY_FILE).stat().st_size
    return {'skipped': reason, 'applied': False, 'fits': size <= budget, 'bytes_before': size,
            'bytes_after': size, 'budget': budget, 'moved': [], 'cut': [], 'already_linked': [],
            'topic_counts': {}, 'exempt': []}


def compact(memory_dir, today: dt.date, budget: int = DEFAULT_BUDGET, apply: bool = False,
            backup_dir=None, before_replace: Optional[Callable[[Path], None]] = None,
            quiet_seconds: int = 0) -> dict:
    """Plan (and with `apply` carry out) the compaction; return the report dict."""
    root = Path(memory_dir)
    if not (root / MEMORY_FILE).exists():
        return {'skipped': 'missing index', 'fits': True, 'applied': False}
    backups = Path(backup_dir) if backup_dir is not None else default_backup_dir(today)
    skipped = quiet_skip(root, quiet_seconds) if apply else None
    if skipped is not None:
        return skipped_report(root, budget, skipped)
    dirty: FrozenSet[str] = frozenset()
    wrote = False
    for _ in range(MAX_ATTEMPTS):
        raw = read_files(root)
        plan = build_plan(raw, today, budget, dirty, root)
        if not apply or not plan.changed:
            return {**plan.report, 'applied': wrote}
        try:
            write_plan(root, raw, plan.changed, backups, before_replace, plan.notes)
        except StaleError as stale:
            dirty, wrote = dirty | stale.written, wrote or bool(stale.written)
            continue
        return {**plan.report, 'applied': True}
    raise RuntimeError('files kept changing while compacting; gave up after {} attempts'.format(MAX_ATTEMPTS))


# ---- command line -----------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, 'error: {}\n'.format(message))  # exit 2 is reserved for "does not fit"


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = _Parser(description='Compact MEMORY.md to a byte budget (dry run unless --apply).')
    parser.add_argument('--dir', help='memory directory (default: memdir.memory_dir())')
    parser.add_argument('--budget', type=int, default=DEFAULT_BUDGET)
    parser.add_argument('--today', type=dt.date.fromisoformat, default=None, help='YYYY-MM-DD')
    parser.add_argument('--apply', action='store_true', help='write the changes (default: print the plan)')
    parser.add_argument('--backup-dir', help='where the .before-HHMMSS copies go')
    parser.add_argument('--quiet-seconds', type=int, default=DEFAULT_QUIET_SECONDS,
                        help='with --apply, write nothing if MEMORY.md changed less than N s ago (exit 3); 0 = off')
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = compact(Path(args.dir) if args.dir else default_memory_dir(),
                         args.today or dt.date.today(), args.budget, args.apply, args.backup_dir,
                         quiet_seconds=args.quiet_seconds)
    except NoTopicSection:
        print(json.dumps({'skipped': "no '## By topic' section", 'applied': False}, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print('error: {}'.format(error), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    if 'skipped' in report:
        return 0 if report['skipped'] == 'missing index' else 3
    return 0 if report['fits'] else 2


if __name__ == '__main__':
    sys.exit(main())
