"""Content-aware, selective sync. All merge work happens in disposable staging."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import unquote

# The bundled interpreter runs with -I and does not add the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_merge as merge
import chat_snapshot as snapshot

PREFERENCES = '.chat-sync-preferences.json'


def fingerprint(obj):
    """Ignore migration bookkeeping, but keep messages AND tool results."""
    return merge.event_digest(obj)


def catalog(home):
    """Replay the active history chain, respecting each segment's ordinal cut."""
    db = merge.db_open(home / merge.DBS[0], True)
    try:
        title = 'title' if 'title' in merge.cols(db, 'threads') else 'id'
        rows = db.execute('SELECT id,rollout_path,' + title + ' FROM threads').fetchall()
    finally:
        db.close()
    segments = {}
    for path in merge.ordered_transcripts(home):
        with merge.open_transcript(path) as f:
            first = json.loads(f.readline())
            meta = first['payload']
            base = meta.get('history_base')
            start = base['end_ordinal_exclusive'] if base else 0
            events = []
            if base:
                parent = segments[base['thread_id']]
                if not parent['start'] <= start <= parent['end']:
                    raise RuntimeError('Invalid history segment boundary')
                events = [(n, h) for n, h in parent['events'] if n < start]
            count = 1
            for ordinal, raw in enumerate(f, 1):
                if not raw.endswith(b'\n'):
                    raise RuntimeError('Incomplete transcript tail; close Codex and retry')
                digest = fingerprint(json.loads(raw))
                if digest:
                    events.append((start + ordinal, digest))
                count += 1
            segments[merge.session_storage_id(path)] = {
                'start': start, 'end': start + count, 'events': events,
                'id': meta.get('id') or meta.get('session_id'),
            }
    result = {}
    for sid, rollout, title in rows:
        head = merge.session_storage_id(Path(rollout.replace('\\', '/')))
        segment = segments.get(head)
        if not segment or segment['id'] != sid:
            raise RuntimeError('Missing active chat transcript: ' + sid)
        result[sid] = {'title': title or sid, 'events': [h for _, h in segment['events']]}
    return result


def classify(source, destination):
    result = {}
    for sid, entry in source.items():
        if sid not in destination:
            status = 'new'
        else:
            a, b = entry['events'], destination[sid]['events']
            if a == b:
                status = 'same'
            elif len(a) > len(b) and a[:len(b)] == b:
                status = 'newer'
            elif len(b) > len(a) and b[:len(a)] == a:
                status = 'older'
            else:
                status = 'conflict'
        result[sid] = status
    return result


def parse_numbers(value, size):
    chosen = set()
    for part in re.split(r'[,，\s]+', value.strip()):
        if not part:
            continue
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', part)
        if not match:
            raise ValueError('请输入编号，如 1,3 或 2-5。')
        lo, hi = int(match[1]), int(match[2] or match[1])
        if not 1 <= lo <= hi <= size:
            raise ValueError('编号超出列表范围。')
        chosen.update(range(lo - 1, hi))
    if not chosen:
        raise ValueError('未选择任何编号。')
    return chosen


def choose(source, destination, settings, action, ask=input):
    states = classify(source, destination)
    prefs = merge.read_json(settings) if settings.exists() else {'format': 1, 'excluded_ids': []}
    if prefs.get('format') != 1 or not isinstance(prefs.get('excluded_ids'), list):
        raise RuntimeError('Unsupported chat sync preferences')
    excluded = set(prefs['excluded_ids'])
    conflicts = [sid for sid in source if states[sid] == 'conflict' and sid not in excluded]
    for sid in conflicts:
        # Terminal control characters in historical titles are not executable UI.
        title = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', source[sid]['title'])[:90]
        print('冲突，保留双方且不覆盖：' + title + ' [' + sid + ']', flush=True)
    print('相同内容跳过 %d 条；目标端内容更多，保留 %d 条；冲突跳过 %d 条。' % (
        list(states.values()).count('same'), list(states.values()).count('older'), len(conflicts)), flush=True)
    eligible = [sid for sid in source if states[sid] in ('new', 'newer')]
    while True:
        available = [sid for sid in eligible if sid not in excluded]
        print('\n%s：可同步 %d 条，长期排除 %d 条。' % (action, len(available), len(excluded)), flush=True)
        for i, sid in enumerate(available, 1):
            title = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', source[sid]['title'])[:90]
            print('%d. [%s] %s [%s]' % (i, '新聊天' if states[sid] == 'new' else '新增内容', title, sid[:8]), flush=True)
        if not available and not excluded:
            return [], states
        print('回车/all：全部可同步项；编号如 1,3 或 2-5：只选这些；0：取消。', flush=True)
        print('x 1,3：将这些聊天长期排除；reset：清除本机排除设置。', flush=True)
        answer = ask('请选择：').strip().lower()
        if answer == '0':
            return [], states
        if answer == 'reset':
            excluded.clear()
        elif answer.startswith('x '):
            try:
                excluded.update(available[i] for i in parse_numbers(answer[2:], len(available)))
            except ValueError as exc:
                print(str(exc), flush=True)
                continue
        elif answer in ('', 'all'):
            return available, states
        else:
            try:
                return [available[i] for i in sorted(parse_numbers(answer, len(available)))], states
            except ValueError as exc:
                print(str(exc), flush=True)
                continue
        merge.write_json(settings, {'format': 1, 'excluded_ids': sorted(excluded)})


def prune_global(value, omitted):
    if isinstance(value, dict):
        return {k: prune_global(v, omitted) for k, v in value.items() if k not in omitted}
    if isinstance(value, list):
        return [prune_global(v, omitted) for v in value if not isinstance(v, str) or v not in omitted]
    return None if isinstance(value, str) and value in omitted else value


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def history_selection(source, destination, home, selected, states):
    """Resolve storage dependencies separately from selectable logical chats.

    Reuse only an exact, recursively verified destination prefix. A missing or
    differing prefix must not import an unselected parent's private messages.
    """
    source_paths = {merge.session_storage_id(p): p for p in merge.ordered_transcripts(source)}
    dest_paths = {merge.session_storage_id(p): p for p in merge.ordered_transcripts(destination)}
    metadata = {sid: merge.transcript_metadata(p) for sid, p in source_paths.items()}
    owners = {sid: meta.get('id') or meta.get('session_id') for sid, meta in metadata.items()}
    mapper = merge.Mapper(str(home.parent), home)
    selected = set(selected)
    blocked, cache = {}, {}

    def prefix(storage, cut):
        key = (storage, cut)
        if key not in cache:
            digest = merge.history_prefix_hash(source_paths[storage], cut, mapper)
            try:
                compatible = (storage in dest_paths and
                              merge.history_prefix_hash(dest_paths[storage], cut, mapper) == digest)
            except RuntimeError:
                compatible = False
            cache[key] = compatible, digest
        return cache[key]

    # Removing an unsafe parent can in turn make a selected child unsafe.
    while True:
        required, rejected = {}, {}
        for logical in sorted(selected):
            seen = set()

            def visit(storage):
                if storage in seen:
                    return
                seen.add(storage)
                base = metadata[storage].get('history_base')
                if not base:
                    return
                parent, cut = base['thread_id'], base['end_ordinal_exclusive']
                if owners[parent] not in selected:
                    compatible, digest = prefix(parent, cut)
                    if not compatible:
                        rejected[logical] = {'parent_id': owners[parent], 'storage_id': parent,
                                             'parent_status': states.get(owners[parent], 'missing')}
                        return
                    prior = required.get(parent)
                    if prior is None or cut > prior['cut']:
                        required[parent] = {'storage_id': parent, 'cut': cut, 'sha256': digest}
                visit(parent)

            for storage in source_paths:
                if owners[storage] == logical:
                    visit(storage)
        if not rejected:
            return selected, list(required.values()), blocked
        blocked.update(rejected)
        selected -= rejected.keys()


def backup_history_selection(source, destination, home, selected, states):
    """Backup can replace selected IDs, while preserved local forks keep ancestry."""
    current_paths = {merge.session_storage_id(p): p for p in merge.ordered_transcripts(destination)}
    source_paths = {merge.session_storage_id(p): p for p in merge.ordered_transcripts(source)}
    owners = {sid: (merge.transcript_metadata(p).get('id') or merge.transcript_metadata(p).get('session_id'))
              for sid, p in current_paths.items()}
    with contextlib.closing(merge.db_open(destination / merge.DBS[0], True)) as db:
        local_ids = {row[0] for row in db.execute('SELECT id FROM threads')}
    selected, blocked = set(selected), {}
    mapper = merge.Mapper(str(home.parent), home)
    while True:
        safe, external, incoming_blocked = history_selection(source, destination, home, selected, states)
        blocked.update(incoming_blocked)
        rejected = {}
        requirements = merge.preserved_history_requirements(destination, safe & local_ids, home)
        for requirement in requirements:
            owner = owners.get(requirement['storage_id'])
            if owner not in safe:
                continue
            path = source_paths.get(requirement['storage_id'])
            try:
                reusable = path is not None and merge.history_prefix_hash(path, requirement['cut'], mapper) == requirement['sha256']
            except RuntimeError:
                reusable = False
            if not reusable:
                rejected[owner] = {'parent_id': owner, 'reason': '本机保留的分支依赖此聊天的不同旧历史，暂不能单独替换父聊天。'}
        if not rejected:
            return safe, external, blocked
        blocked.update(rejected)
        selected = safe - rejected.keys()


def resolve_history_selection(source, destination, home, selected, states, entries, ask=input):
    """Require explicit consent to add parents or to proceed with fewer chats."""
    requested = set(selected)
    prefs = merge.read_json(home / PREFERENCES) if (home / PREFERENCES).exists() else {}
    excluded = set(prefs.get('excluded_ids', []))
    while True:
        safe, required, blocked = history_selection(source, destination, home, requested, states)
        if not blocked:
            return [sid for sid in selected if sid in safe] + sorted(safe - set(selected)), required, {}
        labels = {'same': '内容相同', 'older': '目标端内容更多', 'conflict': '两边历史有冲突',
                  'new': '尚未选中', 'newer': '尚未选中', 'missing': '没有可选的父聊天'}
        print('\n以下聊天的共享历史暂时无法安全同步：', flush=True)
        for sid, info in blocked.items():
            title = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', entries[sid]['title'])[:90]
            reason = '已长期排除' if info['parent_id'] in excluded else labels[info['parent_status']]
            print('  %s [%s]：依赖父聊天 %s（%s）；目标端缺少所需历史分段，或该段前缀不一致。' % (
                title, sid, info['parent_id'], reason), flush=True)
        addable = {info['parent_id'] for info in blocked.values()
                   if states.get(info['parent_id']) in ('new', 'newer') and info['parent_id'] not in excluded
                   and info['parent_id'] not in requested}
        if not safe and not addable:
            print('没有可安全同步的已选聊天；不修改本机或云端。请保留双方副本，不能强制覆盖父聊天。', flush=True)
            return [], [], blocked
        print('可继续同步 %d 条。s：仅同步这些安全项；0/回车：取消本次操作。' % len(safe), flush=True)
        if addable:
            print('a：同时加入上面列出的、未被排除且可安全同步的父聊天。', flush=True)
        answer = ask('请选择：').strip().lower()
        if answer == 'a' and addable:
            requested.update(addable)
        elif answer == 's':
            return [sid for sid in selected if sid in safe] + sorted(safe - set(selected)), required, blocked
        elif answer in ('', '0'):
            return [], [], blocked
        else:
            print('输入无效；请使用上面列出的选项。', flush=True)


def restrict_package(work, selected, external_history=()):
    """Prune ONLY the isolated incoming package, never the source or live home."""
    merge.WORK_ROOT = work
    manifest = merge.verify_package()
    selected = set(selected)
    all_ids = set(manifest['original_thread_settings'])
    if not selected or not selected <= all_ids:
        raise RuntimeError('Empty or invalid chat selection')
    data = work / 'incoming'
    sessions = {p: sid for p, sid in manifest['session_ids'].items() if sid in selected}
    storage_ids = {merge.session_storage_id(Path(p)) for p in sessions}
    external = {entry['storage_id']: entry for entry in external_history}
    references = []
    for relative in sessions:
        with (data / relative).open('rb') as f:
            for n, raw in enumerate(f):
                obj = json.loads(raw)
                if n == 0:
                    base = obj.get('payload', {}).get('history_base', {})
                    parent = base.get('thread_id')
                    if parent and parent not in storage_ids:
                        if parent not in external or base['end_ordinal_exclusive'] > external[parent]['cut']:
                            raise RuntimeError('Unresolved shared history; compare and select again: ' + parent)
                references.extend(strings(obj))
    deferred_edges = []
    for name, tables, ids in ((merge.DBS[0], merge.STATE_TABLES, selected),
                              (merge.DBS[1], merge.HISTORY_TABLES, selected | storage_ids)):
        db = sqlite3.connect(data / name)
        try:
            db.execute('PRAGMA foreign_keys=OFF')
            for table in reversed(tables):
                columns = merge.cols(db, table)
                key = ('id' if table == 'threads' else 'child_thread_id' if table == 'thread_spawn_edges'
                       else 'thread_id' if 'thread_id' in columns else None)
                if key:
                    for row in db.execute('SELECT rowid,* FROM ' + merge.quote(table)).fetchall():
                        record = dict(zip(columns, row[1:]))
                        if record[key] not in ids:
                            db.execute('DELETE FROM ' + merge.quote(table) + ' WHERE rowid=?', (row[0],))
                        elif table == 'thread_spawn_edges' and record.get('parent_thread_id') not in selected:
                            # Reattach to the preserved destination parent during
                            # merging, without importing its unselected messages.
                            deferred_edges.append(record)
                            db.execute('DELETE FROM ' + merge.quote(table) + ' WHERE rowid=?', (row[0],))
                        else:
                            references.extend(v for v in row[1:] if isinstance(v, str))
            db.commit()
            # Fail closed for a selected fork with a missing FK parent. Do not
            # silently upload an unselected parent's messages to satisfy it.
            merge.check_db(db)
            db.execute('VACUUM')
        finally:
            db.close()
    # Include explicitly owned assets, plus generic attachments referenced by
    # selected messages/history/attachment rows. No unrelated registry excerpts.
    reference_text = '\n'.join(unquote(s).replace('\\\\', '/').replace('\\', '/').casefold() for s in references)
    kept_assets = set()
    compatibility_records = []
    for directory in merge.DIRS:
        for p in list(merge.safe_files(data / directory)):
            rel = p.relative_to(data)
            if directory in merge.DIRS[:2]:
                keep = rel.as_posix() in sessions
            elif rel == merge.PASTED_TEXT_REGISTRY:
                continue
            elif rel.parent == merge.HISTORY_COMPAT_DIR:
                compatibility_records.extend(record for record in merge.load_history_compat(p)
                                             if record['thread_id'] in selected | storage_ids)
                keep = False
            else:
                owner = merge.asset_thread_id(rel)
                keep = owner in selected or rel.as_posix().casefold() in reference_text
                if keep:
                    kept_assets.add(rel.as_posix().casefold())
            if not keep:
                p.unlink()
    merge.save_history_compat(data, compatibility_records)
    registry_path = data / merge.PASTED_TEXT_REGISTRY
    if registry_path.exists():
        registry = merge.read_attachment_registry(registry_path)
        def included(path):
            normalized = unquote(path).replace('\\', '/').casefold()
            return any(normalized.endswith('/' + rel) or normalized == rel for rel in kept_assets)
        merge.write_json(registry_path, {
            'attachmentPaths': [p for p in registry['attachmentPaths'] if included(p)],
            'pendingRemovalPaths': [],
            'textExcerptsByPath': {p: t for p, t in registry['textExcerptsByPath'].items() if included(p)},
        })
    for name in ('session_index.jsonl', 'history.jsonl'):
        path = data / name
        if path.exists():
            lines = []
            for raw in path.read_bytes().splitlines():
                if raw.strip():
                    obj = json.loads(raw)
                    if obj.get('id', obj.get('session_id')) in selected:
                        lines.append(raw)
            path.write_bytes(b''.join(raw + b'\n' for raw in lines))
    global_path = data / '.codex-global-state.json'
    merge.write_json(global_path, prune_global(merge.read_json(global_path), all_ids - selected))
    manifest['original_thread_settings'] = {k: v for k, v in manifest['original_thread_settings'].items() if k in selected}
    manifest['line_counts'] = {k: v for k, v in manifest['line_counts'].items() if k in selected}
    manifest.update(session_ids=sessions, source_thread_count=len(selected), session_file_count=len(sessions),
                    history_segment_count=len(storage_ids), selected_ids=sorted(selected),
                    deferred_spawn_edges=deferred_edges, external_history=list(external_history))
    manifest['files'] = [{'path': p.relative_to(data).as_posix(), 'size': p.stat().st_size,
                          'sha256': merge.sha(p)} for p in merge.safe_files(data)]
    manifest['original_source_files'] = [f for f in manifest['original_source_files']
                                         if f.get('prepared_path', f['path']) in {x['path'] for x in manifest['files']}]
    merge.write_json(work / 'package.json', manifest)
    return merge.verify_package()


def capture_local(home, work):
    merge.require_idle()
    before = merge.tree_signature(home, merge.managed_names(home))
    data = work / 'local-copy'
    merge.take_current(home, data)
    if before != merge.tree_signature(home, merge.managed_names(home)):
        raise RuntimeError('Local chats changed while copying; close Codex and retry')
    scrub = snapshot.Scrubber(home)
    for name in merge.DBS:
        snapshot.transform_db(data / name, scrub, sanitize=True)
    for p in merge.safe_files(data):
        if p.suffix.lower() in ('.jsonl', '.json', '.html', '.md', '.txt', '.svg', '.bak'):
            p.write_text(scrub(p.read_text(encoding='utf-8-sig')), encoding='utf-8')
    if not (data / '.codex-global-state.json').exists():
        merge.write_json(data / '.codex-global-state.json', {})
    merge.require_idle()
    return data, before


def prepare_package(source, home, work):
    work.mkdir(parents=True)
    merge.WORK_ROOT = work
    merge.prepare(source, home)
    return work / 'incoming'


def run(action, home, cloud, work, ask=input):
    local, signature = capture_local(home, work)
    cloud_data = prepare_package(cloud, home, work / 'cloud-package')
    local_catalog, cloud_catalog = catalog(local), catalog(cloud_data)
    upload = action == 'upload'
    selected, states = choose(local_catalog if upload else cloud_catalog,
                              cloud_catalog if upload else local_catalog,
                              home / PREFERENCES, '上传' if upload else '更新本机', ask)
    external_history, blocked = [], {}
    if selected:
        selected, external_history, blocked = resolve_history_selection(
            local if upload else cloud_data, cloud_data if upload else local,
            home, selected, states, local_catalog if upload else cloud_catalog, ask)
    result = {'action': action, 'selected_count': len(selected), 'selected_ids': selected,
              'blocked_history': blocked, 'reused_history_segments': len(external_history),
              'counts': {status: list(states.values()).count(status)
                         for status in ('new', 'newer', 'same', 'older', 'conflict')}}
    if not selected:
        merge.write_json(work / 'selection-result.json', result)
        print('没有需要同步的已选聊天；本机和云端记录均未更改。', flush=True)
        return result
    merge.require_idle()
    if signature != merge.tree_signature(home, merge.managed_names(home)):
        raise RuntimeError('Local chats changed after selection; close Codex and retry')
    if upload:
        package_work = work / 'selected-package'
        prepare_package(local, home, package_work)
        manifest = restrict_package(package_work, selected, external_history)
        # Reverse the normal merge direction: cloud is the baseline. Only the
        # selected newer LOCAL chats replace it, retaining every other cloud chat.
        stage = work / 'merged-cloud'
        merge.build_merge(cloud_data, stage, home, manifest)
        result['source'] = str(stage)
    else:
        package_work = work / 'cloud-package'
        manifest = restrict_package(package_work, selected, external_history)
        manifest['expected_target_signature'] = signature
        merge.write_json(package_work / 'package.json', manifest)
        result['package_work'] = str(package_work)
    merge.write_json(work / 'selection-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('upload', 'update'))
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--cloud', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    run(args.action, args.home.resolve(), args.cloud.resolve(), args.work.resolve())


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt, EOFError) as exc:
        print('ERROR: ' + str(exc), flush=True)
        sys.exit(1)
