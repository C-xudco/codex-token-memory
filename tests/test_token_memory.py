import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('token_memory', ROOT / 'scripts/token_memory.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def event(total, last=None):
    return {'type': 'event_msg', 'timestamp': '2026-10-06T00:00:00Z',
            'payload': {'type': 'token_count', 'info': {'total_token_usage': total,
            'last_token_usage': last or total, 'model_context_window': 258400}}}


def line(value):
    return (json.dumps(value) + '\n').encode()


class UsageTests(unittest.TestCase):
    def test_derived_counts_and_unknown(self):
        result = m.metrics({'input_tokens': 1000, 'cached_input_tokens': 400, 'cache_write_input_tokens': 200,
                            'output_tokens': 100, 'reasoning_output_tokens': 30, 'total_tokens': 1100})
        self.assertEqual(result['ordinary_input_tokens'], 400)
        self.assertEqual(result['uncached_input_tokens'], 600)
        self.assertEqual(result['cache_hit_rate'], .4)
        self.assertEqual(result['total_tokens'], 1100)
        unknown = m.metrics({'input_tokens': 100})
        self.assertIsNone(unknown['cached_input_tokens'])
        self.assertIsNone(unknown['cache_write_input_tokens'])
        self.assertIsNone(unknown['cache_hit_rate'])
        self.assertIsNone(m.metrics({'input_tokens': 0, 'cached_input_tokens': 0})['cache_hit_rate'])
        self.assertIsNone(m.metrics({'input_tokens': True})['input_tokens'])

    def test_invalid_counts(self):
        result = m.metrics({'input_tokens': 100, 'cached_input_tokens': 200, 'output_tokens': 5,
                            'reasoning_output_tokens': 6, 'total_tokens': 9})
        self.assertIsNone(result['cache_hit_rate'])
        self.assertIn('缓存读取超过输入，未计算命中率。', result['warnings'])
        self.assertIn('推理输出超过输出，保留原值。', result['warnings'])
        self.assertIn('总量与输入加输出不一致，保留原值。', result['warnings'])

    def test_incremental_duplicate_partial_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout.jsonl'
            first = event({'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 10, 'total_tokens': 110})
            second = event({'input_tokens': 220, 'cached_input_tokens': 180, 'output_tokens': 30, 'total_tokens': 250})
            path.write_bytes(line(first) + line(first))
            reader = m.UsageReader(path)
            self.assertEqual(reader.read()['total']['input_tokens'], 100)
            payload = line(second)
            with path.open('ab') as f:
                f.write(payload[:30])
            self.assertEqual(reader.read()['total']['input_tokens'], 100)
            with path.open('ab') as f:
                f.write(payload[30:])
                f.write(line({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None}}))
                f.write(b'broken json\n')
            result = reader.read()
            self.assertEqual(result['total']['input_tokens'], 220)
            self.assertEqual(result['malformed_lines'], 1)
            self.assertEqual(reader.read()['total']['input_tokens'], 220)
            path.write_bytes(line(first))
            result = reader.read()
            self.assertEqual(result['total']['input_tokens'], 100)
            self.assertEqual(result['malformed_lines'], 0)

    def test_thread_selection_verifies_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / 'sessions'
            root.mkdir()
            a = root / 'rollout-2026-thread-a.jsonl'
            a.write_bytes(line({'type': 'session_meta', 'payload': {'id': 'thread-a', 'cwd': str(home)}}))
            fake = root / 'rollout-2026-thread-a-fake.jsonl'
            fake.write_bytes(line({'type': 'session_meta', 'payload': {'id': 'other'}}))
            self.assertEqual(m.find_session(home, 'thread-a'), a)
            with self.assertRaises(ValueError):
                m.find_session(home, '')
            with self.assertRaises(ValueError):
                m.find_session(home, 'not-found')


class MemoryTests(unittest.TestCase):
    def test_selection_edit_disable_delete_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            base = {'title': 'Preference', 'text': 'Use concise Chinese', 'scope': 'global', 'target': ''}
            g = m.mutate_memory(state, 'add', base)
            t = m.mutate_memory(state, 'add', dict(base, scope='thread', target='thread-a'))
            m.mutate_memory(state, 'add', dict(base, scope='thread', target='other'))
            p = m.mutate_memory(state, 'add', dict(base, scope='project', target=str(ROOT)))
            ids = lambda: {e['id'] for e in m.export_memory(state, 'thread-a', str(ROOT))['entries']}
            self.assertEqual(ids(), {g['id'], t['id'], p['id']})
            m.mutate_memory(state, 'disable', {'id': t['id']})
            self.assertEqual(ids(), {g['id'], p['id']})
            m.mutate_memory(state, 'edit', {'id': g['id'], 'text': 'New text'})
            self.assertIn('New text', m.export_memory(state, 'thread-a', str(ROOT))['text'])
            m.mutate_memory(state, 'enable', {'id': t['id']})
            m.mutate_memory(state, 'delete', {'id': g['id']})
            self.assertEqual(ids(), {t['id'], p['id']})
            self.assertEqual(len(m.load_memory(state)['entries']), 3)
            before = (state / 'memory.json').read_bytes()
            with self.assertRaises(ValueError):
                m.mutate_memory(state, 'edit', {'id': p['id'], 'title': ''})
            self.assertEqual((state / 'memory.json').read_bytes(), before)

    def test_corrupt_store_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            path = state / 'memory.json'
            path.write_text('{bad', encoding='utf-8')
            with self.assertRaises(ValueError):
                m.mutate_memory(state, 'add', {'title': 'a', 'text': 'b', 'scope': 'global'})
            self.assertEqual(path.read_text(), '{bad')


if __name__ == '__main__':
    unittest.main(verbosity=2)
