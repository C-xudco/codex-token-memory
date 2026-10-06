import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    sessions = tmp / 'home/sessions'
    sessions.mkdir(parents=True)
    log = sessions / 'rollout-2026-test-thread.jsonl'
    log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'test-thread', 'cwd': str(root)}}) + '\n' +
                   json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                   'total_token_usage': {'input_tokens': 100, 'cached_input_tokens': 30, 'output_tokens': 5, 'total_tokens': 105}}}}) + '\n')
    state = tmp / 'state'
    process = subprocess.Popen([sys.executable, str(root / 'scripts/token_memory.py'),
              '--codex-home', str(tmp / 'home'), 'serve', '--thread-id', 'test-thread', '--state-dir', str(state)],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (state / 'runtime.json').exists():
            if process.poll() is not None:
                raise RuntimeError(process.communicate()[1].decode())
            if time.monotonic() > deadline:
                raise TimeoutError('Panel startup timeout')
            time.sleep(.1)
        runtime = json.loads((state / 'runtime.json').read_text())
        url, token = runtime['url'], runtime['token']
        def api(path, data=None, auth=True, origin=None):
            headers = {'X-Token-Memory': token} if auth else {}
            if origin:
                headers['Origin'] = origin
            body = json.dumps(data).encode() if data is not None else None
            with urlopen(Request(url + path, data=body, headers=headers), timeout=4) as response:
                return json.load(response)
        for params in ({'auth': False}, {'origin': 'https://outside.invalid'}):
            try:
                api('/api/state', **params)
                raise AssertionError('Authentication bypass')
            except HTTPError as e:
                assert e.code == 403
        snapshot = api('/api/state')
        assert snapshot['usage']['total']['input_tokens'] == 100
        assert snapshot['usage']['total']['cache_write_input_tokens'] is None
        entry = api('/api/memory', {'action': 'add', 'title': '<script>test</script>',
                  'text': 'temporary test data', 'scope': 'thread', 'target': 'test-thread'})
        assert len(api('/api/export')['entries']) == 1
        api('/api/memory', {'action': 'disable', 'id': entry['id']})
        assert api('/api/export')['entries'] == []
        api('/api/memory', {'action': 'edit', 'id': entry['id'], 'text': 'edited test data'})
        api('/api/memory', {'action': 'enable', 'id': entry['id']})
        assert 'edited test data' in api('/api/export')['text']
        api('/api/memory', {'action': 'delete', 'id': entry['id']})
        assert api('/api/state')['memory']['entries'] == []
        with urlopen(url, timeout=4) as response:
            html = response.read().decode()
            assert '__AUTH_TOKEN__' not in html
            assert 'text.textContent=e.text' in html
        api('/api/stop', {})
        process.communicate(timeout=5)
        assert json.loads((state / 'runtime.json').read_text()).get('stopped_at')
        print('HTTP checks passed: authenticating requests, live usage, memory CRUD and selection, graceful stop.')
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
