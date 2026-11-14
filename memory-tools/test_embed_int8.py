"""Int8 exactness tests using synthetic fixtures."""
import itertools
import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest
if os.name == "nt":
    pytest.skip("int8 accelerator is unavailable on Windows; ordinary search uses float32", allow_module_level=True)
np = pytest.importorskip("numpy")
import embed_index as e
from test_embed_matrix import Index, unit

class BuildInt8(Index):
    def test_build_error_and_status(self):
        info = self.build()
        self.assertTrue(Path(info['path'], 'matrix.i8').is_file())
        m = e._matrix()
        small = np.memmap(Path(info['path'], 'matrix.i8'), dtype='i1', mode='r', shape=m[0].shape)
        errors = np.linalg.norm(m[0].astype('f8') - small.astype('f4').astype('f8') * np.float32(info['i8_scale']), axis=1)
        self.assertGreaterEqual(info['i8_error'], max(errors))
        self.assertTrue(e.matrix_status(self.conn)['int8'])

    def test_missing_truncated_and_unwritable(self):
        info = self.build()
        path = Path(info['path'], 'matrix.i8')
        expected = e.search(self.conn, 'q', 50)
        path.write_bytes(b'short')
        self.assertEqual(e.search(self.conn, 'q', 50), expected)
        self.assertFalse(e.matrix_status(self.conn)['int8'])
        path.unlink()
        self.assertEqual(e.search(self.conn, 'q', 50), expected)
        def partial_write(folder, count):
            Path(folder, 'matrix.i8').write_bytes(b'partial')
            raise OSError('disk full')
        with mock.patch.object(e, '_write_int8', side_effect=partial_write):
            info = self.build()
        self.assertFalse(Path(info['path'], 'matrix.i8').exists())
        self.assertNotIn('i8_error', info)
        self.assertEqual(e.search(self.conn, 'q', 50), expected)

    def test_disk_guard_includes_optional_copy(self):
        with mock.patch.object(e, '_require_space', wraps=e._require_space) as guard:
            info = self.build()
        base = {key: info[key] for key in ('model', 'dim', 'count', 'built_at', 'seq', 'epoch', 'store_names')}
        base['count'] = 0
        keys_bytes = info['count'] * (1 + len('rates.md')) + 8192
        self.assertEqual(guard.call_args_list[0].args[0],
                         info['count'] * 4 * e.DIM + keys_bytes + len(json.dumps(base)) + 4096)
        self.assertTrue(any(call.args[0] == info['count'] * e.DIM + 8192
                            for call in guard.call_args_list))

    def test_changes_and_public_iterators(self):
        self.build()
        self.put('file', 'rates.md', unit(0, 1))
        self.put('file', 'new', unit(1, .02))
        self.conn.execute("DELETE FROM embedding WHERE ref='tmux.md'")
        self.conn.commit()
        scored = e._scores(self.conn, 'q')
        with mock.patch.object(e, '_open_int8', return_value=None):
            exact = e._scores(self.conn, 'q')
        for store in (None, 'file', 'hivemind', 'claudemem', 'absent'):
            self.assertEqual(list(e._ranked(scored, store)), list(e._ranked(exact, store)))
        self.assertEqual(e.search(self.conn, 'q', 50), e._select(exact, 50))
        self.assertEqual(e.search_by_store(self.conn, 'q', {'file': 20})['file'],
                         [(r, s) for _, r, s in e._select(exact, 20, 'file')])


class SyntheticInt8(unittest.TestCase):
    def fixture(self, folder, count, seed, clustered=False):
        rng = np.random.default_rng(seed)
        matrix = np.memmap(Path(folder, 'matrix.f32'), mode='w+', dtype='f4', shape=(count, 1024))
        for start in range(0, count, 1024):
            part = rng.normal(size=(min(1024, count-start), 1024)).astype('f4')
            part /= np.linalg.norm(part, axis=1, keepdims=True)
            matrix[start:start+len(part)] = part
        if clustered:
            matrix[:300] = matrix[0] + rng.normal(0, 1e-5, (300, 1024)).astype('f4')
            matrix[:300] /= np.linalg.norm(matrix[:300], axis=1, keepdims=True)
        matrix[301:304] = matrix[300]  # identical rows, stable ties
        matrix[304] = np.nan
        matrix[305] = 0
        matrix[306] = 0
        matrix[306, 0] = 1  # component at the data-derived clipping value
        matrix.flush()
        with mock.patch.object(e, 'DIM', 1024), mock.patch.object(e, 'MATRIX_MIN_FREE', 0), mock.patch.object(e, 'MATRIX_DIR', folder):
            meta = e._write_int8(folder, count)
        return matrix, meta, rng

    def compare(self, folder, matrix, meta, q):
        n = len(matrix)
        exact = np.nan_to_num(np.asarray(matrix @ q), nan=-1.)
        small = e._open_int8(folder, meta, n)
        scores = e._Int8Scores(matrix, small, q, meta)
        codes = (np.arange(n) % 3).astype('u1')
        refs = np.asarray([str(i).encode() for i in range(n)])
        def scored(s):
            return ((codes, ['a', 'b', 'c'], refs, s), {('a', '0')}, (['a'], ['new'], np.asarray([.5], 'f4')))
        for store in (None, 'a', 'b', 'c', 'absent'):
            for k in (1, 20, 30, 64, 500):
                got, truth = e._select(scored(scores), k, store), e._select(scored(exact), k, store)
                self.assertEqual(got, truth)
                self.assertEqual(np.asarray([v for _, _, v in got], 'f4').tobytes(),
                                 np.asarray([v for _, _, v in truth], 'f4').tobytes())
        self.assertEqual(list(itertools.islice(e._ranked(scored(scores)), 3500)),
                         list(itertools.islice(e._ranked(scored(exact)), 3500)))
        return scores

    def test_seeds_clusters_nan_ties_and_random_queries(self):
        for seed, n in ((1, 1600), (41, 5000), (95, 2100)):
            with tempfile.TemporaryDirectory() as folder:
                matrix, meta, rng = self.fixture(folder, n, seed, clustered=True)
                for q in (matrix[0].copy(), rng.normal(size=1024).astype('f4')):
                    q /= np.linalg.norm(q)
                    with mock.patch.object(e, 'DIM', 1024):
                        self.compare(folder, matrix, meta, q)

    def test_proof_widens_and_wrong_error_can_change_answer(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(e, 'DIM', 1024), \
                mock.patch.object(e, 'MATRIX_DIR', folder), mock.patch.object(e, 'MATRIX_MIN_FREE', 0):
            matrix = np.memmap(Path(folder, 'matrix.f32'), mode='w+', dtype='f4', shape=(2048, 1024))
            matrix[:] = 0
            matrix[:, 0] = -.5
            matrix[:512, 0] = np.linspace(.05, .0505, 512)
            matrix[:, 1] = np.sqrt(1 - matrix[:, 0]**2)
            meta = e._write_int8(folder, 2048)
            q = np.zeros(1024, 'f4'); q[0] = 1
            small = e._open_int8(folder, meta, 2048)
            good = e._Int8Scores(matrix, small, q, meta)
            rows = good.select(np.arange(2048), 1)
            self.assertEqual(rows.tolist(), [511])
            self.assertGreater(len(good.rounds), 1)
            self.assertEqual(good.rounds[0], 64)
            self.assertEqual(good.rounds, [64, 256, 1024])
            self.assertLess(good.rounds[-1], len(matrix))
            cutoff = e._top(good.approx, good.rounds[-1] + 1)[-1]
            self.assertGreater(float(good[rows[0]]), float(good.approx[cutoff]) + good.bound)
            bad = e._Int8Scores(matrix, small, q, {**meta, 'i8_error': 0.})
            self.assertNotEqual(bad.select(np.arange(2048), 1).tolist(), [511])

    def test_real_int8_public_changes_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(e, 'DIM', 1024), \
                mock.patch.object(e, 'MATRIX_DIR', folder), mock.patch.object(e, 'MATRIX_MIN_FREE', 0):
            matrix, _, rng = self.fixture(folder, 1800, 17)
            matrix[305] = matrix[306]
            conn = e.connect(str(Path(folder, 'fixture.db')))
            self.addCleanup(conn.close)
            conn.executemany("INSERT INTO embedding VALUES (?,?,?,'h',?,'x')",
                             [('abc'[i % 3], str(i), e.MODEL, row.tobytes()) for i, row in enumerate(matrix)])
            conn.commit()
            info = e.build_matrix(conn)
            q = rng.normal(size=1024).astype('f4'); q /= np.linalg.norm(q)
            conn.execute("DELETE FROM embedding WHERE ref='1'")
            conn.execute("UPDATE embedding SET vec=? WHERE ref='2'", (q.tobytes(),))
            conn.execute("INSERT INTO embedding VALUES ('a','new',?,'h',?,'x')", (e.MODEL, q.tobytes()))
            conn.commit()
            with mock.patch.object(e, 'embed_batch', return_value=[q.tolist()]), \
                    mock.patch.object(e, '_matrix_residency', return_value=None):
                scored = e._scores(conn, 'q')
                self.assertIsInstance(scored[0][3], e._Int8Scores)
                with mock.patch.object(e, '_open_int8', return_value=None):
                    exact = e._scores(conn, 'q')
                self.assertEqual(e.search(conn, 'q', 500), e._select(exact, 500))
                for store in ('a', 'b', 'c'):
                    self.assertEqual(list(e.ranked_by_store(conn, 'q', [store])[store]),
                                     [(r, v) for _, r, v in e._ranked(exact, store)])
                self.assertEqual(e.search_by_store(conn, 'q', {'a': 20, 'b': 30}),
                                 {st: [(r, v) for _, r, v in e._select(exact, k, st)]
                                  for st, k in (('a', 20), ('b', 30))})
            for bad in ({'i8_scale': 0}, {'i8_error': -1}, {'i8_error': float('nan')}, {'i8_scale': 'bad'}):
                self.assertIsNone(e._open_int8(info['path'], {**info, **bad}, 1800))


class Round2Int8(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name
        for name, value in (('DIM', 1024), ('MATRIX_DIR', self.folder), ('MATRIX_MIN_FREE', 0)):
            patch = mock.patch.object(e, name, value)
            patch.start(); self.addCleanup(patch.stop)
        for name, value in (('_I8_RUNTIME_OK', set()), ('_I8_RUNTIME_FAILED', False)):
            patch = mock.patch.object(e, name, value)
            patch.start(); self.addCleanup(patch.stop)
        rng = np.random.default_rng(241)
        self.matrix = np.memmap(Path(self.folder, 'matrix.f32'), mode='w+', dtype='f4', shape=(600, 1024))
        self.matrix[:] = rng.normal(size=self.matrix.shape).astype('f4')
        self.matrix[:] /= np.linalg.norm(self.matrix, axis=1, keepdims=True)
        self.q = self.matrix[0].copy()

    def write(self):
        self.matrix.flush()
        meta = e._write_int8(self.folder, len(self.matrix))
        return meta, e._open_int8(self.folder, meta, len(self.matrix))

    def test_k1_invalid_rows_always_rescored_in_each_pool(self):
        self.matrix[-4:] = 0
        self.matrix[-4, 0] = np.inf
        self.matrix[-3, 0] = -np.inf
        self.matrix[-2, 0] = np.nan
        self.matrix[-1, :2] = [np.inf, -np.inf]
        self.q[:] = 1 / 32
        meta, small = self.write()
        with np.errstate(invalid='ignore'):
            truth = np.nan_to_num(self.matrix @ self.q, nan=-1.)
            for pool in (np.arange(600), np.arange(1, 600, 2)):
                for k in (1, 20, 300):
                    scores = e._Int8Scores(self.matrix, small, self.q, meta)
                    got = scores.select(pool, k)
                    self.assertEqual(got.tolist(), pool[e._top(truth[pool], k)].tolist())
                    self.assertEqual(scores[got].tobytes(), truth[got].tobytes())
                    self.assertTrue(scores.known[np.intersect1d(pool, np.arange(596, 600))].all())

    def test_k2_seeded_pair_checks_only_256_spread_rows(self):
        meta, small = self.write()
        self.assertIsNotNone(small)
        rows = e._int8_sample_rows(600, meta['i8_seed'])
        self.assertEqual(len(set(rows)), 256)
        self.assertLess(rows[0], 3); self.assertGreater(rows[-1], 596)
        reads = []
        class Tracked:
            def __init__(self, data): self.data = data
            def __getitem__(self, ids):
                reads.append(np.asarray(ids).copy())
                return self.data[ids]
        self.assertTrue(e._validate_int8_pair(Tracked(self.matrix), Tracked(small), meta, 600))
        self.assertEqual(len(reads), 2)
        for ids in reads: np.testing.assert_array_equal(ids, rows)
        replacement = np.memmap(Path(self.folder, 'matrix.i8'), mode='r+', dtype='i1', shape=small.shape)
        replacement[:] = small[::-1].copy(); replacement.flush()
        self.assertIsNone(e._open_int8(self.folder, meta, 600))
        meta, small = self.write()
        self.assertIsNone(e._open_int8(self.folder, {**meta, 'i8_error': 0.}, 600))
        with mock.patch.object(e, '_validate_int8_pair', side_effect=OSError('unreadable')):
            self.assertIsNone(e._open_int8(self.folder, meta, 600))

    def test_k3_build_bit_probe_and_signature_gate(self):
        meta, small = self.write()
        self.assertEqual(meta['i8_signature'], e._platform_signature())
        self.assertIsInstance(meta['i8_bits_identical'], bool)
        good = {**meta, '_i8': small, 'i8_bits_identical': True}
        self.assertTrue(e._int8_usable(good, self.q))
        self.assertFalse(e._int8_usable({**good, 'i8_bits_identical': False}, self.q))
        self.assertFalse(e._int8_usable({**good, 'i8_signature': {}}, self.q))
        with mock.patch.object(e, '_gathered_product', return_value=np.zeros(600, 'f4')):
            self.assertFalse(e._check_int8_bits(self.matrix, 9))
        with mock.patch.object(e, '_gathered_product', wraps=e._gathered_product) as probe:
            e._check_int8_bits(self.matrix, 9)
            self.assertEqual(probe.call_count, 36)

    def test_k4_nonunit_rows_and_queries_fall_back(self):
        meta, small = self.write()
        good = {**meta, '_i8': small, 'i8_bits_identical': True}
        self.assertLess(meta['i8_norm_error'], 1e-3)
        for q in (self.q * 2, self.q * 0, self.q * np.nan, self.q * np.inf):
            self.assertFalse(e._int8_usable(good, q))
        self.matrix[4] *= 2
        meta, small = self.write()
        self.assertGreater(meta['i8_norm_error'], .9)
        self.assertFalse(e._int8_usable({**meta, '_i8': small}, self.q))

    def test_k6_getitem_rescores_unknown_scalar_slice_and_array(self):
        meta, small = self.write()
        scores = e._Int8Scores(self.matrix, small, self.q, meta)
        truth = np.nan_to_num(self.matrix @ self.q, nan=-1.)
        for ids in (12, slice(10, 30), np.array([2, 35]), np.arange(600) % 2 == 0):
            self.assertEqual(np.asarray(scores[ids]).tobytes(), np.asarray(truth[ids]).tobytes())

    def test_k8_residency_threshold_errors_and_status(self):
        meta, small = self.write()
        good = {**meta, '_i8': small, 'i8_bits_identical': True}
        for share, path in ((None, 'int8'), (64*1024**2+1, 'int8'), (64*1024**2, 'float32'), (0, 'float32')):
            self.assertEqual(e._score_path(good, share, self.q), path)
        with mock.patch.object(e, '_resident_share', side_effect=RuntimeError('mincore failed')):
            self.assertIsNone(e._matrix_residency(self.matrix))
        share = e._resident_share(self.matrix)
        self.assertGreaterEqual(share, 0.); self.assertLessEqual(share, 1.)


class Round2Build(Index):
    def test_k5_float32_fits_without_optional_copy_and_memoryerror(self):
        original = e._require_space
        def space(needed):
            if needed == 44 * e.DIM + 8192:
                raise e.MatrixError('int8 does not fit')
            original(needed)
        for failure in (None, MemoryError('int8 allocation')):
            with mock.patch.object(e, '_require_space', side_effect=space), \
                    mock.patch.object(e, '_write_int8', side_effect=failure, wraps=e._write_int8):
                info = self.build()
            self.assertTrue(Path(info['path'], 'matrix.f32').exists())
            self.assertFalse(Path(info['path'], 'matrix.i8').exists())
            self.assertIsNotNone(e.search(self.conn, 'q', 20))

    def test_k8_scores_dispatch_and_status_readback(self):
        self.build()
        expected = self.scan(20)
        mapped = e._matrix_for(self.conn)
        meta = {**mapped[3], 'i8_bits_identical': True, 'i8_signature': e._platform_signature()}
        mapped = (*mapped[:3], meta)
        for share, path in ((None, 'int8'), (0., 'float32'), (.95, 'float32'), (1., 'float32')):
            with mock.patch.object(e, '_matrix_for', return_value=mapped), \
                    mock.patch.object(e, '_matrix_residency', return_value=share):
                scored = e._scores(self.conn, 'q')
                self.assertEqual(isinstance(scored[0][3], e._Int8Scores), path == 'int8')
                status = e.matrix_status(self.conn)
                self.assertEqual(status['residency'], share)
                self.assertEqual(status['search_path'], path)
                self.assertEqual(e._select(scored, 20), expected)
        for override in ({'i8_bits_identical': False}, {'i8_signature': {}}, {'i8_norm_error': 1.}):
            bad = (*mapped[:3], {**meta, **override})
            with mock.patch.object(e, '_matrix_for', return_value=bad), \
                    mock.patch.object(e, '_matrix_residency', return_value=0.):
                self.assertNotIsInstance(e._scores(self.conn, 'q')[0][3], e._Int8Scores)



class Round3Int8(unittest.TestCase):
    setUp = Round2Int8.setUp
    write = Round2Int8.write

    def save_meta(self, meta):
        Path(self.folder, 'meta.json').write_text(json.dumps({**meta, 'count': 600}))

    def test_l1_full_pair_hash_and_markers(self):
        meta, small = self.write()
        self.save_meta(meta)
        with mock.patch.object(e, 'SEARCH_CHUNK', 127), \
                mock.patch.object(e, '_int8_rows_valid', wraps=e._int8_rows_valid) as check:
            self.assertTrue(e.verify_int8(self.folder))
            self.assertEqual([len(c.args[0]) for c in check.call_args_list], [127]*4 + [92])
        stamp = json.loads(Path(self.folder, 'i8-verified.json').read_text())
        self.assertEqual(stamp['rows_checked'], 600)
        self.assertIn('time', stamp)
        rows = e._int8_sample_rows(600, meta['i8_seed'])
        target = next(i for i in range(600) if i not in rows)
        changed = np.memmap(Path(self.folder, 'matrix.i8'), mode='r+', dtype='i1', shape=(600, 1024))
        changed[target] = -127; changed.flush()
        self.assertTrue(e._validate_int8_pair(self.matrix, small, meta, 600))
        self.assertFalse(e.verify_int8(self.folder))
        self.assertFalse(Path(self.folder, 'matrix.i8').exists())
        self.assertFalse(Path(self.folder, 'i8-verified.json').exists())
        meta, small = self.write(); self.save_meta({**meta, 'i8_sha256': '0' * 64})
        self.assertFalse(e.verify_int8(self.folder))
        meta, small = self.write(); self.save_meta(meta)
        self.matrix[599] = np.nan; self.matrix.flush()
        self.assertFalse(e.verify_int8(self.folder))

    def test_l2_shapes_runtime_canary_and_sticky_failure(self):
        meta, small = self.write()
        with mock.patch.object(e, '_gathered_product', wraps=e._gathered_product) as product:
            e._check_int8_bits(self.matrix, 9)
        self.assertEqual([len(c.args[1]) for c in product.call_args_list],
                         list(e.I8_CANARY_SIZES) * 4)
        good = {**meta, '_i8': small}
        reads = []
        class Tracked:
            def __getitem__(inner, ids):
                reads.append(len(ids))
                return self.matrix[ids]
        rows, queries = e._canary_inputs(self.matrix, meta['i8_seed'])
        self.assertEqual(e._runtime_digest(Tracked(), rows, queries), meta['i8_canary']['sha256'])
        self.assertEqual(reads, [64, 264])
        with mock.patch.object(e, '_I8_RUNTIME_OK', set()), mock.patch.object(e, '_I8_RUNTIME_FAILED', False):
            with mock.patch.object(e, '_gathered_product', wraps=e._gathered_product) as product:
                self.assertTrue(e._int8_usable(good, self.q))
                self.assertTrue(e._int8_usable(good, self.q))
                self.assertEqual([len(c.args[1]) for c in product.call_args_list], [64, 264] * 2)
            bad = {**good, 'i8_canary': {**meta['i8_canary'], 'sha256': '0' * 64}}
            self.assertFalse(e._int8_usable(bad, self.q))
            self.assertFalse(e._int8_usable(good, self.q))

    def test_l3_missing_bytes_and_vector_sum(self):
        meta, small = self.write(); good = {**meta, '_i8': small}
        for missing, path in ((0, 'float32'), (64*1024**2, 'float32'),
                              (64*1024**2+1, 'int8'), (None, 'int8')):
            self.assertEqual(e._score_path(good, missing, self.q), path)
        with mock.patch.object(e, '_resident_share', side_effect=OSError('mincore')):
            self.assertIsNone(e._matrix_residency(self.matrix))
        self.assertGreaterEqual(e._missing_bytes(self.matrix, .5), self.matrix.nbytes // 2)

    def test_l4_malformed_metadata_and_config_are_total(self):
        meta, small = self.write(); good = {**meta, '_i8': small}
        fields = [key for key in meta if key.startswith('i8_')]
        for key in fields:
            for bad in (None, 'bad', False, float('nan'), -1):
                with self.subTest(key=key, bad=bad):
                    self.assertFalse(e._int8_usable({**good, key: bad}, self.q))
        with mock.patch.object(np.__config__, 'CONFIG', {'Build Dependencies': None}):
            self.assertFalse(e._int8_usable(good, self.q))
        with mock.patch.object(e, '_platform_signature', side_effect=RuntimeError('signature')):
            self.assertFalse(e._int8_usable(good, self.q))
            self.assertIsNone(e._open_int8(self.folder, meta, 600))
        for bad in (None, [], 'bad', False):
            self.assertFalse(e._int8_usable(bad, self.q))

    def test_l5_index_contract(self):
        meta, small = self.write()
        scores = e._Int8Scores(self.matrix, small, self.q, meta)
        truth = np.nan_to_num(self.matrix @ self.q, nan=-1.)
        for ids in (-1, np.int64(2), slice(None, None, -2), np.array([2, -1, 2]),
                    np.array([], dtype=int), np.arange(600) % 3 == 0):
            self.assertEqual(np.asarray(scores[ids]).tobytes(), np.asarray(truth[ids]).tobytes())
        for bad in (True, np.bool_(False), ..., None, 1.2, 'x', [1, 2],
                    (1,), np.array([1.2]), np.ones(2, bool), np.array([[1]])):
            with self.subTest(index=str(bad)), self.assertRaises(TypeError): scores[bad]

    def test_l6_zero_rows_are_always_candidates(self):
        self.matrix[599] = 0
        meta, small = self.write()
        self.assertLess(meta['i8_norm_error'], 1e-3)
        self.assertTrue(e._int8_usable({**meta, '_i8': small}, self.q))
        scores = e._Int8Scores(self.matrix, small, self.q, meta)
        scores.select(np.arange(600), 1)
        self.assertTrue(scores.known[599])
        self.assertEqual(scores[599], 0.)


class Round3Build(Index):
    def test_l1_refresh_daily_and_cli(self):
        info = self.build()
        stamp = Path(info['path'], 'i8-verified.json')
        with mock.patch.object(e, 'verify_int8', wraps=e.verify_int8) as verify:
            e.refresh_matrix(self.conn)
            self.assertEqual(verify.call_count, 1)
            e.refresh_matrix(self.conn)
            self.assertEqual(verify.call_count, 1)
            stamp.write_text(json.dumps({'time': '2000-01-01T00:00:00+00:00', 'rows_checked': info['count']}))
            e.refresh_matrix(self.conn)
            self.assertEqual(verify.call_count, 2)
        with mock.patch('sys.argv', ['embed_index.py', '--verify-int8']), \
                mock.patch.object(e, 'connect', side_effect=AssertionError('CLI opened DB')), \
                mock.patch.object(e, 'verify_int8', return_value=True) as verify:
            e.main()
            verify.assert_called_once()

    def test_l3_l6_status_reports_missing_and_current_path(self):
        self.build()
        for share, missing in ((1., 0), (None, 'unknown')):
            with mock.patch.object(e, '_matrix_residency', return_value=share):
                status = e.matrix_status(self.conn)
                self.assertEqual(status['missing_bytes'], missing)
                self.assertEqual(status['search_path'], 'float32' if share == 1. else 'int8')

    def test_l4_search_falls_back_on_accelerator_failure(self):
        self.build()
        expected = self.scan(20)
        with mock.patch.object(e, '_matrix_residency', return_value=None), \
                mock.patch.object(e, '_Int8Scores', side_effect=ValueError('int8 unreadable')):
            scored = e._scores(self.conn, 'q')
        self.assertIsInstance(scored[0][3], np.ndarray)
        self.assertEqual(e._select(scored, 20), expected)

    def test_l1_failed_daily_check_preserves_float32_search(self):
        info = self.build()
        expected = self.scan(20)
        Path(info['path'], 'matrix.i8').write_bytes(b'broken')
        e.refresh_matrix(self.conn)
        self.assertFalse(Path(info['path'], 'matrix.i8').exists())
        self.assertFalse(e.matrix_status(self.conn)['int8'])
        self.assertEqual(e.search(self.conn, 'q', 20), expected)
