"""Offline chat-only merge. Live Codex is never stopped by this program."""
from __future__ import annotations
import argparse
import array
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid

SCRIPT_ROOT = Path(__file__).resolve().parent
WORK_ROOT = SCRIPT_ROOT
DIRS = ('sessions', 'archived_sessions', 'attachments', 'generated_images', 'visualizations')
PASTED_TEXT_REGISTRY = Path('attachments') / 'pasted-text-attachments.json'
DBS = ('state_5.sqlite', 'thread_history_1.sqlite')
STATE_TABLES = ('projects', 'project_roots', 'thread_sections', 'threads',
                'thread_dynamic_tools', 'thread_attachments', 'thread_spawn_edges')
HISTORY_TABLES = ('thread_turns', 'thread_items', 'thread_realtime_items', 'thread_history_projection_state')
HISTORY_COMPAT_DIR = Path('attachments') / 'codex-zhiru-history-compat-v1'
# Explicit, nullable metadata only. Never omit messages, primary keys, shared
# history boundaries or unknown columns, and never fabricate SQLx migrations.
HISTORY_COMPAT_COLUMNS = {'thread_turns': {'root_turn_id'},
                         'thread_items': {'started_at_ms', 'completed_at_ms'}}
GLOBAL_KEYS = ('electron-saved-workspace-roots', 'projectless-thread-ids',
    'thread-workspace-root-hints', 'project-order', 'electron-workspace-root-labels',
    'pinned-thread-ids', 'thread-projectless-output-directories', 'thread-writable-roots',
    'thread-project-assignments', 'local-projects', 'sidebar-project-thread-orders',
    'electron-thread-read-state-v1', 'app-server-project-id-by-legacy-project-id-by-host',
    'app-server-migrated-pinned-thread-ids-by-host')
SIDEBAR_ATOM_KEYS = ('flat-project-sidebar-preferences-v1', 'sidebar-custom-sections-v3',
    'app-server-pinned-thread-order-v1', 'unified-sidebar-project-order-v1',
    'unified-sidebar-pinned-order-v1', 'sidebar-collapsed-sections-v1')
GLOBAL_KEYS += ('electron-persisted-atom-state',)

def filter_global_state(data):
    result = {key: data[key] for key in GLOBAL_KEYS if key in data and key != 'electron-persisted-atom-state'}
    atom = data.get('electron-persisted-atom-state', {})
    if isinstance(atom, dict):
        result['electron-persisted-atom-state'] = {key: atom[key] for key in SIDEBAR_ATOM_KEYS if key in atom}
    return result
FILES = ('session_index.jsonl', 'history.jsonl', '.codex-global-state.json', '.codex-global-state.json.bak')
PROTECTED_TEXT = {'text', 'message', 'arguments', 'output', 'base_instructions', 'instructions',
                  'prompt', 'title', 'preview', 'first_user_message', 'description', 'input_schema', 'result'}
PATH_KEYS = {'cwd', 'path', 'paths', 'root', 'roots', 'rootPaths', 'writable_roots', 'url',
             'image_url', 'image_path', 'file_path', 'attachment_path', 'workspace', 'directory'}

def log(s):
    print(s, flush=True)

def read_json(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))

def write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.writing')
    with tmp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)

def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def quote(s):
    return '"' + s.replace('"', '""') + '"'

def db_open(p, immutable=False):
    return sqlite3.connect(Path(p).resolve().as_uri() + '?mode=ro' + ('&immutable=1' if immutable else ''), uri=True, timeout=10)

def check_db(c):
    if c.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise RuntimeError('Database integrity check failed')
    if list(c.execute('PRAGMA foreign_key_check')):
        raise RuntimeError('Database foreign-key check failed')

def snapshot_db(src, dst, immutable=False):
    dst.parent.mkdir(parents=True, exist_ok=True)
    a, b = db_open(src, immutable), sqlite3.connect(dst)
    start = time.monotonic()
    def progress(*_):
        if time.monotonic() - start > 120:
            raise RuntimeError('Database remained busy; close Codex and retry')
    try:
        a.backup(b, pages=1024, progress=progress, sleep=0.1)
        check_db(b)
        b.execute('PRAGMA journal_mode=DELETE')
    finally:
        a.close()
        b.close()

def safe_files(root):
    root = Path(root)
    if root.is_symlink() or (root.exists() and root.stat().st_file_attributes & 0x400 if os.name == 'nt' else False):
        raise RuntimeError('Linked data directory is not supported: ' + str(root))
    if not root.exists():
        return
    for p in sorted(root.rglob('*')):
        if p.is_symlink() or (os.name == 'nt' and p.stat().st_file_attributes & 0x400):
            raise RuntimeError('Linked data entry is not supported: ' + str(p))
        if p.is_file():
            yield p

def normalized(s):
    if s.startswith('\\\\?\\UNC\\'):
        return '\\\\' + s[8:]
    return s[4:] if s.startswith('\\\\?\\') else s

class Mapper:
    def __init__(self, old_profile, target, old_home=None):
        self.old = str(PureWindowsPath(old_profile))
        self.new = str(Path(target).parent)
        self.old_home = str(PureWindowsPath(old_home)) if old_home else self.old + '\\.codex'
        self.target = str(target)

    def path(self, s):
        # Exact path prefixes only. Historical prose mentioning paths is unchanged.
        tag = 'local:' if s.startswith('local:') else ''
        raw = s[len(tag):]
        raw = normalized(raw).replace('/', '\\')
        for old, new in ((self.old_home, self.target), (self.old, self.new)):
            if raw.lower() == old.lower() or raw.lower().startswith(old.lower() + '\\'):
                return tag + new + raw[len(old):]
        return s

    def obj(self, value, key='', all_paths=False):
        if not all_paths and key in PROTECTED_TEXT:
            return value
        if isinstance(value, dict):
            return {(self.path(k) if all_paths else k): self.obj(v, k, all_paths) for k, v in value.items()}
        if isinstance(value, list):
            return [self.obj(v, key, all_paths) for v in value]
        if isinstance(value, str) and (all_paths or key in PATH_KEYS or key.endswith(('_path', '_root', '_directory'))):
            return self.path(value)
        return value

def read_attachment_registry(path):
    if not path.exists():
        return {'attachmentPaths': [], 'pendingRemovalPaths': [], 'textExcerptsByPath': {}}
    try:
        data = read_json(path)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('Invalid pasted-text attachment registry JSON') from exc
    fields = {'attachmentPaths', 'pendingRemovalPaths', 'textExcerptsByPath'}
    if not isinstance(data, dict) or not set(data) <= fields:
        raise RuntimeError('Unsupported pasted-text attachment registry format')
    for key in ('attachmentPaths', 'pendingRemovalPaths'):
        if not isinstance(data.get(key), list) or any(not isinstance(p, str) for p in data[key]):
            raise RuntimeError('Invalid pasted-text attachment registry field: ' + key)
    excerpts = data.get('textExcerptsByPath', {})
    if not isinstance(excerpts, dict) or any(not isinstance(v, str) for v in excerpts.values()):
        raise RuntimeError('Invalid pasted-text attachment registry field: textExcerptsByPath')
    return {**data, 'textExcerptsByPath': excerpts}


def attachment_path_key(path):
    return normalized(path).replace('/', '\\').casefold()


def map_attachment_registry(data, mapper):
    # Only registry paths move. Excerpts are historical user text, even when
    # they happen to start with an old profile path.
    return {
        'attachmentPaths': list(dict.fromkeys(mapper.path(p) for p in data['attachmentPaths'])),
        # A source-machine cleanup request must never delete a restored file.
        'pendingRemovalPaths': [],
        'textExcerptsByPath': {mapper.path(p): text for p, text in data['textExcerptsByPath'].items()},
    }


def merge_attachment_registries(current, incoming):
    paths, excerpts = {}, {}
    for registry in (current, incoming):
        for path in registry['attachmentPaths']:
            paths.setdefault(attachment_path_key(path), path)
        for path, text in registry['textExcerptsByPath'].items():
            key = attachment_path_key(path)
            # Keep the current excerpt if the same immutable attachment has a
            # differently truncated or sanitized excerpt in the snapshot.
            excerpts.setdefault(key, (path, text))
    cloud_paths = {attachment_path_key(p) for p in incoming['attachmentPaths']}
    return {
        'attachmentPaths': list(paths.values()),
        # Preserve local cleanup for local-only files. A file restored from the
        # cloud is protected from both machines' pending deletion requests.
        'pendingRemovalPaths': list(dict.fromkeys(
            p for p in current['pendingRemovalPaths'] if attachment_path_key(p) not in cloud_paths)),
        'textExcerptsByPath': {paths.get(key, path): text for key, (path, text) in excerpts.items()},
    }


def assert_attachment_registry_preserved(current, merged):
    paths = {attachment_path_key(p) for p in merged['attachmentPaths']}
    excerpts = {attachment_path_key(p): text for p, text in merged['textExcerptsByPath'].items()}
    if any(attachment_path_key(p) not in paths for p in current['attachmentPaths']):
        raise RuntimeError('A current pasted-text attachment registration was lost')
    if any(excerpts.get(attachment_path_key(p)) != text for p, text in current['textExcerptsByPath'].items()):
        raise RuntimeError('A current pasted-text attachment excerpt changed')


def current_settings(target):
    cfg = target / 'config.toml'
    data = tomllib.loads(cfg.read_text(encoding='utf-8-sig')) if cfg.exists() else {}
    return data.get('model_provider', 'openai'), data.get('model')

def session_storage_id(path):
    found = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$', path.name, re.I)
    if not found:
        raise RuntimeError('Unrecognized transcript filename: ' + path.name)
    return found.group(1)

def ordered_transcripts(source):
    pending = {}
    for directory in ('sessions', 'archived_sessions'):
        for p in safe_files(source / directory):
            if p.suffix != '.jsonl':
                raise RuntimeError('Unexpected session file: ' + p.name)
            with p.open('rb') as f:
                meta = json.loads(f.readline()).get('payload', {})
            storage_id = session_storage_id(p)
            if storage_id in pending:
                raise RuntimeError('Duplicate history segment')
            pending[storage_id] = (p, meta.get('history_base', {}).get('thread_id'))
    result, done = [], set()
    while pending:
        ready = [sid for sid, (_, parent) in pending.items() if parent is None or parent in done]
        if not ready:
            raise RuntimeError('History chain is incomplete or cyclic')
        for sid in ready:
            result.append(pending.pop(sid)[0])
            done.add(sid)
    return result

def transcript_metadata(path):
    with path.open('rb') as stream:
        return json.loads(stream.readline())['payload']


def event_digest(obj):
    kind, payload = obj.get('type'), obj.get('payload', {})
    if kind in ('session_meta', 'turn_context') or (kind == 'event_msg' and payload.get('type') in ('token_count', 'task_started', 'task_complete')):
        return None
    return hashlib.sha256(json.dumps({'type': kind, 'payload': payload}, ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def history_prefix_hash(path, cut, mapper):
    """Compare exact ordinal prefixes, ignoring only layout/provider metadata."""
    meta = transcript_metadata(path)
    start = meta.get('history_base', {}).get('end_ordinal_exclusive', 0)
    if not isinstance(cut, int) or cut < start:
        raise RuntimeError('Invalid shared history boundary')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for ordinal in range(start, cut):
            raw = stream.readline()
            if not raw.endswith(b'\n'):
                raise RuntimeError('Shared history prefix is incomplete')
            obj = mapper.obj(json.loads(raw))
            if ordinal == start:
                payload = obj['payload']
                payload.pop('model_provider', None)
                if payload.get('history_base'):
                    payload['history_base'].pop('end_byte_offset', None)
            digest.update(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8') + b'\n')
    return digest.hexdigest()


def verify_external_history(current, target, requirements):
    if not requirements:
        return
    paths = {session_storage_id(p): p for p in ordered_transcripts(current)}
    mapper = Mapper(str(target.parent), target)
    for requirement in requirements:
        path = paths.get(requirement['storage_id'])
        if path is None or history_prefix_hash(path, requirement['cut'], mapper) != requirement['sha256']:
            raise RuntimeError('Required shared history changed or is missing; compare and select again')


def preserved_history_requirements(current, updated, target):
    """A selected parent must not silently alter a preserved child's ancestry."""
    paths = {session_storage_id(p): p for p in ordered_transcripts(current)}
    metadata = {sid: transcript_metadata(p) for sid, p in paths.items()}
    cuts, seen = {}, set()

    def visit(sid):
        if sid in seen:
            return
        seen.add(sid)
        base = metadata[sid].get('history_base')
        if base:
            parent, cut = base['thread_id'], base['end_ordinal_exclusive']
            cuts[parent] = max(cuts.get(parent, cut), cut)
            visit(parent)

    for sid, meta in metadata.items():
        if (meta.get('id') or meta.get('session_id')) not in updated:
            visit(sid)
    mapper = Mapper(str(target.parent), target)
    return [{'storage_id': sid, 'cut': cut, 'sha256': history_prefix_hash(paths[sid], cut, mapper)}
            for sid, cut in cuts.items()]


def repair_merged_history(stage, incoming_paths):
    """Repair selected heads against preserved parents; never rewrite a parent."""
    offsets_by_id = {}
    hist = sqlite3.connect(stage / DBS[1])
    try:
        for path in ordered_transcripts(stage):
            selected = path.relative_to(stage).as_posix() in incoming_paths
            meta = transcript_metadata(path)
            base = meta.get('history_base')
            start = base['end_ordinal_exclusive'] if base else 0
            new_offset = None
            if base:
                parent_start, parent_offsets = offsets_by_id[base['thread_id']]
                cut = start - parent_start
                if not 0 <= cut < len(parent_offsets):
                    raise RuntimeError('Merged shared history boundary is invalid')
                new_offset = parent_offsets[cut]
                if not selected and base.get('end_byte_offset') != new_offset:
                    raise RuntimeError('Selected update would change a preserved chat history dependency')
            offsets = array.array('Q', [0])
            # Change only the first metadata line of an incoming transcript.
            # All message bytes and every unselected transcript stay untouched.
            temporary = path.with_suffix('.tmp') if selected and base and base.get('end_byte_offset') != new_offset else None
            try:
                with path.open('rb') as stream:
                    out = temporary.open('wb') if temporary else None
                    try:
                        for index, raw in enumerate(stream):
                            if not raw.endswith(b'\n'):
                                raise RuntimeError('Incomplete merged history transcript')
                            if index == 0 and out:
                                obj = json.loads(raw)
                                obj['payload']['history_base']['end_byte_offset'] = new_offset
                                raw = (json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
                            if out:
                                out.write(raw)
                            offsets.append(offsets[-1] + len(raw))
                    finally:
                        if out:
                            out.close()
                if temporary:
                    os.replace(temporary, path)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
            sid = session_storage_id(path)
            offsets_by_id[sid] = (start, offsets)
            if selected:
                repair_segment_offsets(hist, sid, start, offsets)
        hist.commit()
        check_db(hist)
    finally:
        hist.close()

def repair_segment_offsets(hist, storage_id, ordinal_base, offsets):
    for rowid, start, end in hist.execute('SELECT rowid,rollout_ordinal,rollout_end_ordinal FROM thread_turns WHERE thread_id=?', (storage_id,)).fetchall():
        a = start - ordinal_base if start is not None else -1
        b = end - ordinal_base if end is not None else None
        if not 0 <= a < len(offsets) - 1 or (b is not None and not 0 <= b < len(offsets) - 1):
            raise RuntimeError('Invalid historical turn ordinal for segment ' + storage_id)
        hist.execute('UPDATE thread_turns SET rollout_byte_offset=?,rollout_end_byte_offset=? WHERE rowid=?', (offsets[a], offsets[b + 1] if b is not None else None, rowid))
    proj = hist.execute('SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?', (storage_id,)).fetchone()
    if proj:
        index = proj[0] - ordinal_base
        if not 0 <= index < len(offsets):
            raise RuntimeError('Invalid history projection ordinal for segment ' + storage_id)
        hist.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?', (offsets[index], storage_id))

def prepare(source, target):
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise RuntimeError('Source and target must be separate directories')
    data = WORK_ROOT / 'incoming'
    if data.exists():
        raise RuntimeError('Prepared package already exists; use verify')
    if any((source / (name + '-wal')).exists() and (source / (name + '-wal')).stat().st_size for name in DBS):
        raise RuntimeError('Source chat databases contain WAL files. A consistent closed-source copy is required.')
    c = db_open(source / DBS[0], True)
    rows = c.execute('SELECT id,rollout_path,model_provider,model FROM threads').fetchall()
    c.close()
    old_homes = set()
    for _, path, _, _ in rows:
        m = re.match(r'(?i)^(.*)\\(?:sessions|archived_sessions)\\', normalized(path).replace('/', '\\'))
        if not m:
            raise RuntimeError('Unrecognized source transcript path')
        old_homes.add(m.group(1))
    if not rows:
        old_homes.add(str(target))
    if len(old_homes) != 1:
        raise RuntimeError('Source has multiple profile roots')
    old_home = old_homes.pop()
    old_profile = str(PureWindowsPath(old_home).parent)
    mapper = Mapper(old_profile, target, old_home)
    provider, model = current_settings(target)
    overrides = {sid: {'provider': p, 'model': mod} for sid, _, p, mod in rows}
    data.mkdir()
    for name in DBS:
        snapshot_db(source / name, data / name, immutable=True)
    c = sqlite3.connect(data / DBS[0])
    c.execute('PRAGMA foreign_keys=OFF')
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if table not in STATE_TABLES and not table.startswith(('sqlite_', '_sqlx_')):
            c.execute('DELETE FROM ' + quote(table))
    for table in STATE_TABLES:
        cols = [r[1] for r in c.execute('PRAGMA table_info(' + quote(table) + ')')]
        for row in c.execute('SELECT rowid,* FROM ' + quote(table)).fetchall():
            changes = []
            for k, val in zip(cols, row[1:]):
                new = val
                if k in ('rollout_path', 'cwd', 'path') and isinstance(val, str):
                    new = mapper.path(val)
                elif k in ('sandbox_policy', 'payload', 'metadata') and isinstance(val, str):
                    try:
                        obj = json.loads(val)
                        mapped = mapper.obj(obj)
                        if mapped != obj:
                            new = json.dumps(mapped, ensure_ascii=False, separators=(',', ':'))
                    except json.JSONDecodeError:
                        pass
                elif table == 'threads' and k == 'model_provider':
                    new = provider
                elif table == 'threads' and k == 'model' and model and val and not val.startswith('gpt-'):
                    new = model
                if new != val:
                    changes.append((k, new))
            if changes:
                c.execute('UPDATE ' + quote(table) + ' SET ' + ','.join(quote(k) + '=?' for k, _ in changes) + ' WHERE rowid=?', [v for _, v in changes] + [row[0]])
    active_paths = {str(Path(normalized(path))).lower(): sid for sid, path in c.execute('SELECT id,rollout_path FROM threads')}
    c.commit()
    c.execute('VACUUM')
    check_db(c)
    c.close()
    hist = sqlite3.connect(data / DBS[1])
    line_counts = {}
    session_ids = {}
    segment_offsets = {}
    segment_events, prepared_catalog = {}, {}
    original_fingerprints = []
    ordered = ordered_transcripts(source)
    other_files = [p for directory in DIRS[2:] for p in safe_files(source / directory)]
    for src in ordered + other_files:
        directory = src.relative_to(source).parts[0]
        if True:
            rel = src.relative_to(source)
            dst = data / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            before = (src.stat().st_size, src.stat().st_mtime_ns)
            if directory in ('sessions', 'archived_sessions') and src.suffix == '.jsonl':
                offsets = array.array('Q', [0])
                original_hash = hashlib.sha256()
                sid = None
                storage_id = session_storage_id(src)
                ordinal_base = 0
                events = []
                with src.open('rb') as f, dst.open('wb') as out:
                    for ordinal, raw in enumerate(f):
                        if not raw.endswith(b'\n'):
                            raise RuntimeError('聊天历史末尾未写完，请在旧电脑完全退出 Codex 后重新导出：' + str(rel))
                        original_hash.update(raw)
                        if not raw.strip():
                            raise RuntimeError('Empty transcript line: ' + str(rel))
                        obj = json.loads(raw)
                        new = mapper.obj(obj)
                        if ordinal == 0:
                            payload = new.get('payload', {})
                            sid = payload.get('id') or payload.get('session_id')
                            if not sid:
                                raise RuntimeError('Transcript has no session ID: ' + str(rel))
                            base = payload.get('history_base')
                            if base:
                                parent_base, parent_offsets = segment_offsets[base['thread_id']]
                                ordinal_base = base['end_ordinal_exclusive']
                                cut = ordinal_base - parent_base
                                if not 0 <= cut < len(parent_offsets):
                                    raise RuntimeError('Invalid history segment boundary')
                                base['end_byte_offset'] = parent_offsets[cut]
                                events = [(n, h) for n, h in segment_events[base['thread_id']] if n < ordinal_base]
                        if new.get('type') == 'session_meta' and isinstance(new.get('payload'), dict):
                            new['payload']['model_provider'] = provider
                        rendered = (json.dumps(new, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8') if new != obj else raw
                        out.write(rendered)
                        offsets.append(offsets[-1] + len(rendered))
                        digest = event_digest(new)
                        if digest: events.append((ordinal_base + ordinal, digest))
                        if ordinal and ordinal % 10000 == 0:
                            log('整理聊天历史 %d/%d · 当前记录 %d 条' % (len(session_ids) + 1, len(ordered), ordinal))
                session_ids[rel.as_posix()] = sid
                segment_offsets[storage_id] = (ordinal_base, offsets)
                segment_events[storage_id] = events
                repair_segment_offsets(hist, storage_id, ordinal_base, offsets)
                active_sid = active_paths.get(str(target / rel).lower())
                if active_sid:
                    if active_sid != sid:
                        raise RuntimeError('Transcript ID does not match database')
                    prepared_catalog[sid] = {'title': sid, 'events': [h for _, h in events]}
                    line_counts[sid] = len(offsets) - 1
                original_fingerprints.append({'path': rel.as_posix(), 'size': before[0], 'sha256': original_hash.hexdigest()})
                log('Prepared transcript ' + str(len(session_ids)) + '/' + str(len(ordered)))
            else:
                if rel == PASTED_TEXT_REGISTRY:
                    write_json(dst, map_attachment_registry(read_attachment_registry(src), mapper))
                else:
                    shutil.copy2(src, dst)
                original_fingerprints.append({'path': rel.as_posix(), 'size': before[0], 'sha256': sha(src)})
            if before != (src.stat().st_size, src.stat().st_mtime_ns):
                raise RuntimeError('Source changed while preparing: ' + str(rel))
    for table in ('thread_items', 'thread_realtime_items'):
        for rowid, val in hist.execute('SELECT rowid,item_json FROM ' + table):
            obj = json.loads(val)
            mapped = mapper.obj(obj)
            if obj != mapped:
                hist.execute('UPDATE ' + table + ' SET item_json=? WHERE rowid=?', (json.dumps(mapped, ensure_ascii=False, separators=(',', ':')), rowid))
    hist.commit()
    check_db(hist)
    hist.close()
    if len(line_counts) != len(rows):
        raise RuntimeError('Some database threads lack an active transcript')
    for name in ('session_index.jsonl', 'history.jsonl'):
        if (source / name).exists():
            shutil.copy2(source / name, data / name)
    g = read_json(source / '.codex-global-state.json')
    write_json(data / '.codex-global-state.json', mapper.obj(filter_global_state(g), all_paths=True))
    inventory = [{'path': p.relative_to(data).as_posix(), 'size': p.stat().st_size, 'sha256': sha(p)} for p in safe_files(data)]
    manifest = {'format': 1, 'package_id': str(uuid.uuid4()), 'prepared_at': dt.datetime.now().isoformat(),
        'source': str(source), 'old_profile': old_profile, 'target': str(target), 'provider': provider,
        'model_fallback': model, 'source_thread_count': len(rows), 'session_file_count': len(session_ids),
        'history_segment_count': len(segment_offsets), 'segmented_chat_count': sum(1 for sid in set(session_ids.values()) if list(session_ids.values()).count(sid) > 1),
        'session_ids': session_ids, 'original_thread_settings': overrides, 'line_counts': line_counts,
        'files': inventory, 'original_source_files': original_fingerprints, 'prepared_catalog': prepared_catalog,
        'excluded': ['auth.json', 'config.toml', 'browser', 'plugins', 'skills', 'memories', 'goals', 'queue', 'logs', 'runtime files', 'remote enrollments']}
    write_json(WORK_ROOT / 'package.json', manifest)
    log('PACKAGE READY: ' + str(len(rows)) + ' old chats; all transcripts parsed and history offsets repaired.')

def verify_package():
    m = read_json(WORK_ROOT / 'package.json')
    if m.get('format') != 1:
        raise RuntimeError('Unknown package format')
    base = (WORK_ROOT / 'incoming').resolve()
    actual = {p.relative_to(base).as_posix() for p in safe_files(base)}
    expected = {x['path'] for x in m['files']}
    if actual != expected or len(expected) != len(m['files']):
        raise RuntimeError('Package file inventory mismatch')
    for entry in m['files']:
        p = (base / entry['path']).resolve()
        if base not in p.parents or p.stat().st_size != entry['size'] or sha(p) != entry['sha256']:
            raise RuntimeError('Package checksum failed: ' + entry['path'])
    for name in DBS:
        c = db_open(base / name, True)
        check_db(c)
        c.close()
    return m

def managed_names(target):
    names = set(DIRS) | set(FILES) | set(DBS)
    for name in DBS:
        names.update(name + suffix for suffix in ('-wal', '-shm', '-journal'))
    for p in target.glob('*.sqlite'):
        if re.fullmatch(r'(state|thread_history)_\d+\.sqlite', p.name) and p.name not in DBS:
            raise RuntimeError('Codex database version changed. Revalidate the merge before restoring.')
    if target.is_symlink() or (os.name == 'nt' and target.stat().st_file_attributes & 0x400):
        raise RuntimeError('Target data directory must not be a link')
    for name in names:
        p = target / name
        if p.is_symlink() or (p.exists() and os.name == 'nt' and p.stat().st_file_attributes & 0x400):
            raise RuntimeError('Linked target entry is not supported: ' + name)
    return sorted(names)

def tree_signature(target, names):
    values = []
    for name in names:
        p = target / name
        entries = list(safe_files(p)) if p.is_dir() else ([p] if p.exists() else [])
        for f in entries:
            st = f.stat()
            values.append((f.relative_to(target).as_posix(), st.st_size, st.st_mtime_ns))
    return values

def take_current(target, dest, rehearsal=False, include_assets=True, link_assets=False):
    dest.mkdir(parents=True)
    for name in DIRS if include_assets else ():
        for p in safe_files(target / name):
            d = dest / p.relative_to(target)
            d.parent.mkdir(parents=True, exist_ok=True)
            if link_assets:
                try:
                    os.link(p, d)
                    continue
                except OSError: pass
            shutil.copy2(p, d)
    for name in FILES:
        if (target / name).exists():
            shutil.copy2(target / name, dest / name)
    for name in DBS:
        if not (target / name).exists():
            raise RuntimeError('Initialize Codex on this machine first: missing ' + name)
        if rehearsal:
            # Read-only SQLite backup includes committed WAL pages, including
            # schema migrations. Reading just the main file can see an old schema.
            snapshot_db(target / name, dest / name)
        else:
            # Work from a closed-source file copy, including every pending WAL page.
            # Opening the real target with SQLite could otherwise create sidecar files.
            raw = dest.parent / 'raw-current-databases'
            raw.mkdir(exist_ok=True)
            for suffix in ('', '-wal', '-shm'):
                p = target / (name + suffix)
                if p.exists():
                    shutil.copy2(p, raw / p.name)
            snapshot_db(raw / name, dest / name)

def merge_value(current, incoming):
    if isinstance(current, dict) and isinstance(incoming, dict):
        result = dict(current)
        for key, value in incoming.items():
            result[key] = merge_value(result[key], value) if key in result else value
        return result
    if isinstance(current, list) and isinstance(incoming, list):
        return current + [v for v in incoming if v not in current]
    return current

def cols(c, table):
    return [r[1] for r in c.execute('PRAGMA table_info(' + quote(table) + ')')]

def schema_compatibility(source, target):
    differences, failures = [], []
    for name, tables in ((DBS[0], STATE_TABLES), (DBS[1], HISTORY_TABLES)):
        a, b = db_open(source / name, True), db_open(target / name, True)
        try:
            for table in tables:
                incoming = list(a.execute('PRAGMA table_info(' + quote(table) + ')'))
                local = list(b.execute('PRAGMA table_info(' + quote(table) + ')'))
                incoming_names, local_names = {r[1] for r in incoming}, {r[1] for r in local}
                missing = incoming_names - local_names
                required = [r[1] for r in local if r[1] not in incoming_names and r[3] and r[4] is None]
                keys_a = [r[1] for r in sorted(incoming, key=lambda r: r[5]) if r[5]]
                keys_b = [r[1] for r in sorted(local, key=lambda r: r[5]) if r[5]]
                compatible = bool(incoming and local and keys_a and keys_a == keys_b and not required)
                compatible = compatible and missing <= HISTORY_COMPAT_COLUMNS.get(table, set())
                compatible = compatible and not any(r[3] or r[5] for r in incoming if r[1] in missing)
                if missing or required or not compatible:
                    differences.append({'database':name, 'table':table, 'missing_columns':sorted(missing),
                                        'required_columns':required, 'compatible':compatible})
                    if not compatible:
                        reason = table + ('：目标缺少 ' + ', '.join(sorted(missing)) if missing else '')
                        if required: reason += '；备份缺少必填字段 ' + ', '.join(required)
                        if not incoming or not local or keys_a != keys_b: reason += '；表或主键结构不同'
                        failures.append(reason)
        finally:
            a.close(); b.close()
    reason = '' if not failures else ('数据库版本不兼容（' + '；'.join(failures[:4]) +
             '）。请将两台电脑的 Codex 更新到相同版本后重新核对；本机聊天未更改。')
    return {'supported':not failures, 'differences':differences, 'reason':reason}


def insert_table(src, dst, table, existing_ids, preserve_conflicting_rows=False, compatibility=None):
    source_columns = cols(src, table)
    target_cols = cols(dst, table)
    extra = set(source_columns) - set(target_cols)
    info = list(src.execute('PRAGMA table_info(' + quote(table) + ')'))
    optional = HISTORY_COMPAT_COLUMNS.get(table, set())
    if extra and (compatibility is None or not extra <= optional or
                  any(r[3] or r[5] for r in info if r[1] in extra)):
        raise RuntimeError('数据库版本不兼容：' + table + '；目标缺少字段：' + ', '.join(sorted(extra)) +
                           '。请将两台电脑的 Codex 更新到相同版本后重新核对。未写入本机聊天。')
    columns = [column for column in source_columns if column in target_cols]
    pks = [r[1] for r in sorted(src.execute('PRAGMA table_info(' + quote(table) + ')'), key=lambda x: x[5]) if r[5]]
    if not pks or not set(pks) <= set(target_cols):
        raise RuntimeError('Missing primary key: ' + table)
    query = 'INSERT INTO ' + quote(table) + '(' + ','.join(map(quote, columns)) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
    inserted = 0
    for source_row in src.execute('SELECT ' + ','.join(map(quote, source_columns)) + ' FROM ' + quote(table)):
        record = dict(zip(source_columns, source_row))
        sid = record.get('thread_id', record.get('child_thread_id', record.get('id') if table == 'threads' else None))
        if sid in existing_ids:
            continue
        row = tuple(record[column] for column in columns)
        where = ' AND '.join(quote(k) + '=?' for k in pks)
        prior = dst.execute('SELECT ' + ','.join(map(quote, columns)) + ' FROM ' + quote(table) + ' WHERE ' + where, [record[k] for k in pks]).fetchone()
        if prior is not None:
            if prior != row:
                if preserve_conflicting_rows:
                    continue
                raise RuntimeError('Conflicting existing record in ' + table + '; no live files changed')
            continue
        dst.execute(query, row)
        values = {column: record[column] for column in sorted(extra) if record[column] is not None}
        if values:
            compatibility.append({'table': table, 'thread_id': record['thread_id'],
                                  'key': {key: record[key] for key in pks}, 'values': values})
        inserted += 1
    return inserted


def save_history_compat(stage, records):
    if not records:
        return
    data = json.dumps({'format': 'codex-zhiru-history-compat', 'version': 1, 'records': records},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    path = stage / HISTORY_COMPAT_DIR / (hashlib.sha256(data).hexdigest() + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise RuntimeError('兼容元数据校验失败，未写入本机聊天。')
    path.write_bytes(data)


def load_history_compat(path):
    if path.suffix != '.json' or not re.fullmatch('[0-9a-f]{64}', path.stem) or sha(path) != path.stem:
        raise RuntimeError('兼容元数据文件校验失败，未写入本机聊天。')
    data = read_json(path)
    if not isinstance(data, dict) or data.get('format') != 'codex-zhiru-history-compat' or data.get('version') != 1 or not isinstance(data.get('records'), list):
        raise RuntimeError('不支持的聊天兼容元数据，未写入本机聊天。')
    for record in data['records']:
        if not isinstance(record, dict) or not isinstance(record.get('thread_id'), str):
            raise RuntimeError('聊天兼容元数据格式错误，未写入本机聊天。')
        table = record.get('table')
        values, key = record.get('values'), record.get('key')
        if table not in HISTORY_COMPAT_COLUMNS or not isinstance(values, dict) or not isinstance(key, dict):
            raise RuntimeError('聊天兼容元数据格式错误，未写入本机聊天。')
        if not set(values) <= HISTORY_COMPAT_COLUMNS[table] or any(type(value) not in (str, int, type(None)) for value in list(values.values()) + list(key.values())):
            raise RuntimeError('聊天兼容元数据包含不允许的字段或类型。')
    return data['records']


def restore_history_compat(db, stage, selected_ids):
    restored = 0
    primary = {}
    restored_values = {}
    for path in safe_files(stage / HISTORY_COMPAT_DIR):
        for record in load_history_compat(path):
            table = record.get('table')
            values, key = record.get('values'), record.get('key')
            if record.get('thread_id') not in selected_ids:
                continue
            if table not in primary:
                info = list(db.execute('PRAGMA table_info(' + quote(table) + ')'))
                primary[table] = ([r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]], {r[1] for r in info})
            keys, available = primary[table]
            if set(key) != set(keys):
                raise RuntimeError('聊天兼容元数据的主键与目标版本不符，请使用相同版本 Codex。')
            for column, value in values.items():
                if column not in available or value is None:
                    continue
                where = ' AND '.join(quote(k) + ' IS ?' for k in keys)
                row = db.execute('SELECT thread_id,' + quote(column) + ' FROM ' + quote(table) + ' WHERE ' + where,
                                 [key[k] for k in keys]).fetchone()
                identity = (table, tuple(key[k] for k in keys), column)
                if identity in restored_values and restored_values[identity] != value:
                    raise RuntimeError('同一聊天的兼容元数据有冲突，已停止隔离合并，未写入本机聊天。请使用相同版本 Codex 重新核对。')
                if row and row[0] == record['thread_id'] and row[1] is None:
                    db.execute('UPDATE ' + quote(table) + ' SET ' + quote(column) + '=? WHERE ' + where,
                               [value] + [key[k] for k in keys])
                    restored += 1
                    restored_values[identity] = value
    return restored

def row_belongs_to_ids(columns, row, cloud_ids):
    record = dict(zip(columns, row))
    if 'child_thread_id' in record and 'parent_thread_id' in record:
        return record['child_thread_id'] in cloud_ids
    if record.get('id') in cloud_ids and 'id' in record:
        return True
    return any(record.get(key) in cloud_ids for key in ('thread_id', 'parent_thread_id', 'child_thread_id'))


def assert_current_rows_preserved(before, after, cloud_ids, cloud_history_ids=None):
    for name in DBS:
        replaced_ids = cloud_history_ids if name == DBS[1] and cloud_history_ids is not None else cloud_ids
        a, b = db_open(before / name, True), db_open(after / name, True)
        try:
            for (table,) in a.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                columns = cols(a, table)
                for row in a.execute('SELECT * FROM ' + quote(table)):
                    if row_belongs_to_ids(columns, row, replaced_ids):
                        continue
                    where = ' AND '.join(quote(k) + ' IS ?' for k in columns)
                    if not b.execute('SELECT 1 FROM ' + quote(table) + ' WHERE ' + where, row).fetchone():
                        raise RuntimeError('A current database row was changed: ' + table)
        finally:
            a.close()
            b.close()

def transcript_thread_id(path):
    try:
        with Path(path).open('rb') as f:
            payload = json.loads(f.readline()).get('payload', {})
        return payload.get('id') or payload.get('session_id')
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError('Unreadable session transcript: ' + str(path)) from exc


def remove_cloud_rows(db, table, cloud_ids):
    columns = set(cols(db, table))
    keys = []
    if table == 'threads' and 'id' in columns:
        keys.append('id')
    # A parent update must not erase a preserved child's relationship.
    reference_keys = ('child_thread_id',) if table == 'thread_spawn_edges' else ('thread_id', 'parent_thread_id', 'child_thread_id')
    keys.extend(key for key in reference_keys if key in columns)
    if not keys or not cloud_ids:
        return 0
    placeholders = ','.join('?' for _ in cloud_ids)
    clauses = [quote(key) + ' IN (' + placeholders + ')' for key in keys]
    before = db.total_changes
    db.execute('DELETE FROM ' + quote(table) + ' WHERE ' + ' OR '.join(clauses), tuple(cloud_ids) * len(keys))
    return db.total_changes - before


def asset_thread_id(relative_path):
    # These app-owned directories explicitly include the owning chat ID.
    # Generic attachment directory IDs identify files, not chats.
    parts = relative_path.parts
    if len(parts) >= 3 and parts[0] == 'generated_images':
        return parts[1]
    if len(parts) >= 6 and parts[0] == 'visualizations' and re.fullmatch(r'\d{4}/\d{2}/\d{2}', '/'.join(parts[1:4])):
        return parts[4]
    return None


def build_merge(current, stage, target, manifest):
    verify_external_history(current, target, manifest.get('external_history', []))
    shutil.copytree(current, stage)
    incoming = WORK_ROOT / 'incoming'
    c = db_open(current / DBS[0], True)
    existing_ids = {r[0] for r in c.execute('SELECT id FROM threads')}
    c.close()
    source_ids = set(manifest['original_thread_settings'])
    added = source_ids - existing_ids
    updated = source_ids & existing_ids
    preserved_history = preserved_history_requirements(current, updated, target)
    cloud_history_ids = source_ids | {session_storage_id(Path(p)) for p in manifest['session_ids']}
    if cloud_history_ids & (existing_ids - source_ids):
        raise RuntimeError('Cloud history segment conflicts with a local-only chat ID')
    updated_assets = set()
    # Cloud is authoritative for a chat it contains. Local-only chats and all
    # credentials remain untouched; the install's rollback copy is reversible.
    for directory in ('sessions', 'archived_sessions'):
        for p in list(safe_files(stage / directory)):
            if transcript_thread_id(p) in updated:
                cloud_history_ids.add(session_storage_id(p))
                p.unlink()
    for directory in DIRS:
        for p in safe_files(incoming / directory):
            rel = p.relative_to(incoming)
            d = stage / rel
            if rel == PASTED_TEXT_REGISTRY:
                write_json(d, merge_attachment_registries(read_attachment_registry(d), read_attachment_registry(p)))
                continue
            if d.exists():
                if sha(p) != sha(d):
                    if asset_thread_id(rel) not in source_ids:
                        raise RuntimeError('File conflict: ' + str(rel))
                    shutil.copy2(p, d)
                    updated_assets.add(rel)
            else:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, d)
    table_counts = {}
    compatibility, restored_metadata = [], 0
    for name, tables in ((DBS[0], STATE_TABLES), (DBS[1], HISTORY_TABLES)):
        replaced_ids = cloud_history_ids if name == DBS[1] else updated
        a, b = db_open(incoming / name, True), sqlite3.connect(stage / name)
        try:
            # This database is isolated staging. Suppress cascading deletion
            # while replacing a parent; validate ALL foreign keys afterwards.
            b.execute('PRAGMA foreign_keys=OFF')
            with b:
                for table in reversed(tables):
                    remove_cloud_rows(b, table, replaced_ids)
                for table in tables:
                    table_counts[table] = insert_table(
                        a, b, table, existing_ids - updated,
                        preserve_conflicting_rows=table in ('projects', 'project_roots', 'thread_sections'),
                        compatibility=compatibility,
                    )
                    if table == 'thread_spawn_edges':
                        for edge in manifest.get('deferred_spawn_edges', []):
                            if edge.get('child_thread_id') not in source_ids or edge.get('parent_thread_id') not in existing_ids | source_ids:
                                raise RuntimeError('Selected child chat requires its parent; select the parent too')
                            if not set(edge) <= set(cols(b, table)):
                                raise RuntimeError('Incompatible parent/child relationship schema')
                            b.execute('INSERT INTO thread_spawn_edges (' + ','.join(map(quote, edge)) + ') VALUES (' +
                                      ','.join('?' for _ in edge) + ')', tuple(edge.values()))
                if name == DBS[1]:
                    restored_metadata = restore_history_compat(b, stage, cloud_history_ids - (existing_ids - updated))
            check_db(b)
        finally:
            a.close()
            b.close()
    save_history_compat(stage, compatibility)
    repair_merged_history(stage, set(manifest['session_ids']))
    verify_external_history(stage, target, preserved_history)
    for name in ('session_index.jsonl', 'history.jsonl'):
        old, new = stage / name, incoming / name
        if not new.exists():
            continue
        old_lines = old.read_bytes().splitlines() if old.exists() else []
        seen = set()
        with old.open('wb') as out:
            for line in old_lines:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get('id', obj.get('session_id')) in updated:
                    continue
                out.write(line + b'\n')
                seen.add(line)
            for line in new.read_bytes().splitlines():
                if line not in seen and line.strip():
                    out.write(line + b'\n')
                    seen.add(line)
    global_path = stage / '.codex-global-state.json'
    g = read_json(global_path) if global_path.exists() else {}
    for key, value in read_json(incoming / global_path.name).items():
        if key not in GLOBAL_KEYS:
            raise RuntimeError('Unexpected global-state key')
        g[key] = merge_value(g[key], value) if key in g else value
    write_json(global_path, g)
    shutil.copy2(global_path, stage / '.codex-global-state.json.bak')
    assert_current_rows_preserved(current, stage, updated, cloud_history_ids)
    c = db_open(stage / DBS[0], True)
    result_ids = {r[0] for r in c.execute('SELECT id FROM threads')}
    if result_ids != existing_ids | source_ids:
        raise RuntimeError('Merged thread ID set is incorrect')
    missing = []
    for sid, rollout, cwd, history_mode, provider in c.execute('SELECT id,rollout_path,cwd,history_mode,model_provider FROM threads'):
        if sid not in source_ids:
            continue
        rel = Path(normalized(rollout)).relative_to(target)
        if not (stage / rel).is_file():
            raise RuntimeError('Merged transcript is missing')
        with (stage / rel).open('rb') as f:
            first = json.loads(f.readline())
        if first.get('payload', {}).get('id') != sid or provider != manifest['provider']:
            raise RuntimeError('Merged transcript metadata mismatch')
        if history_mode != first.get('payload', {}).get('history_mode', 'legacy'):
            raise RuntimeError('History format does not match transcript')
        if not Path(cwd).exists():
            missing.append(cwd)
    c.close()
    for directory in DIRS:
        for p in safe_files(current / directory):
            if directory in ('sessions', 'archived_sessions') and transcript_thread_id(p) in updated:
                continue
            if p.relative_to(current) in updated_assets:
                continue
            if p.relative_to(current) == PASTED_TEXT_REGISTRY and (incoming / PASTED_TEXT_REGISTRY).exists():
                assert_attachment_registry_preserved(read_attachment_registry(p), read_attachment_registry(stage / PASTED_TEXT_REGISTRY))
                continue
            if sha(p) != sha(stage / p.relative_to(current)):
                raise RuntimeError('A current chat or attachment file changed')
    return {'existing_chats': len(existing_ids), 'added_chats': len(added), 'total_chats': len(result_ids),
            'cloud_asset_files_updated': len(updated_assets),
            'overlapping_chats_updated_from_cloud': len(updated), 'table_rows_added': table_counts,
            'missing_project_directories': sorted(set(missing)), 'local_only_database_rows_preserved': True,
            'local_only_transcripts_preserved': True, 'current_login_and_config_preserved': True,
            'compatibility_rows_preserved': len(compatibility), 'compatibility_values_restored': restored_metadata}

def running_codex():
    import importlib.util
    module_path = SCRIPT_ROOT / 'win_process.py'
    if not module_path.exists(): module_path = SCRIPT_ROOT.parent / 'src' / 'win_process.py'
    spec = importlib.util.spec_from_file_location('zhiru_windows_process', module_path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return [p['pid'] for p in module.owners()]

def require_idle():
    if running_codex():
        raise RuntimeError('Codex is running. No installation is allowed. Close all Codex sessions first.')

def wait_idle():
    start = time.monotonic()
    announced = False
    while running_codex():
        if not announced:
            log('请完全退出 Codex 桌面端、CLI 和 IDE Codex 会话。等待最多 10 分钟，Ctrl+C 可取消。')
            announced = True
        if time.monotonic() - start > 600:
            raise RuntimeError('Codex is still running; close it and double-click again')
        time.sleep(3)

def credential_hashes(target):
    return {name: sha(target / name) if (target / name).exists() else None for name in ('auth.json', 'config.toml', 'installation_id')}

def install_stage(stage, target, backup, names, report, package_id, idle_check=require_idle, fail_after=None, helper_source=None):
    idle_check()
    backup.mkdir(parents=True)
    # Make the independent recovery launcher available BEFORE moving live files.
    if helper_source is not None:
        for helper in helper_source.iterdir():
            shutil.copy2(helper, backup / helper.name)
    original = backup / 'original'
    original.mkdir()
    steps = [{'name': name, 'had_original': (target / name).exists(), 'has_new': (stage / name).exists()} for name in names]
    journal = {'format': 1, 'status': 'installing', 'target': str(target), 'package_id': package_id,
               'steps': steps, 'report': report, 'started_at': dt.datetime.now().isoformat()}
    write_json(backup / 'transaction.json', journal)
    try:
        count = 0
        for step in steps:
            idle_check()
            name = step['name']
            if step['had_original']:
                os.replace(target / name, original / name)
            if step['has_new']:
                os.replace(stage / name, target / name)
            count += 1
            if fail_after is not None and count == fail_after:
                raise RuntimeError('Injected installation failure')
        journal['status'] = 'complete'
        journal['completed_at'] = dt.datetime.now().isoformat()
        write_json(backup / 'transaction.json', journal)
    except BaseException:
        try:
            rollback(backup, idle_check, failure=True)
        except BaseException as error:
            log('Recovery pending: ' + str(error) + '. Keep this backup: ' + str(backup))
        raise

def rollback(backup, idle_check=require_idle, failure=False):
    idle_check()
    info = read_json(backup / 'transaction.json')
    if info['status'] not in ('installing', 'complete', 'rollback_in_progress'):
        raise RuntimeError('This backup does not require rollback')
    target = Path(info['target']).resolve()
    if backup.resolve().parent != target.parent / 'Codex-chat-merge-backups':
        raise RuntimeError('Unexpected backup location')
    allowed = set(managed_names(target))
    if any(s['name'] not in allowed for s in info['steps']):
        raise RuntimeError('Unsafe rollback entry')
    undo = backup / 'state-before-rollback'
    undo.mkdir(exist_ok=True)
    info['status'] = 'rollback_in_progress'
    write_json(backup / 'transaction.json', info)
    for step in reversed(info['steps']):
        idle_check()
        name = step['name']
        original = backup / 'original' / name
        current = target / name
        if original.exists():
            if current.exists():
                if (undo / name).exists():
                    raise RuntimeError('Rollback destination already occupied')
                os.replace(current, undo / name)
            os.replace(original, current)
        elif not step['had_original'] and current.exists():
            if (undo / name).exists():
                raise RuntimeError('Rollback destination already occupied')
            os.replace(current, undo / name)
    info['status'] = 'failed_and_reverted' if failure else 'rolled_back'
    write_json(backup / 'transaction.json', info)

def restore(rehearsal=False):
    m = verify_package()
    target = Path(m['target'])
    names = managed_names(target)
    backup_root = target.parent / 'Codex-chat-merge-backups'
    if not rehearsal:
        wait_idle()
        if current_settings(target)[0] != m['provider']:
            raise RuntimeError('Current model provider changed since preparation; revalidate first')
        for journal in sorted(backup_root.glob('*/transaction.json')) if backup_root.exists() else []:
            info = read_json(journal)
            if info['status'] in ('installing', 'rollback_in_progress'):
                raise RuntimeError('An interrupted operation needs rollback first: ' + str(journal.parent))
            if info.get('package_id') == m['package_id'] and info['status'] == 'complete':
                log('这个恢复包已经合并成功，无需重复运行。回滚备份：' + str(journal.parent))
                return
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    work = WORK_ROOT / ('rehearsal' if rehearsal else 'run') / stamp
    current, stage = work / 'current', work / 'merged'
    before = tree_signature(target, names)
    expected = m.get('expected_target_signature')
    if expected is not None and [list(v) for v in before] != expected:
        raise RuntimeError('Local chats changed since selection. Run the update tool again; nothing was installed.')
    credentials = credential_hashes(target)
    log('Preparing an isolated copy of current chat state...')
    take_current(target, current, rehearsal=rehearsal)
    log('Merging old chats and checking preservation of current chats...')
    report = build_merge(current, stage, target, m)
    report['package_id'] = m['package_id']
    report['rehearsal'] = rehearsal
    report['live_wal_included_in_rehearsal'] = rehearsal
    write_json(work / 'report.json', report)
    if rehearsal:
        write_json(WORK_ROOT / 'VALIDATION.json', report)
        log(json.dumps(report, ensure_ascii=False))
        return work
    require_idle()
    if before != tree_signature(target, names):
        raise RuntimeError('Current chat files changed during preparation. Retry after exiting Codex.')
    if credentials != credential_hashes(target):
        raise RuntimeError('Login or configuration changed during preparation')
    backup = backup_root / stamp
    log('Rollback backup: ' + str(backup))
    install_stage(stage, target, backup, names, report, m['package_id'])
    if credentials != credential_hashes(target):
        raise RuntimeError('Unexpected credential change; inspect the preserved backup')
    log('合并成功：原有 ' + str(report['existing_chats']) + ' 条，新增 ' + str(report['added_chats']) + ' 条，共 ' + str(report['total_chats']) + ' 条。')
    log('回滚备份已保留：' + str(backup))
    log('现在可以重新打开 Codex，检查普通聊天、归档聊天和历史消息。')

def rollback_latest():
    m = read_json(WORK_ROOT / 'package.json')
    wait_idle()
    root = Path(m['target']).parent / 'Codex-chat-merge-backups'
    candidates = []
    for p in root.glob('*/transaction.json'):
        info = read_json(p)
        if info.get('package_id') == m['package_id'] and info['status'] in ('complete', 'installing', 'rollback_in_progress'):
            candidates.append(p.parent)
    if not candidates:
        raise RuntimeError('No merge backup is available for rollback')
    backup = sorted(candidates)[-1]
    rollback(backup)
    log('回滚完成。合并后的数据也保留在：' + str(backup / 'state-before-rollback'))

def main():
    global WORK_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'verify', 'rehearse', 'restore', 'rollback'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--work', type=Path, default=SCRIPT_ROOT)
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    WORK_ROOT = args.work.resolve()
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    # A Windows byte-range lock prevents two copies of this launcher running together.
    lock = (WORK_ROOT / '.runner.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        if args.action == 'prepare':
            prepare(args.source, args.target)
        elif args.action == 'verify':
            m = verify_package()
            log('校验成功：' + str(m['source_thread_count']) + ' 条旧聊天，' + str(len(m['files'])) + ' 个恢复文件。')
        elif args.action == 'rehearse':
            restore(rehearsal=True)
        elif args.action == 'restore':
            restore()
        elif args.action == 'rollback' and args.backup:
            rollback(args.backup.resolve())
            log('回滚完成。合并后的数据也保留在：' + str(args.backup.resolve() / 'state-before-rollback'))
        else:
            rollback_latest()
    finally:
        lock.close()

if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        log('ERROR: ' + str(exc))
        sys.exit(1)


