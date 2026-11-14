"""The portable embedding scan remains correct without acceleration."""
from contextlib import closing
import sqlite3
import struct
from types import SimpleNamespace
from unittest import mock

import embed_index as e


def test_scan_without_numpy_and_per_store_quotas():
    with closing(e.connect(':memory:')) as conn:
        with mock.patch.object(e, 'DIM', 2), mock.patch.object(e, 'np', None), mock.patch.object(e, 'embed_batch', return_value=[[1.0, 0.0]]):
            conn.executemany('INSERT INTO embedding VALUES (?,?,?,?,?,?)', [
                ('file', 'best', e.MODEL, 'x', struct.pack('2f', 1, 0), 'now'),
                ('file', 'other', e.MODEL, 'x', struct.pack('2f', 0, 1), 'now'),
                ('hivemind', 'near', e.MODEL, 'x', struct.pack('2f', 1, 1), 'now'),
                ('file', 'bad', e.MODEL, 'x', b'x', 'now')])
            conn.commit()
            result = e.search(conn, 'query', 3)
            assert [r[:2] for r in result] == [('file', 'best'), ('hivemind', 'near'), ('file', 'other')]
            assert abs(result[1][2] - 2 ** -0.5) < 1e-10
            ranked = e.ranked_by_store(conn, 'query', ['file', 'hivemind'])
            assert [ref for ref, _ in ranked['file']] == ['best', 'other']
            assert [ref for ref, _ in ranked['hivemind']] == ['near']


def test_windows_uses_scan_instead_of_mapping():
    with mock.patch.object(e, 'os', SimpleNamespace(name='nt')), mock.patch.object(e, '_matrix', side_effect=AssertionError('mapped')):
        assert e._matrix_for(None) is None
