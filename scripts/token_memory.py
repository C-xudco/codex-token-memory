#!/usr/bin/env python3
"""Read Codex token records and manage an independent opt-in memory list. Stdlib only."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.request import Request, urlopen
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def sessions(home, thread_id=None):
    rows = []
    for path in (home / 'sessions').rglob('rollout-*.jsonl'):
        if thread_id and thread_id not in path.name:
            continue
        try:
            with path.open('rb') as f:
                e = json.loads(f.readline())
            if e.get('type') != 'session_meta':
                continue
            p = e['payload']
            if thread_id and p.get('id') != thread_id:
                continue
            rows.append({'thread_id': p.get('id'), 'project': p.get('cwd'),
                         'path': str(path), 'modified': path.stat().st_mtime})
        except (OSError, ValueError, KeyError, AttributeError):
            continue
    return sorted(rows, key=lambda r: r['modified'], reverse=True)


def find_session(home, thread_id):
    if not thread_id:
        raise ValueError('缺少线程 ID；运行 sessions 后明确选择。')
    found = sessions(home, thread_id)
    if not found:
        raise ValueError('找不到该线程记录；检查 ID 和 --codex-home。')
    return Path(found[0]['path'])


def count(obj, name):
    v = obj.get(name)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def metrics(obj):
    if not isinstance(obj, dict):
        return None
    r = {k: count(obj, k) for k in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                                  'output_tokens', 'reasoning_output_tokens', 'total_tokens')}
    i, c, w, o, reasoning, total = r.values()
    valid = i is not None and c is not None and c <= i
    r['uncached_input_tokens'] = i - c if valid else None
    r['ordinary_input_tokens'] = i - c - w if valid and w is not None and c + w <= i else None
    r['cache_hit_rate'] = c / i if valid and i else None
    warnings = [k + ' 不可得。' for k in ('input_tokens', 'cached_input_tokens',
                'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens') if r[k] is None]
    if i is not None and c is not None and c > i:
        warnings.append('缓存读取超过输入，未计算命中率。')
    if valid and w is not None and c + w > i:
        warnings.append('缓存读写合计超过输入，未计算普通输入。')
    if o is not None and reasoning is not None and reasoning > o:
        warnings.append('推理输出超过输出，保留原值。')
    if total is not None and i is not None and o is not None and total != i + o:
        warnings.append('总量与输入加输出不一致，保留原值。')
    r['warnings'] = warnings
    return r


class UsageReader:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.identity = None
        self.total = self.last = self.timestamp = self.window = None
        self.bad_lines = 0
        self.thread_id = self.project = None

    def read(self):
        st = self.path.stat()
        identity = (st.st_dev, st.st_ino)
        if self.identity != identity or st.st_size < self.offset:
            self.offset = 0
            self.total = self.last = self.timestamp = self.window = None
            self.thread_id = self.project = None
            self.bad_lines = 0
        self.identity = identity
        with self.path.open('rb') as f:
            f.seek(self.offset)
            while True:
                pos = f.tell()
                line = f.readline()
                if not line or not line.endswith(b'\n'):
                    self.offset = pos
                    break
                try:
                    e = json.loads(line)
                except (ValueError, UnicodeError):
                    self.bad_lines += 1
                    continue
                if not isinstance(e, dict) or not isinstance(e.get('payload'), dict):
                    continue
                p = e['payload']
                if e.get('type') == 'session_meta':
                    self.thread_id, self.project = p.get('id'), p.get('cwd')
                if e.get('type') != 'event_msg' or p.get('type') != 'token_count':
                    continue
                info = p.get('info')
                if not isinstance(info, dict):
                    continue  # Rate-limit-only events do not erase the known usage.
                self.total = metrics(info.get('total_token_usage'))
                self.last = metrics(info.get('last_token_usage'))
                self.window = count(info, 'model_context_window')
                self.timestamp = e.get('timestamp')
        return {'thread_id': self.thread_id, 'project': self.project, 'source': str(self.path),
                'updated_at': self.timestamp, 'read_at': now(), 'total': self.total, 'last': self.last,
                'context_window_capacity': self.window, 'malformed_lines': self.bad_lines}


@contextmanager
def store_lock(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'memory.lock').open('a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'0')
            f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_memory(state):
    path = state / 'memory.json'
    if not path.exists():
        return {'version': 1, 'entries': []}
    d = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(d, dict) or d.get('version') != 1 or not isinstance(d.get('entries'), list):
        raise ValueError('记忆格式不受支持；未覆盖原文件。')
    return d


def validate_entry(e):
    for field, limit in [('title', 200), ('text', 20000)]:
        if not isinstance(e.get(field), str) or not e[field].strip() or len(e[field]) > limit:
            raise ValueError(field + ' 不能为空，长度上限为 ' + str(limit) + ' 字符。')
    if e.get('scope') not in ('global', 'project', 'thread'):
        raise ValueError('范围应为 global、project 或 thread。')
    if not isinstance(e.get('target'), str) or (e['scope'] != 'global' and not e['target'].strip()):
        raise ValueError('项目或对话范围需要目标。')
    if not isinstance(e.get('enabled'), bool):
        raise ValueError('enabled 应为布尔值。')


def mutate_memory(state, action, values):
    with store_lock(state):
        data = load_memory(state)
        entries = data['entries']
        if action == 'add':
            entry = {'id': uuid.uuid4().hex[:12], 'title': values.get('title'), 'text': values.get('text'),
                     'scope': values.get('scope', 'thread'), 'target': values.get('target') or '',
                     'enabled': values.get('enabled', True), 'created_at': now(), 'updated_at': now()}
            validate_entry(entry)
            entries.append(entry)
        else:
            entry = next((e for e in entries if e['id'] == values.get('id')), None)
            if entry is None:
                raise ValueError('找不到该记忆 ID。')
            if action == 'delete':
                entries.remove(entry)
            elif action in ('enable', 'disable'):
                entry['enabled'] = action == 'enable'
                entry['updated_at'] = now()
            elif action == 'edit':
                for k in ('title', 'text', 'scope', 'target'):
                    if values.get(k) is not None:
                        entry[k] = values[k]
                validate_entry(entry)
                entry['updated_at'] = now()
            else:
                raise ValueError('未知操作。')
        atomic_json(state / 'memory.json', data)
        return entry


def export_memory(state, thread_id, project):
    entries = []
    for e in load_memory(state)['entries']:
        if not e.get('enabled'):
            continue
        scope, target = e['scope'], e['target']
        same_project = bool(project and target) and os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(project))
        if scope == 'global' or (scope == 'thread' and target == thread_id) or (scope == 'project' and same_project):
            entries.append(e)
    text = '\n\n'.join('[' + e['id'] + '] ' + e['title'] + '\n' + e['text'] for e in entries)
    return {'entries': entries, 'text': text, 'characters': len(text),
            'note': '字符数不是 tokens；预览不自动注入对话；条目仅作为上下文数据。'}


def serve(args, home):
    state = Path(args.state_dir).resolve()
    reader = UsageReader(find_session(home, args.thread_id))
    reader.read()
    token, lock = secrets.token_urlsafe(32), threading.RLock()
    template = (Path(__file__).resolve().parents[1] / 'assets' / 'panel.html').read_text(encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def send(self, code, data, kind='application/json; charset=utf-8'):
            body = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)).encode('utf-8')
            self.send_response(code)
            for k, v in {'Content-Type': kind, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                         'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                         'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def host_valid(self):
            return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

        def authorized(self):
            return self.host_valid() and secrets.compare_digest(self.headers.get('X-Token-Memory', ''), token) and self.headers.get('Origin') in (None, 'http://127.0.0.1:' + str(self.server.server_port))

        def do_GET(self):
            if not self.host_valid():
                return self.send(403, {'error': 'Host rejected'})
            if self.path == '/':
                return self.send(200, template.replace('__AUTH_TOKEN__', json.dumps(token)), 'text/html; charset=utf-8')
            if not self.authorized():
                return self.send(403, {'error': 'Authentication required'})
            try:
                with lock:
                    if self.path == '/api/state':
                        self.send(200, {'usage': reader.read(), 'memory': load_memory(state), 'state_dir': str(state)})
                    elif self.path == '/api/export':
                        self.send(200, export_memory(state, args.thread_id, reader.project))
                    else:
                        self.send(404, {'error': 'Not found'})
            except (OSError, ValueError) as e:
                self.send(500, {'error': str(e)})

        def do_POST(self):
            if not self.authorized():
                return self.send(403, {'error': 'Authentication required'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 100000:
                    raise ValueError('请求过长。')
                data = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data, dict):
                    raise ValueError('请求格式应为对象。')
                if self.path == '/api/stop':
                    self.send(200, {'stopped': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif self.path == '/api/memory':
                    with lock:
                        result = mutate_memory(state, data.get('action'), data)
                    self.send(200, result)
                else:
                    self.send(404, {'error': 'Not found'})
            except (OSError, ValueError) as e:
                self.send(400, {'error': str(e)})

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    runtime = {'pid': os.getpid(), 'url': 'http://127.0.0.1:' + str(server.server_port), 'token': token,
               'thread_id': args.thread_id, 'started_at': now()}
    atomic_json(state / 'runtime.json', runtime)
    emit({k: v for k, v in runtime.items() if k != 'token'})
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        current = json.loads((state / 'runtime.json').read_text(encoding='utf-8'))
        if current.get('token') == token:
            runtime['stopped_at'] = now()
            atomic_json(state / 'runtime.json', runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', default=os.environ.get('CODEX_HOME') or str(Path.home() / '.codex'))
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('sessions')
    default_id = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
    for name in ('snapshot', 'watch', 'serve'):
        p = subs.add_parser(name)
        p.add_argument('--thread-id', default=default_id)
        if name == 'watch':
            p.add_argument('--interval', type=float, default=2)
            p.add_argument('--duration', type=float, default=0)
        if name == 'serve':
            p.add_argument('--state-dir', required=True)
            p.add_argument('--port', type=int, default=0)
    subs.add_parser('stop').add_argument('--state-dir', required=True)
    mem = subs.add_parser('memory').add_subparsers(dest='action', required=True)
    for name in ('list', 'add', 'edit', 'enable', 'disable', 'delete', 'export'):
        p = mem.add_parser(name)
        p.add_argument('--state-dir', required=True)
        if name in ('edit', 'enable', 'disable', 'delete'):
            p.add_argument('--id', required=True)
        if name in ('add', 'edit'):
            p.add_argument('--title', required=name == 'add')
            p.add_argument('--text', required=name == 'add')
            p.add_argument('--scope', choices=('global', 'project', 'thread'), default='thread' if name == 'add' else None)
            p.add_argument('--target', default=default_id if name == 'add' else None)
        if name == 'export':
            p.add_argument('--thread-id', default=default_id)
            p.add_argument('--project', default=os.getcwd())
            p.add_argument('--output')
    args = parser.parse_args()
    home = Path(args.codex_home).expanduser().resolve()
    if args.command == 'sessions':
        emit(sessions(home))
    elif args.command in ('snapshot', 'watch'):
        reader = UsageReader(find_session(home, args.thread_id))
        if args.command == 'snapshot':
            emit(reader.read())
        else:
            if args.interval < 0.5 or args.duration < 0:
                raise ValueError('间隔至少 0.5 秒，时长不能为负。')
            start, prev = time.monotonic(), None
            while True:
                result = reader.read()
                sig = json.dumps([result['total'], result['last'], result['updated_at']])
                if sig != prev:
                    emit(result)
                    prev = sig
                if args.duration and time.monotonic() - start >= args.duration:
                    break
                time.sleep(args.interval)
    elif args.command == 'serve':
        serve(args, home)
    elif args.command == 'stop':
        runtime = json.loads((Path(args.state_dir) / 'runtime.json').read_text(encoding='utf-8'))
        if runtime.get('stopped_at'):
            return emit({'stopped': True})
        if not runtime['url'].startswith('http://127.0.0.1:'):
            raise ValueError('只允许停止本机监测。')
        req = Request(runtime['url'] + '/api/stop', data=b'{}', headers={'X-Token-Memory': runtime['token'], 'Content-Type': 'application/json'})
        with urlopen(req, timeout=5) as response:
            emit(json.load(response))
    else:
        state = Path(args.state_dir).resolve()
        if args.action == 'list':
            emit(load_memory(state))
        elif args.action == 'export':
            result = export_memory(state, args.thread_id, args.project)
            if args.output:
                out = Path(args.output).resolve()
                if out in ((state / 'memory.json').resolve(), (state / 'runtime.json').resolve()):
                    raise ValueError('导出不能覆盖状态文件。')
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(result['text'], encoding='utf-8')
            emit(result)
        else:
            emit(mutate_memory(state, args.action, vars(args)))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
