"""Behavioral and filesystem safety tests for the memory index compactor."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import memory_index_compact as tool

TODAY = dt.date(2026, 11, 11)
TOPICS = ('infra', 'gotchas', 'memory', 'tools', 'other')
REPORT_KEYS = {'fits', 'bytes_before', 'bytes_after', 'budget', 'moved', 'cut',
               'already_linked', 'topic_counts', 'applied', 'exempt'}   # + exempt
KEEP_KINDS = ('STANDING', 'RULE', 'CONTROLLING', 'FEEDBACK')
OLD = '11-01: old'   # an undated entry with no note file is protected, so "old" tests carry a date


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def links(root):
    return set(re.findall(r'\]\(([^()]+\.md)\)',
                          '\n'.join(p.read_text() for p in root.glob('*.md'))))


def entry(name, hook):
    return '- [{}]({}.md) — {}\n'.format(name, name, hook)


def nbytes(text):
    return len(text.encode('utf-8'))


def local_noon(days_ago):
    """Epoch seconds of local noon, `days_ago` days before TODAY (a local date, as the tool reads it)."""
    return dt.datetime.combine(TODAY - dt.timedelta(days=days_ago), dt.time(12)).timestamp()


class CompactTests(unittest.TestCase):
    def setUp(self):
        lane_tmp = Path(__file__).resolve().parents[1] / 'tmp'  # the build lane's own folder (builder sandbox)
        self.temp = tempfile.TemporaryDirectory(dir=lane_tmp if lane_tmp.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'memory'
        self.root.mkdir()
        self.backup = Path(self.temp.name) / 'backup'

    def write_topics(self):
        refs = []
        for topic in TOPICS:
            name = 'index-' + topic + '.md'
            (self.root / name).write_text(
                '---\ndescription: "Topic memories (999)"\nmodified: 2000-01-01\n---\n'
                '\n- Label: [seed](seed.md)\n')
            refs.append('[999 memories](' + name + ')')
        return refs

    def seed(self, entries):
        refs = self.write_topics()
        text = '# Index\r\n\r\n' + entries + '\n## By topic (all)\n' + ' · '.join(refs) + '\n'
        (self.root / 'MEMORY.md').write_bytes(text.encode())
        return len(text.encode())

    def note(self, name, days_ago):
        """The note file `<name>.md` of an entry, last modified `days_ago` days before TODAY."""
        path = self.root / (name + '.md')
        path.write_text('note\n')
        os.utime(path, (local_noon(days_ago), local_noon(days_ago)))

    def memory_text(self):
        return (self.root / 'MEMORY.md').read_bytes().decode('utf-8')

    def tmp_files(self):
        return sorted(p.name for p in self.root.iterdir() if p.name.endswith('.tmp'))

    def run_compact(self, budget=tool.DEFAULT_BUDGET, apply=False, **kwargs):
        return tool.compact(self.root, TODAY, budget, apply,
                            backup_dir=self.backup, **kwargs)

    def parse(self, line, root=None):
        return tool.parse_entry(line, TODAY, root=root)

    # ---- parsing, dating, kinds -------------------------------------------------

    def test_parse_and_dates(self):
        # the 60-character window is gone, so the date after 60 characters now counts.
        cases = [('RULE 12-31: wrap', 315), ('RULE 11/01: slash', 10),
                 ('RULE 10-01, 11-03: latest', 8),
                 ('RULE 11-03, 10-01: latest', 8),
                 ('RULE ' + 'a' * 60 + ' 11-10', 1),
                 ('RULE 13-01 00/03 11-32', None), ('no date', None)]
        for hook, age in cases:
            with self.subTest(hook=hook):
                parsed = self.parse(entry('a', hook))
                self.assertEqual(parsed.age_days, age)
        self.assertEqual(self.parse(entry('a', 'RULE: yes')).kind, 'RULE')
        self.assertEqual(self.parse(entry('a', 'ABC: no')).kind, '')
        self.assertIsNone(self.parse(entry('a', 'RULE [two](b.md)')))
        self.assertIsNone(self.parse('other [a](a.md) — RULE\n'))

    def test_date_boundaries(self):
        # a date ending at character 61 or later counts too.
        last_in = 'x' * 55 + '11-10'
        first_out = 'x' * 56 + '11-10'
        cases = [('12-11', -30), ('12-12', (TODAY - dt.date(2025, 12, 12)).days),
                 ('02-30', None), ('02-29', None), ('2026-11-10', 1),
                 (last_in, 1), (first_out, 1), ('11-20 and 11-09', -9)]
        for hook, age in cases:
            with self.subTest(hook=hook):
                self.assertEqual(self.parse(entry('a', hook)).age_days, age)

    def test_date_in_hook_after_char_60_title_and_file_name(self):
        # the live case (date at character 65), a date only in the title, a 20YYMMDD in the name.
        live = 'DECISION: ' + 'a' * 50 + '11-09: below the limit'
        self.assertEqual(live.index('11-09') + 5, 65)
        self.assertEqual(self.parse(entry('pricing', live)).age_days, 2)
        self.assertEqual(self.parse('- [Policy 11-10](p.md) — DECISION: no date in the hook\n').age_days, 1)
        named = '- [Pricing](example-pricing-20261016.md) — {}\n'
        self.assertEqual(self.parse(named.format('DECISION: no other date')).age_days,
                         (TODAY - dt.date(2026, 10, 16)).days)
        self.assertEqual(self.parse(named.format('DECISION 11-09: a later date wins')).age_days, 2)
        self.assertEqual(self.parse('- [P](x-20250101.md) — DECISION 11-01: x\n').age_days, 10)  # smaller age wins
        self.assertIsNone(self.parse('- [P](x-20261399.md) — DECISION: bad name date\n').age_days)

    def test_age_is_the_smaller_of_date_and_note_modification(self):
        # note file `<root>/<name>` modified date (local) against the date in the entry.
        self.note('yest', 1)
        self.note('old40', 40)
        self.note('both', 20)
        self.assertEqual(self.parse(entry('yest', 'DECISION: no date'), self.root).age_days, 1)
        self.assertEqual(self.parse(entry('old40', 'DECISION: no date'), self.root).age_days, 40)
        self.assertEqual(self.parse(entry('yest', 'DECISION 10-12: 30 days'), self.root).age_days, 1)
        self.assertEqual(self.parse(entry('both', 'DECISION 11-10: date is younger'), self.root).age_days, 1)
        self.assertIsNone(self.parse(entry('nofile', 'DECISION: no date'), self.root).age_days)
        self.assertEqual(self.parse(entry('nofile', 'DECISION 11-01: date only'), self.root).age_days, 10)

    def test_note_modification_is_a_local_date(self):
        for hour in (0, 23):
            with self.subTest(hour=hour):
                stamp = dt.datetime(2026, 11, 5, hour, 30).timestamp()
                path = self.root / 'late.md'
                path.write_text('note\n')
                os.utime(path, (stamp, stamp))
                self.assertEqual(self.parse(entry('late', 'DECISION: x'), self.root).age_days, 6)

    def test_future_date_or_modification_time_is_protected(self):
        self.note('tomorrow', -1)
        self.assertEqual(self.parse(entry('tomorrow', 'DECISION: x'), self.root).age_days, -1)
        self.assertEqual(self.parse(entry('tomorrow', 'DECISION 11-20: x'), self.root).age_days, -9)

    def test_note_state_is_nanoseconds_missing_or_error(self):
        # the record of one note file: its modification time in ns, "missing", or "error".
        self.note('here', 3)
        self.assertEqual(tool.note_stamp(self.root, 'here.md'), (self.root / 'here.md').stat().st_mtime_ns)
        self.assertEqual(tool.note_stamp(self.root, 'gone.md'), 'missing')
        (self.root / 'afile').write_text('x')
        self.assertEqual(tool.note_stamp(self.root, 'afile/under.md'), 'missing')       # NotADirectoryError
        real_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path.name == 'here.md':
                raise PermissionError(13, 'denied')
            return real_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', denied):
            self.assertEqual(tool.note_stamp(self.root, 'here.md'), 'error')
        self.assertEqual(tool.note_stamp(self.root, 'bad\0name.md'), 'error')           # ValueError: cannot be examined

    def test_note_that_cannot_be_examined_protects_its_entry(self):
        # a PermissionError from stat is not "no note file": the entry is protected
        # even though its text carries dates; its neighbours are not affected.
        self.seed(entry('x', 'STANDING 10-01: ' + 'a' * 60) + entry('d', 'DECISION 10-01: ' + 'b' * 60)
                  + entry('y', 'REFERENCE 10-01: old'))
        real_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path.name in ('x.md', 'd.md'):
                raise PermissionError(13, 'denied')
            return real_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', denied):
            self.assertIsNone(self.parse(entry('x', 'STANDING 10-01: x'), self.root).age_days)
            report = self.run_compact(0, True)
        self.assertEqual([m['name'] for m in report['moved']], ['y.md'])
        self.assertEqual(report['cut'], [])
        text = self.memory_text()
        self.assertIn(entry('x', 'STANDING 10-01: ' + 'a' * 60), text)
        self.assertIn(entry('d', 'DECISION 10-01: ' + 'b' * 60), text)

    def test_a_note_under_a_file_counts_as_no_note(self):
        # NotADirectoryError is a confirmed "no such note": the dates of the text decide.
        (self.root / 'sub').write_text('not a directory')
        self.seed('- [z](sub/z.md) — 11-01: old\n')
        self.assertEqual(self.parse('- [z](sub/z.md) — 11-01: old\n', self.root).age_days, 10)
        self.assertEqual([m['name'] for m in self.run_compact(0, True)['moved']], ['sub/z.md'])

    def test_note_touched_inside_the_hook_makes_the_run_replan(self):
        # the note files are compared again before each replace.
        for action in ('touch', 'delete'):
            with self.subTest(action=action):
                self.seed(entry('x', 'old') + entry('y', OLD))
                self.note('x', 40)
                calls = []

                def hook(path):
                    calls.append(path.name)
                    if len(calls) == 1:
                        if action == 'touch':
                            os.utime(self.root / 'x.md', (local_noon(0), local_noon(0)))
                        else:
                            (self.root / 'x.md').unlink()
                report = self.run_compact(0, True, before_replace=hook)
                self.assertEqual(calls, ['index-infra.md', 'index-infra.md', 'MEMORY.md'])   # a second attempt
                self.assertEqual([m['name'] for m in report['moved']], ['y.md'])
                self.assertIn(entry('x', 'old'), self.memory_text())                        # left alone
                self.assertNotIn('(y.md)', self.memory_text().split('## By topic')[0])
                self.assertEqual(self.tmp_files(), [])

    def test_note_touched_in_every_attempt_exhausts_the_retries(self):
        self.seed(entry('x', 'old'))
        self.note('x', 40)
        calls = []

        def hook(path):
            calls.append(path.name)
            stamp = local_noon(40) + len(calls)
            os.utime(self.root / 'x.md', (stamp, stamp))
        with self.assertRaisesRegex(RuntimeError, '3'):
            self.run_compact(0, True, before_replace=hook)
        self.assertEqual(len(calls), 3)
        self.assertIn(entry('x', 'old'), self.memory_text())
        self.assertEqual(self.tmp_files(), [])

    def test_kind_classes_and_fields(self):
        keep = KEEP_KINDS
        dated = ('GOTCHA', 'REFERENCE', 'RETIRED', 'CAPITALS', '')
        for kind in keep + dated + ('DECISION',):
            with self.subTest(kind=kind):
                parsed = self.parse(entry('a', (kind + ' 11-01: x').strip()))
                self.assertEqual(parsed.kind, kind)
                self.assertEqual(parsed.keep_always, kind in keep)
                # a decision is its own group, between the rules and the rest
                self.assertEqual(parsed.group, 'rule' if kind in keep else 'decision' if kind == 'DECISION' else 'dated')
        parsed = self.parse('- [A title (x)](some name.md) — RULE 11-01: x\r\n')
        self.assertEqual((parsed.title, parsed.name), ('A title (x)', 'some name.md'))

    def test_a_second_link_opener_makes_the_line_a_non_entry(self):
        # exactly one `](` in the whole line.
        self.assertIsNone(self.parse(entry('a', '11-01: odd [x]y](other.md) tail')))
        self.assertIsNone(self.parse('- [a](a.md) — 11-01: ](\n'))
        self.assertIsNotNone(self.parse(entry('a', '11-01: fine (brackets) [x] and (parens)')))

    def cut(self, hook, title='t', name='t'):
        parsed = self.parse('- [{}]({}.md) — {}\n'.format(title, name, hook))
        cut = tool.cut_hook(parsed, hook, TODAY)
        again = self.parse('- [{}]({}.md) — {}\n'.format(title, name, cut))
        self.assertEqual(tool.cut_hook(again, cut, TODAY), cut)       # a cut hook is not cut again
        self.assertEqual(again.age_days, parsed.age_days)             # and keeps its age
        return cut

    def test_cut_hook_rules(self):
        # the cut gets the entry and today; a long lead keeps the date (`KIND MM-DD`).
        part44 = 'RULE ' + 'a' * 39
        self.assertEqual(len(part44), 44)
        cases = [('RULE 11-01: details', 'RULE 11-01'),
                 (part44 + ': x', part44),
                 (part44 + 'a: x', 'RULE'),                          # no date anywhere: the kind alone
                 ('RULE ' + 'a' * 60, 'RULE'),
                 ('RULE 11-01', 'RULE 11-01'),
                 ('RULE: yes', 'RULE'),
                 ('RULE 11-01: a: b', 'RULE 11-01'),
                 ('RULE ' + 'a' * 50 + ' 11-01: x', 'RULE 11-01'),   # long lead: date kept
                 ('RULE ' + 'a' * 50 + ' 11-01 and 11/05: x', 'RULE 11-05')]
        for hook, expected in cases:
            with self.subTest(hook=hook):
                self.assertEqual(self.cut(hook), expected)

    def test_cut_never_removes_the_only_date(self):
        # the lead `DECISION` is short enough, but the date stands after the first `: `.
        self.assertEqual(self.cut('DECISION: policy choices; revise on error; 11-05: below the limit'),
                         'DECISION 11-05')
        self.assertEqual(self.cut('STANDING: 10-26 x'), 'STANDING 10-26')
        # a date that the title or the file name carries is not lost by a cut of the hook
        self.assertEqual(self.cut('STANDING: later 11-05 text', title='Rule 11-05'), 'STANDING')
        self.assertEqual(self.cut('STANDING: nothing', name='x-20261016'), 'STANDING')
        # when the title carries the latest date too, the fallback is the kind alone
        self.assertEqual(self.cut('STANDING ' + 'a' * 60 + ' 11-05', title='T 11-05'), 'STANDING')
        self.assertEqual(self.cut('STANDING ' + 'a' * 60 + ' 11-05', name='x-20261105'), 'STANDING')
        self.assertEqual(self.cut('STANDING ' + 'a' * 60 + ' 11-05', title='T 10-05'), 'STANDING 11-05')

    def test_cut_fallback_does_not_redate_an_entry_dated_by_its_file_name(self):
        # `old-rule-20241016.md` with a long lead used to become `STANDING 10-16` = 2026-10-16.
        line = '- [Old rule](old-rule-20241016.md) — STANDING ' + 'a' * 60 + '\n'
        self.assertEqual(self.parse(line).age_days, 756)
        self.seed(line)
        report = self.run_compact(0, True)
        self.assertEqual([c['name'] for c in report['cut']], ['old-rule-20241016.md'])
        self.assertIn('- [Old rule](old-rule-20241016.md) — STANDING\n', self.memory_text())
        cut_line = '- [Old rule](old-rule-20241016.md) — STANDING\n'
        self.assertEqual(self.parse(cut_line).age_days, 756)          # the same age on the next run
        self.assertFalse(self.run_compact(0, True)['applied'])

    def test_keep_marker_in_the_lead_of_the_hook(self):
        # the capital word KEEP, a word of its own, in the lead (the part before the first `: `).
        exempt = ['DECISION 11-01 KEEP: the owner stopped the job; do not re-enable without approval',
                  'STANDING KEEP: x', 'KEEP', 'RULE KEEP, 11-01: x', 'KEEP 11-01 and no colon at all']
        plain = ['DECISION 11-01: KEEP the old copy', 'KEEPER: x', 'keep: x', 'Keep: x', 'KEEPS: x',
                 'DECISION 11-01 UNKEEP: x', 'STANDING: x', 'RULE 11-01: x KEEP', 'no marker']
        for hook in exempt:
            with self.subTest(hook=hook):
                self.assertTrue(self.parse(entry('a', hook)).exempt)
        for hook in plain:
            with self.subTest(hook=hook):
                self.assertFalse(self.parse(entry('a', hook)).exempt)
        # the marker changes neither the kind nor the dates
        marked, unmarked = self.parse(entry('a', 'DECISION 11-01 KEEP: x')), self.parse(entry('a', 'DECISION 11-01: x'))
        self.assertEqual((marked.kind, marked.group, marked.age_days), (unmarked.kind, unmarked.group, unmarked.age_days))
        self.assertEqual((marked.kind, marked.age_days), ('DECISION', 10))

    def test_exempt_entries_are_never_cut_or_moved(self):
        # at budget 0 the marked lines of every kind and age stay byte for byte; their twins go.
        pad = 'z' * 60
        keep = {'keep-rule': 'STANDING 10-01 KEEP: ' + pad, 'keep-dec': 'DECISION 10-01 KEEP: ' + pad,
                'keep-plain': '10-01 KEEP: ' + pad, 'keep-note': 'KEEP: no date here'}
        twin = {'twin-rule': 'STANDING 10-01: ' + pad, 'twin-dec': 'DECISION 10-01: ' + pad,
                'twin-plain': '10-01: ' + pad, 'twin-note': 'no date here'}
        self.seed(entry('keep-rule', keep['keep-rule']) + entry('twin-rule', twin['twin-rule'])
                  + entry('keep-dec', keep['keep-dec']) + entry('twin-dec', twin['twin-dec'])
                  + entry('keep-plain', keep['keep-plain']) + entry('twin-plain', twin['twin-plain'])
                  + entry('keep-note', keep['keep-note']) + entry('twin-note', twin['twin-note']))
        self.note('keep-note', 40)
        self.note('twin-note', 40)
        dry = self.run_compact(0)
        self.assertEqual(dry['exempt'], ['keep-rule.md', 'keep-dec.md', 'keep-plain.md', 'keep-note.md'])
        report = self.run_compact(0, True)
        self.assertFalse(report['fits'])                           # the exempt lines still count
        self.assertEqual([m['name'] for m in report['moved']], ['twin-plain.md', 'twin-note.md', 'twin-dec.md'])
        self.assertEqual([c['name'] for c in report['cut']], ['twin-rule.md', 'twin-dec.md'])
        self.assertEqual(report['exempt'], ['keep-rule.md', 'keep-dec.md', 'keep-plain.md', 'keep-note.md'])
        text = self.memory_text()
        for name, hook in keep.items():
            self.assertIn(entry(name, hook), text)
        self.assertIn(entry('twin-rule', 'STANDING 10-01'), text)
        for name in twin:
            self.assertEqual(name in ('twin-rule',), '(' + name + '.md)' in text.split('## By topic')[0])
        after = snapshot(self.root)
        second = self.run_compact(0, True)                         # a second run is a no-op
        self.assertEqual((second['moved'], second['cut'], second['applied']), ([], [], False))
        self.assertEqual(second['exempt'], report['exempt'])
        self.assertEqual(snapshot(self.root), after)

    # ---- routing ----------------------------------------------------------------

    def test_routing(self):
        for hook, topic in [('memory note', 'memory'), ('agent tools', 'tools'),
                            ('nothing special', 'infra'), ('GOTCHA 11-01', 'gotchas')]:
            with self.subTest(hook=hook):
                self.assertEqual(tool.route(self.parse(entry('zz', hook))), 'index-' + topic + '.md')

    # ---- steps ------------------------------------------------------------------

    def staged(self):
        return (entry('dated-old', 'REFERENCE 11-01: ' + 'a' * 120)
                + entry('keep-old', 'RULE 11-01: ' + 'b' * 120)
                + entry('dated-mid', 'REFERENCE 11-07: ' + 'c' * 120)
                + entry('keep-mid', 'STANDING 11-07: ' + 'd' * 120)
                + entry('protected', 'RULE 11-09: ' + 'e' * 120))

    def test_all_step_boundaries(self):
        # step 6 is gone; after the cuts of step 4 nothing else may happen to a rule.
        initial = self.seed(self.staged())
        count_delta = len('[2 memories]') - len('[999 memories]')
        after1 = initial - len(entry('dated-old', 'REFERENCE 11-01: ' + 'a' * 120).encode()) + count_delta
        after2 = after1 - len((': ' + 'b' * 120).encode())
        after3 = after2 - len(entry('dated-mid', 'REFERENCE 11-07: ' + 'c' * 120).encode())
        after4 = after3 - len((': ' + 'd' * 120).encode())
        expected = [(after1, ['dated-old.md'], []),
                    (after2, ['dated-old.md'], ['keep-old.md']),
                    (after3, ['dated-old.md', 'dated-mid.md'], ['keep-old.md']),
                    (after4, ['dated-old.md', 'dated-mid.md'], ['keep-old.md', 'keep-mid.md'])]
        for budget, moved, cut in expected:
            with self.subTest(budget=budget):
                report = self.run_compact(budget)
                self.assertTrue(report['fits'])
                self.assertEqual(report['bytes_after'], budget)
                self.assertEqual([x['name'] for x in report['moved']], moved)
                self.assertEqual([x['name'] for x in report['cut']], cut)
        for budget in (after4 - 1, 0):                      # nothing is left to do: fits is false
            report = self.run_compact(budget)
            self.assertEqual((report['fits'], report['bytes_after']), (False, after4))
            self.assertEqual([x['name'] for x in report['moved']], ['dated-old.md', 'dated-mid.md'])
            self.assertEqual([x['name'] for x in report['cut']], ['keep-old.md', 'keep-mid.md'])

    def test_seven_and_two_day_boundaries(self):
        # Added: "older than 7" and "older than 2" are strict; ages 7 and 3 fall to the later steps.
        kinds = {'d': ('REFERENCE', 'a'), 'k': ('RULE', 'b')}
        days = {'2': '09', '3': '08', '7': '04', '8': '03'}
        lines = {c + n: entry(c + n, '{} 11-{}: {}'.format(kinds[c][0], days[n], kinds[c][1] * 100))
                 for c in 'dk' for n in days}
        dated = {k: v for k, v in lines.items() if k[0] == 'd'}
        keep = {k: v for k, v in lines.items() if k[0] == 'k'}
        initial = self.seed(''.join(lines[c + n] for n in '2378' for c in 'kd'))
        after1 = initial - nbytes(dated['d8']) - 2
        after2 = after1 - nbytes(': ' + 'b' * 100)
        report = self.run_compact(after1)
        self.assertEqual(([m['name'] for m in report['moved']], report['cut']), (['d8.md'], []))
        report = self.run_compact(after2)
        self.assertEqual(([m['name'] for m in report['moved']], [c['name'] for c in report['cut']]),
                         (['d8.md'], ['k8.md']))
        self.assertEqual((report['moved'][0]['kind'], report['moved'][0]['age_days'],
                          report['moved'][0]['to']), ('REFERENCE', 8, 'index-infra.md'))
        self.assertEqual((report['cut'][0]['kind'], report['cut'][0]['age_days']), ('RULE', 8))
        report = self.run_compact(0, True)
        # the rules are cut but never moved
        self.assertEqual([m['name'] for m in report['moved']], ['d8.md', 'd7.md', 'd3.md'])
        self.assertEqual([c['name'] for c in report['cut']], ['k8.md', 'k7.md', 'k3.md'])
        text = self.memory_text()
        self.assertIn(dated['d2'], text)
        self.assertIn(keep['k2'], text)
        for name, day in (('k8', '03'), ('k7', '04'), ('k3', '08')):
            self.assertIn('- [{0}]({0}.md) — RULE 11-{1}\n'.format(name, day), text)
        self.assertEqual(text.count('\n- ['), 5)

    def test_decisions_are_cut_like_rules_and_moved_only_after_everything_dated(self):
        # a decision is cut like a rule (steps 2, 4) and moved in a step of
        # its own, after every dated entry. the rule itself is never moved.
        lines = {'ref-old': entry('ref-old', 'REFERENCE 10-20: ' + 'a' * 80),
                 'dec-old': entry('dec-old', 'DECISION 10-21: ' + 'b' * 80),
                 'rule-old': entry('rule-old', 'STANDING 09-19: ' + 'c' * 80),
                 'ref-mid': entry('ref-mid', 'GOTCHA 11-07: ' + 'd' * 80),
                 'dec-mid': entry('dec-mid', 'DECISION 11-06: ' + 'e' * 80),
                 'dec-new': entry('dec-new', 'DECISION 11-10: ' + 'f' * 80)}
        initial = self.seed(''.join(lines.values()))
        report = self.run_compact(initial - 1)                      # one byte over: step 1 only
        self.assertEqual(([m['name'] for m in report['moved']], report['cut']), (['ref-old.md'], []))
        report = self.run_compact(report['bytes_after'] - 1)        # step 2: the oldest of rules and decisions
        self.assertEqual([c['name'] for c in report['cut']], ['rule-old.md'])
        report = self.run_compact(report['bytes_after'] - 1)
        self.assertEqual(([m['name'] for m in report['moved']], [c['name'] for c in report['cut']]),
                         (['ref-old.md'], ['rule-old.md', 'dec-old.md']))
        report = self.run_compact(0, True)                          # everything the steps allow
        self.assertFalse(report['fits'])
        self.assertEqual([m['name'] for m in report['moved']],
                         ['ref-old.md', 'ref-mid.md', 'dec-old.md', 'dec-mid.md'])
        self.assertEqual([c['name'] for c in report['cut']], ['rule-old.md', 'dec-old.md', 'dec-mid.md'])
        self.assertIn(lines['dec-new'], self.memory_text())
        self.assertIn('- [rule-old](rule-old.md) — STANDING 09-19\n', self.memory_text())

    def test_budget_zero_never_moves_a_rule(self):
        # with budget 0 every KEEP-ALWAYS entry is still in MEMORY.md (cut at most) and fits is false.
        rules = ''.join(entry(kind.lower() + str(n), '{} 10-{:02d}: {}'.format(kind, 10 + n, 'x' * 60))
                        for n, kind in enumerate(KEEP_KINDS))
        self.seed(rules + entry('dec', 'DECISION 10-10: ' + 'y' * 60) + entry('ref', 'REFERENCE 10-10: z'))
        report = self.run_compact(0, True)
        self.assertFalse(report['fits'])
        self.assertEqual(sorted(m['name'] for m in report['moved']), ['dec.md', 'ref.md'])
        self.assertFalse([m for m in report['moved'] if m['kind'] in KEEP_KINDS])
        text = self.memory_text()
        for n, kind in enumerate(KEEP_KINDS):
            self.assertIn('- [{0}{1}]({0}{1}.md) — {2} 10-{3:02d}\n'.format(kind.lower(), n, kind, 10 + n), text)
        self.assertFalse(self.run_compact(0, True)['applied'])      # and a second run changes nothing

    def test_age_order_and_protection(self):
        # an undated entry without a note file is protected, not "oldest".
        self.seed(entry('recent', '11-09: keep') + entry('future', '11-20: keep')
                  + entry('tie1', '11-01: old') + entry('tie2', '11-01: old')
                  + entry('older', '10-01: old') + entry('undated', 'old'))
        report = self.run_compact(0, True)
        self.assertFalse(report['fits'])
        self.assertEqual([m['name'] for m in report['moved']], ['older.md', 'tie1.md', 'tie2.md'])
        text = (self.root / 'MEMORY.md').read_text()
        self.assertIn(entry('recent', '11-09: keep'), text)
        self.assertIn(entry('future', '11-20: keep'), text)
        self.assertIn(entry('undated', 'old'), text)
        self.assertFalse(self.run_compact(0, True)['applied'])

    def test_undated_entries_follow_their_note_file(self):
        # undated + note yesterday = untouched; undated + note 40 days old = aged 40;
        # dated 30 days ago + note yesterday = untouched; undated + no file = untouched, fits false.
        self.seed(entry('yest', 'old') + entry('old40', 'old') + entry('both', '10-12: old')
                  + entry('nofile', 'old'))
        self.note('yest', 1)
        self.note('old40', 40)
        self.note('both', 1)
        report = self.run_compact(0, True)
        self.assertFalse(report['fits'])
        self.assertEqual([(m['name'], m['age_days']) for m in report['moved']], [('old40.md', 40)])
        text = self.memory_text()
        for name, hook in (('yest', 'old'), ('both', '10-12: old'), ('nofile', 'old')):
            self.assertIn(entry(name, hook), text)
        self.assertNotIn('old40.md', text.split('## By topic')[0])

    def test_young_entries_of_every_kind_are_never_touched(self):
        young = ''.join(entry(kind.lower() + str(age), '{} 11-{:02d}: {}'.format(kind, 11 - age, 'z' * 50))
                        for kind in ('RULE', 'GOTCHA', 'STANDING', 'DECISION') for age in (0, 1, 2))
        self.seed(young)
        before = self.memory_text()
        report = self.run_compact(0, True)
        self.assertEqual((report['moved'], report['cut'], report['applied']), ([], [], False))
        self.assertFalse(report['fits'])
        self.assertEqual(self.memory_text(), before)

    def test_cut_fallback_and_already_cut(self):
        # the long lead is cut to `STANDING 11-01` (date kept); a second run leaves it alone.
        self.seed(entry('long', 'STANDING 11-01 ' + 'a' * 50 + ': details')
                  + entry('short', 'FEEDBACK 11-01: details') + entry('cut', 'STANDING 11-01'))
        report = self.run_compact(0, True)
        self.assertEqual([c['name'] for c in report['cut']], ['long.md', 'short.md'])
        self.assertEqual(report['moved'], [])
        self.assertIn('- [long](long.md) — STANDING 11-01\n', self.memory_text())
        after = snapshot(self.root)
        second = self.run_compact(0, True)
        self.assertEqual((second['cut'], second['moved'], second['applied']), ([], [], False))
        self.assertEqual(snapshot(self.root), after)

    def test_cut_result_text(self):
        # Added: check the text of the cut hooks (budget 0 would hide it behind a move).
        initial = self.seed(entry('long', 'STANDING 11-01 ' + 'a' * 50 + ': details')
                            + entry('short', 'FEEDBACK 11-01: details') + entry('cut', 'STANDING 11-01'))
        budget = initial - nbytes(': details') - nbytes(': details') - nbytes('a' * 50 + ' ')
        report = self.run_compact(budget, True)
        self.assertTrue(report['fits'])
        self.assertEqual(report['moved'], [])
        text = self.memory_text()
        self.assertIn('- [long](long.md) — STANDING 11-01\n', text)
        self.assertIn('- [short](short.md) — FEEDBACK 11-01\n', text)
        self.assertIn('- [cut](cut.md) — STANDING 11-01\n', text)
        self.assertEqual(nbytes(text), budget)

    def test_cut_keeps_a_late_date_in_the_file(self):
        # Through the whole tool: the live case shape, aged 6 days, lead `DECISION` followed by a late date.
        hook = 'DECISION: policy choices; revise on error; 11-05: below the limit'
        initial = self.seed(entry('late', hook))
        budget = initial - (nbytes(hook) - nbytes('DECISION 11-05'))      # stop right after the cut
        report = self.run_compact(budget, True)
        self.assertEqual((report['moved'], [c['name'] for c in report['cut']]), ([], ['late.md']))
        self.assertIn('- [late](late.md) — DECISION 11-05\n', self.memory_text())
        self.assertFalse(self.run_compact(budget, True)['applied'])
        self.assertEqual([m['name'] for m in self.run_compact(0)['moved']], ['late.md'])   # a decision may go in step 5

    # ---- dry run, write rules ---------------------------------------------------

    def test_dry_run_and_within_budget(self):
        self.seed(self.staged())
        before = snapshot(Path(self.temp.name))
        self.run_compact(0)
        self.assertEqual(before, snapshot(Path(self.temp.name)))
        self.assertFalse(self.backup.exists())          # added: not even the backup directory
        result = self.run_compact(100000, True)
        self.assertFalse(result['applied'])
        self.assertEqual(before, snapshot(Path(self.temp.name)))
        self.assertFalse(self.backup.exists())

    def test_report_shape(self):
        self.seed(self.staged())
        report = self.run_compact(0)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report['budget'], 0)
        self.assertEqual(set(report['moved'][0]), {'name', 'to', 'kind', 'age_days'})
        self.assertEqual(set(report['cut'][0]), {'name', 'kind', 'age_days'})
        self.assertFalse(report['applied'])
        self.assertEqual(report['exempt'], [])                    # no marker, an empty list
        json.dumps(report)

    def test_counts_dedup_grouping_conservation_atomic_backup(self):
        self.seed(''.join(entry('old' + str(n), OLD) for n in range(7)))
        other = self.root / 'index-other.md'
        other.write_text(other.read_text() + '- Label: [already](old0.md)\n')
        before = snapshot(self.root)
        names = links(self.root)
        real_replace = os.replace
        replacements = []

        def check_replace(src, dst):
            self.assertEqual(Path(src).parent, Path(dst).parent)
            for filename in ('MEMORY.md', 'index-infra.md'):
                backups = list(self.backup.glob(filename + '.before-*'))
                self.assertTrue(backups)
                self.assertEqual(backups[0].read_bytes(), before[filename])
            replacements.append(Path(dst).name)
            real_replace(src, dst)

        with patch.object(tool.os, 'replace', side_effect=check_replace):
            report = self.run_compact(0, True)
        self.assertEqual(links(self.root), names)
        self.assertEqual(report['already_linked'], ['old0.md'])
        # the owner of the already-linked entry (index-other) is counted too (its body holds 2 links)
        self.assertEqual(report['topic_counts'], {'index-other.md': 2, 'index-infra.md': 7})
        topic = (self.root / 'index-infra.md').read_text()
        self.assertIn('memories (7)', topic)
        self.assertIn('modified: 2000-01-01', topic)
        self.assertIn('[7 memories](index-infra.md)', (self.root / 'MEMORY.md').read_text())
        new_lines = [line for line in topic.splitlines() if 'old' in line]
        self.assertEqual([line.count('](') for line in new_lines], [3, 3])
        self.assertEqual(replacements[-1], 'MEMORY.md')
        self.assertEqual(replacements[:-1], sorted(replacements[:-1]))      # topic files first, MEMORY.md last
        self.assertEqual((self.root / 'MEMORY.md').read_bytes()[:11], before['MEMORY.md'][:11])
        after = snapshot(self.root)
        self.assertFalse(self.run_compact(0, True)['applied'])
        self.assertEqual(snapshot(self.root), after)

    def test_counts_only_for_changed_topics_and_clean_directory(self):
        # Added: the other counts on the same line stay; only the two changed files are backed up
        # (file name + .before-HHMMSS); no temporary file is left behind; only those two are rewritten.
        self.seed(entry('old', OLD))
        before = snapshot(self.root)
        self.run_compact(0, True)
        text = self.memory_text()
        for topic in TOPICS[1:]:
            self.assertIn('[999 memories](index-{}.md)'.format(topic), text)
        self.assertIn('[2 memories](index-infra.md)', text)
        after = snapshot(self.root)
        self.assertEqual({k for k in after if after[k] != before[k]}, {'MEMORY.md', 'index-infra.md'})
        saved = sorted(p.name for p in self.backup.iterdir())
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(re.fullmatch(r'(MEMORY|index-infra)\.md\.before-\d{6}', n) for n in saved), saved)
        self.assertEqual(self.tmp_files(), [])

    def test_loss_guard(self):
        self.seed(entry('old', OLD))
        before = snapshot(self.root)
        with patch.object(tool, 'append_links', return_value='---\ndescription: memories (0)\n---\n'):
            with self.assertRaisesRegex(ValueError, 'link'):
                self.run_compact(0, True)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.backup.exists())

    def test_missing_heading_and_topic(self):
        self.seed(entry('old', OLD))
        (self.root / 'index-infra.md').unlink()
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, 'index-infra.md'):
            self.run_compact(0, True)
        self.assertEqual(snapshot(self.root), before)
        (self.root / 'MEMORY.md').write_text('no heading\n')
        with self.assertRaisesRegex(ValueError, 'By topic'):
            self.run_compact(100000, True)
        self.assertFalse(self.backup.exists())

    def test_non_entry_lines_byte_for_byte(self):
        # Added: no trailing newline, CRLF and trailing spaces survive; a two-link line and an
        # entry-shaped line after the heading are not entries; counts of unchanged topics stay.
        # the rule `b` is cut, not moved, so it stays (with its CRLF) and only `a` goes.
        self.write_topics()
        raw = ('# T\r\n  indented line  \n\n- [bad](bad.md) — has [other](o.md) link\n'
               '- [a](a.md) — 11-01: old\r\n- [b](b.md) — RULE 11-01: details\r\n'
               '## By topic x\n- [new](n.md) — old after heading\n'
               '- X: [999 memories](index-infra.md) · [999 memories](index-other.md)')
        (self.root / 'MEMORY.md').write_bytes(raw.encode())
        names = links(self.root)
        self.run_compact(0, True)
        self.assertEqual(self.memory_text(),
                         '# T\r\n  indented line  \n\n- [bad](bad.md) — has [other](o.md) link\n'
                         '- [b](b.md) — RULE 11-01\r\n'
                         '## By topic x\n- [new](n.md) — old after heading\n'
                         '- X: [2 memories](index-infra.md) · [999 memories](index-other.md)')
        self.assertEqual(links(self.root), names)
        self.assertIn('- Label: [a](a.md)\n', (self.root / 'index-infra.md').read_text())
        # a cut line keeps its own CRLF
        self.write_topics()
        raw = ('# T\n- [k](k.md) — RULE 11-01: details\r\n## By topic\n'
               '- X: [5 memories](index-infra.md)\n')
        (self.root / 'MEMORY.md').write_bytes(raw.encode())
        report = self.run_compact(nbytes(raw) - nbytes(': details'), True)
        self.assertTrue(report['fits'])
        self.assertEqual(self.memory_text(), raw.replace(': details', ''))

    def test_line_with_a_second_link_opener_is_left_alone(self):
        # such a line would be moved; now it is not an entry, and its link survives.
        odd = entry('odd', '11-01: has [x]y](other.md) in the hook')
        self.seed(odd + entry('old', OLD))
        names = links(self.root)
        report = self.run_compact(0, True)
        self.assertEqual([m['name'] for m in report['moved']], ['old.md'])
        self.assertIn(odd, self.memory_text())
        self.assertEqual(links(self.root), names)

    def test_sizes_are_utf8_bytes(self):
        # Added: the em dash and accents are several bytes; character count must not be used.
        self.seed(entry('uni', '11-01: ' + 'é—ü' * 30))
        text = self.memory_text()
        self.assertGreater(nbytes(text), len(text))
        report = self.run_compact(len(text))
        self.assertEqual(report['bytes_before'], nbytes(text))
        self.assertEqual([m['name'] for m in report['moved']], ['uni.md'])
        self.assertTrue(report['fits'])

    def test_duplicate_entry_names(self):
        # Added: the same link on two lines is appended once; the second line is only removed.
        self.seed(entry('dup', OLD) + entry('dup', '11-01: older'))
        report = self.run_compact(0, True)
        self.assertEqual(report['already_linked'], ['dup.md'])
        self.assertEqual(len(report['moved']), 2)
        self.assertEqual(sum(p.read_text().count('(dup.md)') for p in self.root.glob('index-*.md')), 1)
        self.assertEqual(report['topic_counts'], {'index-infra.md': 2})

    def test_link_only_in_front_matter_is_not_already_linked(self):
        # owners come from the bodies of the topic files only.
        self.seed(entry('old', OLD))
        other = self.root / 'index-other.md'
        other.write_text(other.read_text().replace('Topic memories (999)', 'Topic memories (999) see [old](old.md)'))
        report = self.run_compact(0, True)
        self.assertEqual(report['already_linked'], [])
        self.assertIn('[old](old.md)', (self.root / 'index-infra.md').read_text().split('\n---\n', 1)[1])

    def test_crash_between_topic_and_memory_heals_both_counts(self):
        # the topic file already holds the link, MEMORY.md still has the entry and the old count.
        for description in (2, 999):
            with self.subTest(description=description):
                self.seed(entry('old', OLD))
                topic = self.root / 'index-infra.md'
                topic.write_text('---\ndescription: "Topic memories ({})"\nmodified: 2000-01-01\n---\n\n'
                                 '- Label: [seed](seed.md)\n- Label: [old](old.md)\n'.format(description))
                report = self.run_compact(0, True)
                text = self.memory_text()
                self.assertNotIn('(old.md)', text)
                self.assertIn('[2 memories](index-infra.md)', text)
                self.assertIn('memories (2)', topic.read_text())
                self.assertEqual((report['already_linked'], report['topic_counts']),
                                 (['old.md'], {'index-infra.md': 2}))
                self.assertEqual(topic.read_text().count('(old.md)'), 1)

    def test_topic_label_and_grouping(self):
        # Added: label = the most used label of the file (ties: first); empty body uses the default.
        text = ('---\ndescription: "x memories (3)"\n---\n'
                '- A: [1](1.md)\n- B: [2](2.md) · [3](3.md)\n- B: [4](4.md)\n')
        out = tool.append_links(text, [(str(n), '{}.md'.format(n)) for n in range(10, 17)], 'Default')
        self.assertTrue(out.startswith(text))
        self.assertEqual(out[len(text):],
                         '- B: [10](10.md) · [11](11.md) · [12](12.md)\n'
                         '- B: [13](13.md) · [14](14.md) · [15](15.md)\n- B: [16](16.md)\n')
        empty = tool.append_links('---\ndescription: "x memories (0)"\n---\n', [('t', 't.md')], 'Default')
        self.assertTrue(empty.endswith('- Default: [t](t.md)\n'))
        no_newline = tool.append_links('---\ndescription: "x"\n---\n- C: [1](1.md)', [('t', 't.md')], 'D')
        self.assertTrue(no_newline.endswith('[1](1.md)\n- C: [t](t.md)\n'))

    def test_default_locations(self):
        # Added: TASK.md names two defaults; they follow HOME, so no real home is touched.
        home = Path(self.temp.name) / 'home'
        with patch.dict(os.environ, {'HOME': str(home), 'USERPROFILE': str(home), 'CLAUDE_MEMORY_DIR': ''}):
            self.assertEqual(tool.default_memory_dir(), Path(tool.memdir.memory_dir()))
            self.assertEqual(tool.default_backup_dir(TODAY), home / '.claude/recovery/memory-index-2026-11-11')
            self.seed(entry('old', OLD))
            tool.compact(self.root, TODAY, 0, True)
        saved = sorted(p.name for p in (home / '.claude/recovery/memory-index-2026-11-11').iterdir())
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(re.fullmatch(r'(MEMORY|index-infra)\.md\.before-\d{6}', n) for n in saved), saved)

    def test_module_docstring_states_the_one_exception(self):
        # non-entry lines are kept byte for byte, except the numbers of the topic count links.
        doc = tool.__doc__
        self.assertIn('byte for byte', doc)
        self.assertIn('[N memories](index-x.md)', doc)
        self.assertIn('## By topic', doc)

    # ---- concurrent writers -----------------------------------------------------

    def test_retry_preserves_concurrent_memory_and_topic_edits(self):
        for target in ('MEMORY.md', 'index-infra.md'):
            self.seed(entry('old', OLD))
            changed = []
            def hook(path):
                if not changed and path.name == target:
                    changed.append(True)
                    path.write_bytes(path.read_bytes() + b'\nconcurrent annotation\n')
            self.run_compact(0, True, before_replace=hook)
            self.assertIn(b'concurrent annotation', (self.root / target).read_bytes())
            self.assertIn('old.md', links(self.root))

    def test_retry_after_partial_write_keeps_counts(self):
        # Added: the topic file was already replaced when MEMORY.md changed; the second attempt
        # must still fix the count in MEMORY.md for that topic.
        self.seed(entry('old', OLD))
        changed = []
        def hook(path):
            if not changed and path.name == 'MEMORY.md':
                changed.append(True)
                path.write_bytes(path.read_bytes() + b'concurrent annotation\n')
        report = self.run_compact(0, True, before_replace=hook)
        self.assertTrue(report['applied'])
        self.assertIn('[2 memories](index-infra.md)', self.memory_text())
        self.assertIn('memories (2)', (self.root / 'index-infra.md').read_text())
        self.assertIn('concurrent annotation', self.memory_text())

    def test_retry_when_a_read_only_topic_changes(self):
        # Added: a topic file that only decided "already linked" changed between read and write.
        self.seed(entry('old', OLD))
        calls = []
        def hook(path):
            calls.append(path.name)
            if len(calls) == 1:
                other = self.root / 'index-other.md'
                other.write_text(other.read_text() + '- Label: [late](late.md)\n')
        self.run_compact(0, True, before_replace=hook)
        self.assertEqual(calls, ['index-infra.md', 'index-infra.md', 'MEMORY.md'])
        self.assertIn('late.md', links(self.root))

    def test_the_target_is_read_last_before_the_replace(self):
        # in the comparison before a replace the file about to be replaced is read last.
        self.seed(entry('old', OLD))
        reads, last_read = [], []
        real_read, real_replace = Path.read_bytes, os.replace

        def spy(path):
            reads.append(path.name)
            return real_read(path)

        def check_replace(src, dst):
            last_read.append((Path(dst).name, reads[-1]))
            real_replace(src, dst)
        with patch.object(Path, 'read_bytes', spy), patch.object(tool.os, 'replace', side_effect=check_replace):
            self.run_compact(0, True)
        self.assertEqual(last_read, [('index-infra.md', 'index-infra.md'), ('MEMORY.md', 'MEMORY.md')])

    def test_hook_runs_after_the_temp_file_exists_and_a_mismatch_removes_it(self):
        # temp file written first, then the hook, then the compare, then os.replace.
        self.seed(entry('old', OLD))
        seen = []
        def hook(path):
            seen.append(self.tmp_files())
            if len(seen) == 1:
                path.write_bytes(path.read_bytes() + b'\nconcurrent annotation\n')
        self.run_compact(0, True, before_replace=hook)
        self.assertEqual(len(seen[0]), 1)
        self.assertTrue(seen[0][0].startswith('.index-infra.md.'), seen[0])
        self.assertEqual(len(seen[1]), 1)                 # the first temp file was deleted on the mismatch
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(self.tmp_files(), [])
        self.assertIn(b'concurrent annotation', (self.root / 'index-infra.md').read_bytes())
        self.assertIn('old.md', links(self.root))

    def test_retry_exhaustion(self):
        self.seed(entry('old', OLD))
        calls = []
        def hook(path):
            calls.append(path)
            path.write_bytes(path.read_bytes() + b'\nexternal\n')
        with self.assertRaisesRegex(RuntimeError, '3'):
            self.run_compact(0, True, before_replace=hook)
        self.assertEqual(len(calls), 3)
        self.assertIn('old.md', links(self.root))
        self.assertEqual(self.tmp_files(), [])

    # ---- quiet check ------------------------------------------------------------

    def test_quiet_check_skips_a_fresh_memory_file(self):
        # apply + quiet_seconds > 0 + MEMORY.md modified less than N seconds ago = nothing is written.
        self.seed(entry('old', OLD))
        before = snapshot(self.root)
        report = self.run_compact(0, True, quiet_seconds=90)
        self.assertFalse(report['applied'])
        self.assertRegex(report['skipped'], r'^MEMORY\.md changed \d+ s ago$')
        # nothing is planned, so the lists are empty and the sizes are the current size
        size = len((self.root / 'MEMORY.md').read_bytes())
        self.assertEqual(set(report), REPORT_KEYS | {'skipped'})
        self.assertEqual((report['moved'], report['cut'], report['already_linked'], report['topic_counts'],
                          report['exempt']), ([], [], [], {}, []))
        self.assertEqual((report['bytes_before'], report['bytes_after'], report['budget'], report['fits']),
                         (size, size, 0, False))
        self.assertTrue(self.run_compact(size, True, quiet_seconds=90)['fits'])
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.backup.exists())
        self.assertNotIn('skipped', self.run_compact(0, False, quiet_seconds=90))      # a dry run is never skipped
        self.assertNotIn('skipped', self.run_compact(0, True, quiet_seconds=0))        # 0 switches the check off
        self.assertIn('[2 memories](index-infra.md)', self.memory_text())

    def test_quiet_check_comes_before_any_parsing(self):
        # a malformed MEMORY.md is skipped (report, exit 3 on the command line), not an error.
        self.seed(entry('old', OLD))
        for content in (b'no heading\n', b'\xff\xfe not utf-8'):
            with self.subTest(content=content):
                (self.root / 'MEMORY.md').write_bytes(content)
                before = snapshot(self.root)
                report = self.run_compact(100, True, quiet_seconds=90)
                self.assertEqual(report['applied'], False)
                self.assertRegex(report['skipped'], r'^MEMORY\.md changed \d+ s ago$')
                self.assertEqual((report['bytes_before'], report['bytes_after'], report['fits']),
                                 (len(content), len(content), True))
                self.assertEqual(snapshot(self.root), before)
                result = self.cli('--apply', '--backup-dir', str(self.backup))
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertTrue(json.loads(result.stdout)['skipped'].startswith('MEMORY.md changed '))
        self.assertEqual(self.cli('--apply', '--quiet-seconds', '0').returncode, 1)     # off: the error shows
        self.assertFalse(self.backup.exists())

    def test_quiet_check_lets_an_old_memory_file_through(self):
        self.seed(entry('old', OLD))
        stamp = time.time() - 1000
        os.utime(self.root / 'MEMORY.md', (stamp, stamp))
        report = self.run_compact(0, True, quiet_seconds=90)
        self.assertTrue(report['applied'])
        self.assertNotIn('skipped', report)
        self.assertEqual(set(report), REPORT_KEYS)

    # ---- the real shapes --------------------------------------------------------


    # ---- command line -----------------------------------------------------------

    def cli(self, *args):
        script = Path(tool.__file__).resolve()
        return subprocess.run([sys.executable, '-B', str(script), '--dir', str(self.root),
                               '--today', TODAY.isoformat(), *args],
                              capture_output=True, text=True)

    def test_cli(self):
        self.seed(entry('recent', '11-11: protected'))
        script = Path(tool.__file__).resolve()
        before = snapshot(self.root)
        for budget, code in [(99999, 0), (0, 2)]:
            result = subprocess.run([sys.executable, str(script), '--dir', str(self.root),
                                     '--today', TODAY.isoformat(), '--budget', str(budget)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(json.loads(result.stdout)['fits'], code == 0)
        self.assertEqual(before, snapshot(self.root))

    def test_cli_apply_backup_dir_and_errors(self):
        # Added: --apply and --backup-dir reach the library; failures exit 1 with a message.
        self.seed(entry('old', OLD))
        fit = self.run_compact(0)['bytes_after']
        result = self.cli('--budget', str(fit), '--apply', '--quiet-seconds', '0', '--backup-dir', str(self.backup))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['applied'] and report['fits'])
        self.assertEqual(len(list(self.backup.iterdir())), 2)
        self.assertNotIn('old.md', self.memory_text())
        again = self.cli('--budget', str(fit), '--apply', '--quiet-seconds', '0', '--backup-dir', str(self.backup))
        self.assertEqual((again.returncode, json.loads(again.stdout)['applied']), (0, False))
        (self.root / 'MEMORY.md').write_text('no heading\n')
        broken = self.cli('--apply', '--quiet-seconds', '0', '--backup-dir', str(self.backup))
        # An index with no '## By topic' section (how Claude Code writes it) is left alone: exit 0.
        self.assertEqual(broken.returncode, 0, broken.stderr)
        self.assertEqual(json.loads(broken.stdout), {'skipped': "no '## By topic' section", 'applied': False})
        self.assertEqual((self.root / 'MEMORY.md').read_text(), 'no heading\n')
        for bad in (('--budget', 'lots'), ('--today', '11/11/2026'), ('--nope',), ('--quiet-seconds', 'soon')):
            self.assertEqual(self.cli(*bad).returncode, 1, bad)   # 2 is reserved for "does not fit"

    def test_cli_quiet_check_exit_3_and_override(self):
        # command-line default 90 seconds; exit 3 when skipped; --quiet-seconds 0 runs.
        self.seed(entry('old', OLD))
        fit = self.run_compact(0)['bytes_after']
        before = snapshot(self.root)
        skipped = self.cli('--budget', str(fit), '--apply', '--backup-dir', str(self.backup))
        self.assertEqual(skipped.returncode, 3, skipped.stderr)
        report = json.loads(skipped.stdout)
        self.assertFalse(report['applied'])
        self.assertTrue(report['skipped'].startswith('MEMORY.md changed '))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.backup.exists())
        ran = self.cli('--budget', str(fit), '--apply', '--quiet-seconds', '0', '--backup-dir', str(self.backup))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(json.loads(ran.stdout)['applied'])


if __name__ == '__main__':
    unittest.main()
