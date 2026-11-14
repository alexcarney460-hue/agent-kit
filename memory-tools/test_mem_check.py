"""Checker behavior on synthetic stores, with no network or real home."""
from contextlib import closing
import datetime as dt
import sqlite3
from unittest import mock

import consolidate
import mem_check


def stores():
    hm, sem = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    hm.execute('CREATE TABLE learnings (id INTEGER PRIMARY KEY, category TEXT, rule TEXT, context TEXT, created_at TEXT, active INTEGER, protected INTEGER)')
    consolidate.ensure_schema(hm, sem)
    return hm, sem


def test_stale_and_confirm():
    hm, sem = stores()
    with closing(hm), closing(sem):
        hm.execute("INSERT INTO learnings VALUES (1,'context','old','', '2020-01-01',1,0,NULL,NULL,NULL,NULL,NULL,NULL)")
        hm.commit()
        assert mem_check.stale_map(hm, [1]) == {1: '2020-01-01'}
        assert mem_check.confirm(hm, [1]) == 1
        assert mem_check.stale_map(hm, [1]) == {}
        assert not mem_check.is_stale('correction', '2020-01-01', dt.datetime(2026, 1, 1))


def test_flags_resolve_and_dismiss():
    hm, sem = stores()
    with closing(hm), closing(sem):
        hm.executemany('INSERT INTO learnings (id,category,rule,created_at,active,protected) VALUES (?, ?, ?, ?, 1, 0)',
                       [(1,'context','a','2020-01-01'), (2,'context','b','2020-01-01'), (3,'context','c','2020-01-01')])
        hm.commit()
        flag = mem_check.record_flag(sem, 1, 2, 'conflict', 1, 'fixture')
        mem_check.resolve(hm, sem, 1, 2)
        assert hm.execute('SELECT active,superseded_by FROM learnings WHERE id=2').fetchone() == (0, 1)
        assert sem.execute('SELECT status FROM memory_flag WHERE id=?', (flag,)).fetchone() == ('resolved',)
        flag2 = mem_check.record_flag(sem, 1, 3, 'conflict', 1, 'fixture')
        assert mem_check.dismiss(sem, flag2) == 1
        assert sem.execute('SELECT status FROM memory_flag WHERE id=?', (flag2,)).fetchone() == ('dismissed',)


def test_check_reports_open_flags_and_never_sends_when_cloud_off(tmp_path):
    hm, sem = stores()
    with closing(hm), closing(sem):
        mem_check.record_flag(sem, 1, 2, 'review', 1, 'fixture')
        with mock.patch.object(mem_check, '_open_embeddings', return_value=None), mock.patch.object(mem_check.memdir, 'cloud_ok', return_value=False), mock.patch.object(mem_check, 'adjudicate_pairs', side_effect=AssertionError('sender reached')):
            result = mem_check.phase_check(hm, sem, True, str(tmp_path))
        assert result['check_flags_open'] == 1
        assert result['check_flags'][0]['reason'] == 'review'
