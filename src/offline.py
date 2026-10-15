"""codex智入: local-only worker. No network calls, no forced process termination."""
from __future__ import annotations
import contextlib
import ctypes
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'src'))
import chat_merge as m
import chat_select as selection
import chat_snapshot as snapshot
import win_process
import sidebar
import asset_conflicts

VERSION = '1.3.0'
FORMAT = 'codex-zhiru-offline'
CACHE = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'CodexZhiru' / 'jobs'
RUNTIME_SHA = '009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b'


def emit(kind, **values):
    print(json.dumps({'type': kind, **values}, ensure_ascii=False), flush=True)


def progress(message, percent=None):
    text = str(message)
    if text.startswith('Prepared transcript '):
        text = '整理聊天历史 ' + text[len('Prepared transcript '):]
    elif text.startswith('PACKAGE READY:'):
        text = '聊天历史整理完成，正在检查数据。'
    emit('progress', message=text, percent=percent)


def new_job():
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / uuid.uuid4().hex[:12]
    path.mkdir()
    return path


def job_path(token):
    if not re.fullmatch(r'[0-9a-f]{12}', str(token)):
        raise ValueError('无效的临时任务。请重新选择备份包。')
    result = (CACHE / token).resolve()
    if result.parent != CACHE.resolve():
        raise ValueError('临时路径不安全。')
    return result


def clean_job(path):
    path = Path(path).resolve()
    if path.parent != CACHE.resolve() or not re.fullmatch(r'[0-9a-f]{12}', path.name):
        raise ValueError('只允许清理本软件生成的临时任务目录。')
    if path.exists():
        shutil.rmtree(path)


def desktop():
    if os.name == 'nt':
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / 'Desktop'


def validate_home(home, allow_empty=False, allow_incomplete=False):
    home = Path(home).resolve()
    protected = {Path(home.anchor), Path.home(), desktop(), Path.home() / 'Documents', ROOT}
    if home in protected or home.is_symlink() or (home.exists() and home.stat().st_file_attributes & 0x400):
        raise ValueError('请选择 Codex 数据目录，不能选择桌面、用户主目录或链接目录。')
    if not allow_incomplete and not (home / m.DBS[0]).is_file():
        if not allow_empty or (home.exists() and any(home.iterdir())):
            raise ValueError('此目录没有 Codex 聊天数据库。可先启动 Codex 并登录一次，或选择空目录。')
    return home


def owners():
    return win_process.owners()


def ensure_closed(auto_close=True):
    found = owners()
    if not found:
        return
    if auto_close:
        progress('正在请求 Codex 和使用它的编辑器正常退出；请处理未保存内容的提示。')
        win_process.close_windows(found)
        # A normal Windows application-exit notification also reaches tray apps.
        # It is scoped to verified PIDs/start times, with force flag always zero.
        win_process.graceful_shutdown(found)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if not owners():
            return
    raise RuntimeError('Codex 或其后台会话仍在运行。请保存工作，从托盘退出 Codex，并关闭相关终端/IDE 会话，再重试。软件没有强杀进程，也没有写入聊天。')


def idle():
    if owners():
        raise RuntimeError('检测到 Codex 重新启动；已停止操作，请完全退出后重试。')


def discover():
    candidates = []
    if os.environ.get('CODEX_HOME'):
        candidates.append(Path(os.environ['CODEX_HOME']))
    candidates.append(Path(os.environ.get('USERPROFILE', str(Path.home()))) / '.codex')
    candidates.append(Path.home() / '.codex')
    seen, profiles = set(), []
    for path in candidates:
        path = path.resolve()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        profiles.append({'path': str(path), 'exists': (path / m.DBS[0]).is_file(),
                         'label': '当前 Codex 数据' if not profiles else '另一个 Codex 数据目录'})
    return {'profiles': profiles, 'desktop': str(desktop()), 'version': VERSION}


def safe_title(value, fallback):
    value = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value or '')).strip()
    return value[:180] or fallback


def read_state(home, work=None):
    # A short readonly transaction includes committed WAL data without copying
    # and integrity-scanning an entire database for every list or click.
    db = m.db_open(home / m.DBS[0])
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        threads = [dict(row) for row in db.execute('SELECT * FROM threads')]
        projects = {r['id']: dict(r) for r in db.execute('SELECT * FROM projects')} if 'projects' in tables else {}
        edges = {r['child_thread_id']: r['parent_thread_id'] for r in db.execute('SELECT * FROM thread_spawn_edges')} if 'thread_spawn_edges' in tables else {}
        roots = [dict(r) for r in db.execute('SELECT * FROM project_roots')] if 'project_roots' in tables else []
        sections = {r['id']: dict(r) for r in db.execute('SELECT * FROM thread_sections')} if 'thread_sections' in tables else {}
        return threads, projects, edges, roots, sections
    finally:
        db.close()


def segments(home):
    result = {}
    for directory in m.DIRS[:2]:
        for path in m.safe_files(home / directory):
            if path.suffix != '.jsonl':
                continue
            sid = m.session_storage_id(path)
            if sid in result:
                raise RuntimeError('发现重复的历史分段，请保留原目录后单独检查：' + sid)
            result[sid] = {'path': path, 'meta': m.transcript_metadata(path)}
    return result


def list_chats(home, *, snapshot_home=None):
    home = validate_home(home)
    # Export uses the already captured metadata, not another SQLite connection
    # to the original WAL-mode database. Even mode=ro can create/update WAL/SHM
    # sidecars and would trip our source-consistency check. Session files stay
    # streamed from home to avoid a second full copy of the conversation assets.
    metadata_home = validate_home(snapshot_home) if snapshot_home is not None else home
    with contextlib.nullcontext():
        rows, projects, edges, roots, sections = read_state(metadata_home)
        storage = segments(home)
        indexed_names = {}
        index = metadata_home / 'session_index.jsonl'
        if index.exists():
            with index.open('rb') as stream:
                for raw in stream:
                    if not raw.endswith(b'\n'):
                        continue
                    try:
                        item = json.loads(raw)
                        if item.get('thread_name'):
                            indexed_names[item['id']] = item['thread_name']
                    except (ValueError, KeyError):
                        continue
        result = []
        for row in rows:
            sid = row['id']
            head = m.session_storage_id(Path(row['rollout_path'].replace('\\', '/')))
            entry = storage.get(head)
            meta = entry['meta'] if entry else {}
            project = projects.get(row.get('project_id'), {})
            parent = meta.get('forked_from_id') or meta.get('parent_thread_id') or edges.get(sid)
            if not parent and meta.get('history_base'):
                ancestor = storage.get(meta['history_base']['thread_id'], {}).get('meta', {})
                owner = ancestor.get('id') or ancestor.get('session_id')
                if owner != sid:
                    parent = owner
            title = row.get('name') or row.get('title') or indexed_names.get(sid) or meta.get('agent_nickname') or row.get('first_user_message')
            result.append({'id': sid, 'title': safe_title(title, '未命名聊天 · ' + sid[:8]),
                           'project_id': row.get('project_id') or '',
                           'project': safe_title(project.get('name'), '未分组聊天'),
                           'parent_id': parent or '', 'archived': bool(row.get('archived')),
                           'available': entry is not None, 'head': head,
                           'updated_at': row.get('updated_at_ms') or int(row.get('updated_at', 0) or 0) * 1000})
        gpath = metadata_home / '.codex-global-state.json'
        global_state = m.read_json(gpath) if gpath.exists() else {}
        result = sidebar.apply(result, rows, projects, roots, sections, global_state, home)
        return {'home': str(home), 'chats': result,
                'projects': len({r['project_id'] for r in result if r['project_id']}), 'segments': len(storage),
                'archived': sum(r['archived'] for r in result), 'helpers': sum(r['helper'] for r in result)}


def iter_history(storage, sid, cut=None, seen=None):
    seen = set() if seen is None else set(seen)
    if sid in seen or sid not in storage:
        raise RuntimeError('聊天的共享历史缺失或形成循环，无法完整预览。')
    seen.add(sid)
    entry = storage[sid]
    base = entry['meta'].get('history_base')
    start = base['end_ordinal_exclusive'] if base else 0
    if cut is not None and cut < start:
        raise RuntimeError('聊天历史边界不正确。')
    if base:
        yield from iter_history(storage, base['thread_id'], start, seen)
    with entry['path'].open('rb') as stream:
        for n, raw in enumerate(stream):
            if cut is not None and start + n >= cut:
                break
            if not raw.endswith(b'\n'):
                break  # Live preview ignores an unfinished final record.
            yield json.loads(raw)


def message_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    pieces = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get('type') in ('input_text', 'output_text', 'text', 'refusal'):
            pieces.append(str(item.get('text') or item.get('refusal') or ''))
        elif item.get('type') in ('input_image', 'image', 'output_image'):
            reference = str(item.get('image_url') or item.get('image_path') or '')
            pieces.append('〔图片附件〕 ' + ('内嵌图片' if reference.startswith('data:') else reference))
    return '\n'.join(pieces)


def read_messages(storage, head, tools):
    chain, current, seen = [], head, set()
    while current:
        if current in seen or current not in storage: raise RuntimeError('共享历史缺失或形成循环。')
        seen.add(current); path = storage[current]['path']; st = path.stat()
        chain.append([str(path.resolve()), st.st_size, st.st_mtime_ns])
        current = storage[current]['meta'].get('history_base', {}).get('thread_id')
    key = hashlib.sha256(json.dumps([chain, bool(tools)]).encode()).hexdigest()
    view_root = CACHE.parent / 'views'; view_root.mkdir(parents=True, exist_ok=True)
    cache = view_root / (key + '.json')
    if cache.exists():
        try: return m.read_json(cache)
        except (ValueError, OSError): pass
    messages = []
    for obj in iter_history(storage, head):
        kind, p = obj.get('type'), obj.get('payload', {})
        role, text, origin = None, '', kind
        if kind == 'response_item' and p.get('type') == 'message' and p.get('role') in ('user', 'assistant'):
            role, text = p['role'], message_text(p.get('content'))
        elif kind == 'event_msg' and p.get('type') in ('user_message', 'agent_message'):
            role = 'user' if p['type'] == 'user_message' else 'assistant'
            text = str(p.get('message') or '')
        elif tools and kind == 'response_item' and p.get('type') in ('function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
            role = 'tool'
            text = str(p.get('name') or p.get('type')) + '\n' + str(p.get('arguments') or p.get('input') or p.get('output') or '')
        if not role or not text.strip():
            continue
        if messages and messages[-1]['role'] == role and messages[-1]['text'] == text and messages[-1]['origin'] != origin:
            # A response and its mirrored UI event are one visible message.
            messages[-1]['origin'] = origin
            continue
        messages.append({'role': role, 'text': text, 'time': obj.get('timestamp') or '', 'origin': origin})
    m.write_json(cache, messages)
    return messages


def conversation(home, sid, start=-1, tools=False):
    info = list_chats(home)
    row = next((r for r in info['chats'] if r['id'] == sid), None)
    if not row or not row['available']:
        raise ValueError('这台电脑没有这条聊天的历史，或历史文件缺失。可切换查看备份中的对话。')
    messages = read_messages(segments(Path(home)), row['head'], tools)
    count = len(messages)
    start = max(0, count - 60) if int(start) < 0 else min(max(0, int(start)), max(0, count - 1))
    return {'chat': row, 'messages': messages[start:start + 60], 'start': start, 'total': count}


def signature(home):
    return [list(x) for x in m.tree_signature(home, m.managed_names(home))] if home.exists() else []


def check_export_source(home, before, phase):
    after = signature(home)
    if after == before:
        return
    original = {entry[0]: entry[1:] for entry in before}
    current = {entry[0]: entry[1:] for entry in after}
    changed = []
    for name in sorted(original.keys() | current.keys()):
        if original.get(name) != current.get(name):
            kind = '新增' if name not in original else '删除' if name not in current else '修改'
            changed.append(kind + '：' + name)
    details = '\n'.join(changed[:8])
    if len(changed) > 8:
        details += '\n另有 %d 个文件发生变化。' % (len(changed) - 8)
    raise RuntimeError(phase + '检测到源文件变化，已停止且未发布备份。'
                       '请确认相关后台会话已退出，也没有其他备份或同步软件正在修改此目录。\n'
                       '变更文件：\n' + details)


def capture(home, dest, include_assets=True):
    idle()
    before = signature(home)
    m.take_current(home, dest, include_assets=include_assets)
    check_export_source(home, before, '复制期间')
    # Offline archives preserve conversation text verbatim. Account files are
    # excluded by the allowlist; do not redact chat text and create false forks.
    # Only remove non-chat state tables. Rewriting every TEXT value in both
    # databases made the previous exporter unnecessarily slow.
    with contextlib.closing(sqlite3.connect(dest / m.DBS[0])) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if table not in m.STATE_TABLES and not table.startswith(('sqlite_', '_sqlx_')):
                db.execute('DELETE FROM ' + m.quote(table))
        db.commit()
        db.execute('VACUUM')
    if not (dest / '.codex-global-state.json').exists():
        m.write_json(dest / '.codex-global-state.json', {})
    else:
        m.write_json(dest / '.codex-global-state.json', m.filter_global_state(m.read_json(dest / '.codex-global-state.json')))
    backup_state = dest / '.codex-global-state.json.bak'
    if backup_state.exists(): backup_state.unlink()
    idle()
    return before, 0


def prepare_readers(home, archive_home=None, auto_close=True):
    """Independent metadata + read-only transcript hardlinks survive installation.

    Never hardlink SQLite files. A viewer must not reopen the original database
    or a preview job that the importing worker may clean up after success.
    """
    home = validate_home(home, allow_empty=True)
    ensure_closed(auto_close)
    work = new_job()
    result = {'token': work.name, 'home': '', 'archive_home': ''}
    try:
        for key, source in (('home', home), ('archive_home', Path(archive_home) if archive_home else None)):
            if source is None or not (source / m.DBS[0]).exists():
                continue
            source = validate_home(source)
            before = signature(source)
            destination = work / key
            raw = work / (key + '-raw')
            destination.mkdir(); raw.mkdir()
            for suffix in ('', '-wal', '-shm'):
                path = source / (m.DBS[0] + suffix)
                if path.exists(): shutil.copy2(path, raw / path.name)
            m.snapshot_db(raw / m.DBS[0], destination / m.DBS[0])
            for name in ('session_index.jsonl',):
                if (source / name).exists(): shutil.copy2(source / name, destination / name)
            state = source / '.codex-global-state.json'
            m.write_json(destination / state.name, m.filter_global_state(m.read_json(state)) if state.exists() else {})
            for directory in m.DIRS[:2]:
                for path in m.safe_files(source / directory):
                    if path.suffix != '.jsonl': continue
                    target = destination / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try: os.link(path, target)
                    except OSError: shutil.copy2(path, target)
            check_export_source(source, before, '准备只读浏览副本时')
            result[key] = str(destination)
        idle()
        return result
    except BaseException:
        clean_job(work)
        raise


def export_temporary(work, output):
    # The normal Desktop export stays in application cache, not beside the
    # finished backup. A different volume needs its own hidden staging folder
    # so the final rename is atomic rather than a partially visible file copy.
    if work.drive.casefold() == output.resolve().drive.casefold():
        return work / 'archive.partial', None
    folder = Path(tempfile.mkdtemp(prefix='.codex-zhiru-export-', dir=output.parent))
    if os.name == 'nt':
        attributes = folder.stat().st_file_attributes
        if not ctypes.windll.kernel32.SetFileAttributesW(str(folder), attributes | 0x2):
            folder.rmdir()
            raise OSError('无法隐藏跨磁盘的打包临时目录，已停止导出。')
    return folder / 'archive.partial', folder


def approved_path(name):
    if not isinstance(name, str) or not name or not PurePosixPath(name).parts or not snapshot.approved_member(name):
        return False
    for part in PurePosixPath(name).parts:
        if part.endswith(('.', ' ')) or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part):
            return False
    return True


def verify_bundle(bundle, destination=None):
    """Verify inventory and every payload hash; optionally extract safe files."""
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if 'codex-zhiru.json' not in names or len(names) != len(set(n.casefold() for n in names)):
            raise ValueError('不是有效的 codex智入 备份包，或包含重复文件。')
        meta_info = archive.getinfo('codex-zhiru.json')
        if meta_info.file_size > 16 * 1024 * 1024:
            raise ValueError('备份清单异常。')
        manifest = json.loads(archive.read(meta_info).decode('utf-8-sig'))
        if manifest.get('format') != FORMAT or manifest.get('version') != 1:
            raise ValueError('不支持此备份格式。请使用匹配版本的 codex智入。')
        files = manifest.get('files', [])
        if not isinstance(files, list) or not files or any(not isinstance(f, dict) or not approved_path(f.get('path')) or
                type(f.get('size')) is not int or f['size'] < 0 or
                not re.fullmatch('[0-9a-f]{64}', str(f.get('sha256', ''))) for f in files):
            raise ValueError('备份包包含不允许的文件路径。')
        inventory = {'data/' + f['path']: f for f in files}
        if len(inventory) != len(files) or set(names) != set(inventory) | {'codex-zhiru.json'}:
            raise ValueError('备份包文件清单不一致。')
        size = sum(int(f['size']) for f in files)
        consumed, last_progress = 0, time.monotonic()
        if destination:
            destination.mkdir(parents=True, exist_ok=False)
            if shutil.disk_usage(destination).free < size * 3 + 256 * 1024 * 1024:
                raise RuntimeError('磁盘剩余空间不足。请为解压、比较和回滚准备更多空间。')
        for i, (name, entry) in enumerate(inventory.items(), 1):
            info = archive.getinfo(name)
            if info.file_size != entry['size'] or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('备份文件大小或类型不正确。')
            path = destination / entry['path'] if destination else None
            if path:
                if len(str(path)) >= 250 or destination.resolve() not in path.resolve().parents:
                    raise ValueError('解压路径过长或不安全。请将工具放到更短的目录。')
                path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.open(info) as source, (path.open('wb') if path else contextlib.nullcontext()) as out:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                    if out:
                        out.write(block)
                    consumed += len(block)
                    if time.monotonic() - last_progress >= 0.5:
                        percent = int(consumed * 100 / max(size, 1))
                        progress('校验并解压 %d%% · %d/%d 个文件 · %.0f/%.0f MB' % (percent, i, len(files), consumed/1048576, size/1048576), percent)
                        last_progress = time.monotonic()
            if digest.hexdigest() != entry['sha256']:
                raise ValueError('备份完整性校验失败，未写入本机聊天。')
            if i % 25 == 0:
                progress('校验备份文件 %d/%d' % (i, len(files)))
        return manifest


def export_bundle(home, output=None, auto_close=True):
    home = validate_home(home)
    assert_no_pending(home)
    ensure_closed(auto_close)
    output = Path(output) if output else desktop() / ('codex智入-聊天备份-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.czr')
    output = output.resolve()
    if output.exists() or output.with_name(output.name + '.partial').exists():
        raise ValueError('同名备份已经存在，请使用另一个文件名。')
    work = new_job()
    partial, external_temp = None, None
    try:
        progress('读取已退出 Codex 的完整聊天数据，包括归档和分支。')
        before, redacted = capture(home, work / 'copy', include_assets=False)
        incoming = work / 'copy'
        catalog = list_chats(home, snapshot_home=incoming)
        payload = list(m.safe_files(incoming))
        payload += [p for directory in m.DIRS for p in m.safe_files(home / directory)]
        manifest = {'format': FORMAT, 'version': 1, 'tool_version': VERSION,
                    'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'chats': catalog['chats'], 'projects': catalog['projects'],
                    'redacted_occurrences': redacted, 'files': [],
                    'excludes': ['登录凭据', 'API配置', '浏览器登录', '插件', '项目源代码', '自动化队列']}
        output.parent.mkdir(parents=True, exist_ok=True)
        partial, external_temp = export_temporary(work, output)
        required = sum(p.stat().st_size for p in payload)
        if min(shutil.disk_usage(output.parent).free, shutil.disk_usage(partial.parent).free) < required + 128 * 1024 * 1024:
            raise RuntimeError('桌面所在磁盘空间不足，无法生成完整备份包。')
        progress('正在压缩为一个离线包，请保持窗口打开。')
        completed, last = 0, time.monotonic()
        with zipfile.ZipFile(partial, 'w', zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for number, path in enumerate(payload, 1):
                rel = path.relative_to(incoming if incoming in path.parents else home).as_posix()
                if not approved_path(rel): raise ValueError('不支持的备份路径：' + rel)
                info = zipfile.ZipInfo('data/' + rel)
                info.compress_type = zipfile.ZIP_DEFLATED if path.suffix.lower() in ('.jsonl', '.json', '.sqlite', '.html', '.txt', '.md', '.svg') else zipfile.ZIP_STORED
                info._compresslevel = 1
                digest = hashlib.sha256(); count = 0
                with path.open('rb') as source, archive.open(info, 'w', force_zip64=True) as out:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block); out.write(block); count += len(block); completed += len(block)
                        if time.monotonic() - last >= 0.5:
                            percent = int(completed * 100 / max(required, 1))
                            progress('快速打包 %d%% · %d/%d 个文件 · %.0f/%.0f MB' % (percent, number, len(payload), completed/1048576, required/1048576), percent)
                            last = time.monotonic()
                manifest['files'].append({'path': rel, 'size': count, 'sha256': digest.hexdigest()})
            archive.writestr('codex-zhiru.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
        # Hash exactly the bytes written; check the completed ZIP inventory and
        # central directory. The importer still verifies every payload hash.
        with zipfile.ZipFile(partial) as check:
            if len(check.infolist()) != len(payload) + 1 or any(check.getinfo('data/' + f['path']).file_size != f['size'] for f in manifest['files']):
                raise RuntimeError('打包未完成，请重试。')
        progress('打包完成，正在确认源数据保持一致。', 100)
        idle()
        check_export_source(home, before, '导出期间')
        if output.exists():
            raise RuntimeError('打包期间出现同名文件，已保留该文件并停止发布。请换一个文件名重试。')
        if os.name == 'nt':
            os.rename(partial, output)  # Windows refuses to replace a racing existing file.
        else:
            os.link(partial, output); partial.unlink()
        return {'path': str(output), 'size': output.stat().st_size, 'chats': len(catalog['chats']),
                'projects': catalog['projects'], 'redacted': redacted}
    finally:
        if partial is not None and partial.exists():
            partial.unlink()
        if external_temp is not None:
            resolved = external_temp.resolve()
            if resolved.parent == output.parent.resolve() and resolved.name.startswith('.codex-zhiru-export-'):
                resolved.rmdir()
        clean_job(work)


def empty_baseline(source, dest):
    dest.mkdir()
    for name, tables in ((m.DBS[0], m.STATE_TABLES), (m.DBS[1], m.HISTORY_TABLES)):
        shutil.copy2(source / name, dest / name)
        db = sqlite3.connect(dest / name)
        try:
            db.execute('PRAGMA foreign_keys=OFF')
            for table in reversed(tables):
                db.execute('DELETE FROM ' + m.quote(table))
            db.commit()
            m.check_db(db)
        finally:
            db.close()
    m.write_json(dest / '.codex-global-state.json', {})


def preview_import(bundle, home, auto_close=True):
    home = validate_home(home, allow_empty=True)
    assert_no_pending(home)
    ensure_closed(auto_close)
    work = new_job()
    try:
        progress('检查离线备份包；不会写入本机聊天。')
        bundle_meta = verify_bundle(Path(bundle), work / 'data')
        incoming = selection.prepare_package(work / 'data', home, work / 'pack')
        current = work / 'current'
        before = signature(home)
        if (home / m.DBS[0]).exists():
            # Baseline files are only read, never edited. The final merged stage
            # uses independent copies, so a hardlink cannot overwrite live data.
            m.take_current(home, current, link_assets=True)
        else:
            empty_baseline(incoming, current)
        if signature(home) != before:
            raise RuntimeError('核对期间本机聊天发生变化，请关闭 Codex 后重试。')
        progress('按消息内容核对新旧，不使用电脑时间判断覆盖。')
        source_catalog = m.read_json(work / 'pack' / 'package.json').get('prepared_catalog') or selection.catalog(incoming)
        destination_catalog = selection.catalog(current)
        states = selection.classify(source_catalog, destination_catalog)
        selected = [sid for sid, state in states.items() if state in ('new', 'newer')]
        safe, required, blocked = selection.history_selection(incoming, current, home, selected, states)
        schema = m.schema_compatibility(incoming, current) if safe else {'supported': True, 'differences': [], 'reason': ''}
        if not schema['supported']: safe = set()
        names = {r['id']: r for r in list_chats(incoming)['chats']}
        # Persist complete readable names for instant conversation lookup and
        # correct context when a change belongs to an internal child chat.
        m.write_json(work / 'chat-names.json', names)
        labels = {'new': '新增聊天', 'newer': '备份有新增对话', 'conflict': '双方历史不同，保留本机',
                  'same': '相同，跳过', 'older': '本机更新，跳过'}
        changes = []
        for sid, state in states.items():
            if state in ('same', 'older'):
                continue
            row = dict(names[sid])
            row.update(status=state, reason=labels[state], selectable=sid in safe,
                       source_records=len(source_catalog[sid]['events']),
                       local_records=len(destination_catalog.get(sid, {}).get('events', [])))
            if sid in blocked:
                parent = blocked[sid]['parent_id']
                row['reason'] = '共享历史无法安全复用；父聊天：' + names.get(parent, {}).get('title', parent)
            if not schema['supported'] and state in ('new', 'newer'): row['reason'] = schema['reason']
            changes.append(row)
        changes.sort(key=lambda r: (r.get('group_rank', 0), r.get('helper', False), r.get('sort_order', 0)))
        job = {'home': str(home), 'signature': before, 'credential_hashes': m.credential_hashes(home),
               'states': states, 'selected': sorted(safe), 'created_at': time.time(), 'used': False}
        m.write_json(work / 'job.json', job)
        return {'token': work.name, 'home': str(home), 'archive_home': str(incoming), 'changes': changes,
                'source_chats': len(source_catalog), 'local_chats': len(destination_catalog),
                'same': list(states.values()).count('same'), 'local_newer': list(states.values()).count('older'),
                'safe': len(safe), 'blocked': len(blocked), 'conflicts': list(states.values()).count('conflict'),
                'schema': schema,
                'created_at': bundle_meta['created_at'], 'new_profile': not (home / m.DBS[0]).exists()}
    except BaseException:
        clean_job(work)
        raise


def rollback_helper(backup):
    # An independent offline helper survives removal of the original app folder.
    shutil.copy2(ROOT / 'vendor' / 'python-3.11.9-embed-amd64.zip', backup / 'python-runtime.zip')
    shutil.copy2(ROOT / 'lib' / 'chat_merge.py', backup / 'chat_merge.py')
    shutil.copy2(ROOT / 'src' / 'win_process.py', backup / 'win_process.py')
    script = r'''$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSCommandPath
Write-Host 'Please exit Codex, CLI and related IDE sessions before rollback.'
if ((Get-FileHash -LiteralPath (Join-Path $root 'python-runtime.zip') -Algorithm SHA256).Hash -ne 'RUNTIME_HASH') { throw 'Runtime checksum failed.' }
if (-not (Test-Path -LiteralPath (Join-Path $root 'runtime\python.exe'))) { Expand-Archive -LiteralPath (Join-Path $root 'python-runtime.zip') -DestinationPath (Join-Path $root 'runtime') -Force }
& (Join-Path $root 'runtime\python.exe') -I -X utf8 (Join-Path $root 'chat_merge.py') rollback --backup $root
pause
'''.replace('RUNTIME_HASH', RUNTIME_SHA.upper())
    (backup / 'rollback.ps1').write_text(script, encoding='utf-8-sig')
    (backup / '恢复到导入前.cmd').write_text('@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0rollback.ps1"\r\n', encoding='ascii')


def apply_import(token, selected, auto_close=True):
    work = job_path(token)
    job = m.read_json(work / 'job.json')
    home = validate_home(job['home'], allow_empty=True)
    assert_no_pending(home)
    selected = set(selected)
    if job['used']:
        raise ValueError('这份预览已经导入完成，请重新核对。')
    if not selected:
        return {'noop': True, 'added': 0, 'updated': 0, 'selected': 0, 'backup': ''}
    if not selected <= set(job['selected']):
        raise ValueError('选择里包含不能导入的聊天，请取消相应勾选。预览仍可继续使用。')
    ensure_closed(auto_close)
    if signature(home) != job['signature'] or m.credential_hashes(home) != job['credential_hashes']:
        raise RuntimeError('预览之后本机记录或配置有变化。为避免覆盖新对话，请重新选择备份包进行核对。')
    safe, external, blocked = selection.history_selection(work / 'pack' / 'incoming', work / 'current', home, selected, job['states'])
    if blocked or safe != selected:
        raise RuntimeError('当前勾选的分支还需要同时勾选可导入的父聊天。请回到列表补选，不能单独覆盖共享历史。')
    attempt = work / ('attempt-' + uuid.uuid4().hex[:8])
    attempt.mkdir()
    try:
        return install_attempt(work, job, home, selected, external, attempt)
    finally:
        if attempt.exists() and attempt.resolve().parent == work.resolve() and re.fullmatch(r'attempt-[0-9a-f]{8}', attempt.name):
            shutil.rmtree(attempt)


def install_attempt(work, job, home, selected, external, attempt):
    def isolated_copy(source, destination):
        if Path(source).suffix.lower() not in ('.sqlite', '.json', '.jsonl'):
            try:
                os.link(source, destination)
                return destination
            except OSError: pass
        return shutil.copy2(source, destination)
    # Selection may prune an isolated attempt, never the reusable preview.
    shutil.copytree(work / 'pack', attempt / 'pack', copy_function=isolated_copy)
    manifest = selection.restrict_package(attempt / 'pack', selected, external)
    renamed = asset_conflicts.resolve(attempt / 'pack' / 'incoming', work / 'current', home, manifest, m)
    progress('在隔离目录演练合并，核对项目、分支、消息偏移和原有记录。')
    stage = attempt / 'merged'
    report = m.build_merge(work / 'current', stage, home, manifest)
    idle()
    if signature(home) != job['signature'] or m.credential_hashes(home) != job['credential_hashes']:
        raise RuntimeError('正式写入前检测到本机状态改变，已停止。请重新预览。')
    home.mkdir(parents=True, exist_ok=True)
    backup = home.parent / 'Codex-chat-merge-backups' / ('zhiru-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    # Prepare helper before transactional install; failures here cannot alter chats.
    helper = attempt / 'rollback-helper'
    helper.mkdir()
    rollback_helper(helper)
    local_stage = None
    try:
        # os.replace must stay on the destination volume, even for CODEX_HOME on D:.
        if stage.drive.casefold() != home.drive.casefold():
            local_stage = Path(tempfile.mkdtemp(prefix='.codex-zhiru-stage-', dir=home.parent))
            shutil.copytree(stage, local_stage / 'data')
            stage = local_stage / 'data'
        idle()
        if signature(home) != job['signature']:
            raise RuntimeError('复制期间本机聊天发生变化，已停止写入。请重新核对。')
        progress('正在保留回滚副本并安装已勾选的聊天，请勿关闭窗口。')
        m.install_stage(stage, home, backup, m.managed_names(home), report, manifest['package_id'],
                        idle_check=idle, helper_source=helper)
    except BaseException as exc:
        if (backup / 'transaction.json').exists():
            raise RuntimeError(str(exc) + '\n恢复记录和独立回滚工具位于：' + str(backup)) from exc
        raise
    finally:
        if local_stage and local_stage.resolve().parent == home.parent and local_stage.name.startswith('.codex-zhiru-stage-'):
            shutil.rmtree(local_stage)
    if m.credential_hashes(home) != job['credential_hashes']:
        raise RuntimeError('登录配置在操作期间被其他程序改变，请保留回滚副本并检查：' + str(backup))
    job['used'] = True
    m.write_json(work / 'job.json', job)
    clean_job(work)
    return {'added': report['added_chats'], 'updated': report['overlapping_chats_updated_from_cloud'],
            'total': report['total_chats'], 'backup': str(backup), 'selected': len(selected), 'renamed_assets': renamed,
            'compatibility_rows_preserved': report.get('compatibility_rows_preserved', 0),
            'compatibility_values_restored': report.get('compatibility_values_restored', 0)}


def assert_no_pending(home):
    for path in (home.parent / 'Codex-chat-merge-backups').glob('*/transaction.json'):
        info = m.read_json(path)
        if Path(info.get('target', '')).resolve() == home and info.get('status') in ('installing', 'rollback_in_progress'):
            raise RuntimeError('上次导入中断。请先使用“恢复到导入前”或备份内的回滚工具：' + str(path.parent))


def rollback_latest(home, auto_close=True):
    home = validate_home(home, allow_incomplete=True)
    root = home.parent / 'Codex-chat-merge-backups'
    candidates = []
    for path in root.glob('zhiru-*/transaction.json'):
        info = m.read_json(path)
        if Path(info['target']).resolve() == home and info['status'] in ('complete', 'installing', 'rollback_in_progress'):
            candidates.append((info['status'] != 'complete', path.parent))
    if not candidates:
        raise RuntimeError('未找到这份 Codex 数据的可回滚导入。')
    ensure_closed(auto_close)
    backup = sorted(candidates)[-1][1]
    m.rollback(backup, idle_check=idle)
    return {'backup': str(backup)}


@contextlib.contextmanager
def operation_lock():
    import msvcrt
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with (CACHE.parent / 'operation.lock').open('a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('另一个 codex智入 操作正在进行。请等待完成后重试。') from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main(request):
    if request.get('action') in ('export', 'preview', 'apply', 'rollback', 'prepare_readers'):
        with operation_lock():
            return dispatch(request)
    return dispatch(request)


def dispatch(request):
    m.log = progress
    m.running_codex = lambda: [p['pid'] for p in owners()]
    m.require_idle = idle
    action = request['action']
    if action == 'discover':
        return discover()
    if action == 'owners':
        return {'owners': owners()}
    if action == 'list':
        return list_chats(Path(request['home']))
    if action == 'conversation':
        return conversation(Path(request['home']), request['id'], request.get('start', -1), request.get('tools', False))
    if action == 'export':
        return export_bundle(request['home'], request.get('output'), request.get('auto_close', True))
    if action == 'prepare_readers':
        return prepare_readers(request['home'], request.get('archive_home'), request.get('auto_close', True))
    if action == 'preview':
        return preview_import(request['bundle'], request['home'], request.get('auto_close', True))
    if action == 'apply':
        return apply_import(request['token'], request['selected'], request.get('auto_close', True))
    if action == 'rollback':
        return rollback_latest(request['home'], request.get('auto_close', True))
    if action == 'cleanup':
        clean_job(job_path(request['token']))
        return {'cleaned': True}
    if action == 'cleanup_views':
        views = CACHE.parent / 'views'
        if views.exists(): shutil.rmtree(views)
        return {'cleaned': True}
    raise ValueError('不支持的操作。')


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.read())
        emit('result', data=main(request))
    except Exception as exc:
        emit('error', message=str(exc))
        sys.exit(1)
