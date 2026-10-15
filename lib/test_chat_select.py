"""Isolated fixtures only: no live profile, network, or GitHub upload."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_select as s

m = s.merge
A, B, C, SEG = [str(n) * 8 + '-' + str(n) * 4 + '-' + str(n) * 4 + '-' + str(n) * 4 + '-' + str(n) * 12
                for n in range(1, 5)]


def make_home(root, chats):
    home = root / '.codex'
    home.mkdir(parents=True)
    for name in m.DIRS:
        (home / name).mkdir()
    (home / 'auth.json').write_text('{"test":"never-upload-this-file"}', encoding='utf-8')
    (home / 'config.toml').write_text('model_provider="openai"', encoding='utf-8')
    db = sqlite3.connect(home / m.DBS[0])
    db.executescript('''
      CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
      CREATE TABLE project_roots(id TEXT PRIMARY KEY, project_id TEXT);
      CREATE TABLE thread_sections(id TEXT PRIMARY KEY, thread_id TEXT);
      CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,history_mode TEXT,
                          model_provider TEXT,model TEXT,title TEXT,archived INTEGER);
      CREATE TABLE thread_dynamic_tools(id TEXT PRIMARY KEY,thread_id TEXT);
      CREATE TABLE thread_attachments(id TEXT PRIMARY KEY,thread_id TEXT,path TEXT);
      CREATE TABLE thread_spawn_edges(id TEXT PRIMARY KEY,parent_thread_id TEXT REFERENCES threads(id) ON DELETE CASCADE,
                                     child_thread_id TEXT REFERENCES threads(id) ON DELETE CASCADE);
      CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY, checksum TEXT);
      INSERT INTO _sqlx_migrations VALUES(1,'schema-keep');
    ''')
    hist = sqlite3.connect(home / m.DBS[1])
    hist.executescript('''
      CREATE TABLE thread_turns(id TEXT PRIMARY KEY,thread_id TEXT,rollout_ordinal INTEGER,
          rollout_end_ordinal INTEGER,rollout_byte_offset INTEGER,rollout_end_byte_offset INTEGER);
      CREATE TABLE thread_items(id TEXT PRIMARY KEY,thread_id TEXT,item_json TEXT);
      CREATE TABLE thread_realtime_items(id TEXT PRIMARY KEY,thread_id TEXT,item_json TEXT);
      CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_ordinal INTEGER,next_rollout_byte_offset INTEGER);
    ''')
    for sid, texts in chats.items():
        path = home / 'sessions' / ('rollout-' + sid + '.jsonl')
        rows = [{'type': 'session_meta', 'payload': {'id': sid, 'history_mode': 'legacy', 'model_provider': 'openai'}}]
        for i, text in enumerate(texts):
            rows.append({'timestamp': '2026-01-01T00:00:00Z', 'type': 'response_item',
                         'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}})
            hist.execute('INSERT INTO thread_items VALUES(?,?,?)', (sid + ':' + str(i), sid, json.dumps(rows[-1]['payload'])))
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)', (sid, str(path), str(root), 'legacy', 'openai', 'gpt-test', 'Fixture ' + sid[:8], 0))
        if texts:
            hist.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)', (sid, sid, 1, len(texts), 0, 0))
        hist.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)', (sid, len(rows), 0))
    db.commit()
    db.close()
    hist.commit()
    hist.close()
    m.write_json(home / '.codex-global-state.json', {'pinned-thread-ids': list(chats)})
    (home / 'session_index.jsonl').write_text(''.join(json.dumps({'id': sid, 'title': 'Fixture'}) + '\n' for sid in chats), encoding='utf-8')
    return home


def fork_history(home, child, parent, cut):
    path = home / 'sessions' / ('rollout-' + child + '.jsonl')
    parent_path = home / 'sessions' / ('rollout-' + parent + '.jsonl')
    parent_lines = parent_path.read_bytes().splitlines(keepends=True)
    parent_base = json.loads(parent_lines[0])['payload'].get('history_base', {}).get('end_ordinal_exclusive', 0)
    rows = path.read_bytes().splitlines(keepends=True)
    first = json.loads(rows[0])
    first['payload']['history_base'] = {'thread_id': parent, 'end_ordinal_exclusive': cut,
                                      'end_byte_offset': sum(map(len, parent_lines[:cut - parent_base]))}
    path.write_bytes((json.dumps(first) + '\n').encode() + b''.join(rows[1:]))
    db = sqlite3.connect(home / m.DBS[1])
    db.execute('UPDATE thread_turns SET rollout_ordinal=rollout_ordinal+?,rollout_end_ordinal=rollout_end_ordinal+? WHERE thread_id=?', (cut, cut, child))
    db.execute('UPDATE thread_history_projection_state SET next_rollout_ordinal=next_rollout_ordinal+? WHERE thread_id=?', (cut, child))
    db.commit()
    db.close()


def compact_transcript(path):
    path.write_bytes(b''.join((json.dumps(json.loads(line), separators=(',', ':')) + '\n').encode()
                             for line in path.read_bytes().splitlines()))


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.temp.cleanup()

    def test_compare_content_not_timestamp(self):
        a = {'same': ['1'], 'newer': ['1', '2'], 'older': ['1'], 'fork': ['1', 'a'], 'new': []}
        b = {'same': ['1'], 'newer': ['1'], 'older': ['1', '2'], 'fork': ['1', 'b']}
        wrap = lambda d: {sid: {'events': events} for sid, events in d.items()}
        self.assertEqual(s.classify(wrap(a), wrap(b)), {'same': 'same', 'newer': 'newer', 'older': 'older', 'fork': 'conflict', 'new': 'new'})

    def test_fingerprint_ignores_timestamps_not_tools(self):
        obj = {'type': 'response_item', 'timestamp': 'old', 'payload': {'type': 'function_call_output', 'output': 'first'}}
        self.assertEqual(s.fingerprint(obj), s.fingerprint({**obj, 'timestamp': 'new'}))
        self.assertNotEqual(s.fingerprint(obj), s.fingerprint({**obj, 'payload': {'output': 'second'}}))
        self.assertIsNone(s.fingerprint({'type': 'session_meta', 'payload': {}}))
        self.assertIsNone(s.fingerprint({'type': 'event_msg', 'payload': {'type': 'token_count'}}))

    def test_selector_ranges_and_errors(self):
        self.assertEqual(s.parse_numbers('1，3-5 3', 5), {0, 2, 3, 4})
        for value in ('0', '6', '3-1', '-1', 'a', ''):
            with self.assertRaises(ValueError):
                s.parse_numbers(value, 5)

    def test_persistent_exclusion_reset_and_cancel(self):
        entries = {A: {'events': ['1'], 'title': 'one'}, B: {'events': ['2'], 'title': 'two'}}
        settings = self.root / 'settings.json'
        answers = iter(['x 1', ''])
        chosen, _ = s.choose(entries, {}, settings, 'test', lambda _: next(answers))
        self.assertEqual(chosen, [B])
        self.assertEqual(m.read_json(settings)['excluded_ids'], [A])
        self.assertEqual(s.choose(entries, {}, settings, 'test', lambda _: '')[0], [B])
        answers = iter(['reset', '1'])
        self.assertEqual(s.choose(entries, {}, settings, 'test', lambda _: next(answers))[0], [A])
        self.assertEqual(s.choose(entries, {}, settings, 'test', lambda _: '0')[0], [])

    def test_conflicts_and_older_versions_not_selectable(self):
        source = {A: {'title': 'fork', 'events': ['old', 'branch']}, B: {'title': 'older', 'events': ['old']}}
        dest = {A: {'events': ['old', 'other']}, B: {'events': ['old', 'latest']}}
        def should_not_prompt(_):
            self.fail('No eligible changes should not prompt')
        self.assertEqual(s.choose(source, dest, self.root / 'settings', 'test', should_not_prompt)[0], [])

    def test_segment_chain_uses_cut_and_active_head(self):
        home = make_home(self.root / 'local', {A: ['one', 'discarded-tail']})
        old = home / 'sessions' / ('rollout-' + A + '.jsonl')
        parent = home / 'sessions' / ('rollout-' + SEG + '.jsonl')
        old.rename(parent)
        old.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': A, 'history_base': {'thread_id': SEG, 'end_ordinal_exclusive': 2}}}) + '\n' +
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'two'}]}}) + '\n', encoding='utf-8')
        other = make_home(self.root / 'other', {A: ['one', 'two']})
        self.assertEqual(s.catalog(home)[A]['events'], s.catalog(other)[A]['events'])

    def test_mapped_image_paths_do_not_create_false_conflict(self):
        cloud = make_home(self.root / 'old', {A: ['one']})
        local = make_home(self.root / 'local', {A: ['one']})
        for home in (cloud, local):
            path = home / 'sessions' / ('rollout-' + A + '.jsonl')
            obj = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                   'content': [{'type': 'input_image', 'image_url': str(home / 'attachments' / 'image.png')}]}}
            with path.open('a', encoding='utf-8') as out:
                out.write(json.dumps(obj) + '\n')
        incoming = s.prepare_package(cloud, local, self.root / 'work')
        self.assertEqual(s.classify(s.catalog(incoming), s.catalog(local))[A], 'same')

    def run_sync(self, action, local, cloud, answer=''):
        work = self.root / ('work-' + action)
        work.mkdir()
        answers = iter(answer) if isinstance(answer, list) else None
        with patch.object(m, 'require_idle', return_value=None):
            result = s.run(action, local, cloud, work, lambda _: next(answers) if answers else answer)
        return result, work

    def test_shared_history_reuses_newer_cloud_parent_without_uploading_it(self):
        local = make_home(self.root / 'local', {A: ['one'], B: ['fork message']})
        cloud = make_home(self.root / 'cloud', {A: ['one', 'cloud latest']})
        fork_history(local, B, A, 2)
        compact_transcript(cloud / 'sessions' / ('rollout-' + A + '.jsonl'))
        before = (cloud / 'sessions' / ('rollout-' + A + '.jsonl')).read_bytes()
        result, work = self.run_sync('upload', local, cloud)
        merged = Path(result['source'])
        self.assertEqual(result['selected_ids'], [B])
        self.assertEqual(result['reused_history_segments'], 1)
        self.assertEqual((merged / 'sessions' / ('rollout-' + A + '.jsonl')).read_bytes(), before)
        self.assertEqual(s.catalog(merged)[B]['events'], s.catalog(local)[B]['events'])
        package = m.read_json(work / 'selected-package' / 'package.json')
        self.assertEqual(set(package['original_thread_settings']), {B})
        self.assertNotIn(A, package['session_ids'].values())
        m.assert_current_rows_preserved(work / 'cloud-package' / 'incoming', merged, {B}, {B})

    def test_shared_history_update_repairs_offsets_and_rolls_back(self):
        local = make_home(self.root / 'local', {A: ['one', 'local latest']})
        cloud = make_home(self.root / 'cloud', {A: ['one'], B: ['fork message']})
        fork_history(cloud, B, A, 2)
        compact_transcript(local / 'sessions' / ('rollout-' + A + '.jsonl'))
        original = {p.relative_to(local): p.read_bytes() for p in m.safe_files(local)}
        result, _ = self.run_sync('update', local, cloud)
        m.WORK_ROOT = Path(result['package_work'])
        with patch.object(m, 'running_codex', return_value=[]):
            m.restore()
            child = local / 'sessions' / ('rollout-' + B + '.jsonl')
            rows = child.read_bytes().splitlines(keepends=True)
            parent_prefix = sum(map(len, original[Path('sessions') / ('rollout-' + A + '.jsonl')].splitlines(keepends=True)[:2]))
            self.assertEqual(json.loads(rows[0])['payload']['history_base']['end_byte_offset'], parent_prefix)
            hist = m.db_open(local / m.DBS[1], True)
            try:
                self.assertEqual(hist.execute('SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns WHERE thread_id=?', (B,)).fetchone(), (len(rows[0]), len(b''.join(rows))))
                self.assertEqual(hist.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?', (B,)).fetchone()[0], child.stat().st_size)
            finally:
                hist.close()
            backup = next((local.parent / 'Codex-chat-merge-backups').iterdir())
            m.rollback(backup, idle_check=lambda: None)
        for path, data in original.items():
            self.assertEqual((local / path).read_bytes(), data)

    def test_shared_history_unchanged_parent_not_required_in_menu(self):
        local = make_home(self.root / 'local', {A: ['same'], B: ['fork']})
        cloud = make_home(self.root / 'cloud', {A: ['same']})
        fork_history(local, B, A, 2)
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [B])
        self.assertEqual(result['counts']['same'], 1)
        self.assertFalse(result['blocked_history'])

    def test_conflicting_parent_tail_does_not_block_matching_shared_prefix(self):
        local = make_home(self.root / 'local', {A: ['shared', 'local private tail'], B: ['fork']})
        cloud = make_home(self.root / 'cloud', {A: ['shared', 'cloud tail']})
        fork_history(local, B, A, 2)
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [B])
        self.assertEqual(result['counts']['conflict'], 1)
        merged = Path(result['source'])
        self.assertFalse(any('local private tail' in p.read_text() for p in (merged / 'sessions').glob('*')))

    def test_conflicting_shared_prefix_requires_consent_to_skip_child(self):
        local = make_home(self.root / 'local', {A: ['local parent'], B: ['fork'], C: ['safe']})
        cloud = make_home(self.root / 'cloud', {A: ['cloud parent']})
        fork_history(local, B, A, 2)
        result, _ = self.run_sync('upload', local, cloud, ['', 's'])
        self.assertEqual(result['selected_ids'], [C])
        self.assertEqual(result['blocked_history'][B]['parent_id'], A)
        self.assertEqual(set(s.catalog(Path(result['source']))), {A, C})

    def test_shared_history_skip_prompt_defaults_to_cancellation(self):
        local = make_home(self.root / 'local', {A: ['local parent'], B: ['fork'], C: ['safe']})
        cloud = make_home(self.root / 'cloud', {A: ['cloud parent']})
        fork_history(local, B, A, 2)
        result, work = self.run_sync('upload', local, cloud, ['', ''])
        self.assertEqual(result['selected_count'], 0)
        self.assertFalse((work / 'selected-package').exists())

    def test_shared_history_can_explicitly_add_safe_parent(self):
        local = make_home(self.root / 'local', {A: ['parent'], B: ['fork']})
        cloud = make_home(self.root / 'cloud', {})
        fork_history(local, B, A, 2)
        result, _ = self.run_sync('upload', local, cloud, ['2', 'a'])
        self.assertEqual(set(result['selected_ids']), {A, B})
        self.assertEqual(s.catalog(Path(result['source'])), s.catalog(local))

    def test_excluded_parent_is_not_implicitly_uploaded_for_fork(self):
        local = make_home(self.root / 'local', {A: ['private parent'], B: ['fork'], C: ['safe']})
        cloud = make_home(self.root / 'cloud', {})
        fork_history(local, B, A, 2)
        m.write_json(local / s.PREFERENCES, {'format': 1, 'excluded_ids': [A]})
        result, _ = self.run_sync('upload', local, cloud, ['', 's'])
        self.assertEqual(result['selected_ids'], [C])
        self.assertEqual(set(s.catalog(Path(result['source']))), {C})
        self.assertEqual(m.read_json(local / s.PREFERENCES)['excluded_ids'], [A])

    def test_excluded_parent_existing_prefix_may_be_reused_without_upload(self):
        local = make_home(self.root / 'local', {A: ['same', 'private tail'], B: ['fork']})
        cloud = make_home(self.root / 'cloud', {A: ['same']})
        fork_history(local, B, A, 2)
        m.write_json(local / s.PREFERENCES, {'format': 1, 'excluded_ids': [A]})
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [B])
        self.assertEqual(s.catalog(Path(result['source']))[A]['events'], s.catalog(cloud)[A]['events'])

    def test_recursive_shared_segments_are_checked_including_ancestors(self):
        local = make_home(self.root / 'local', {A: ['root'], SEG: ['middle'], B: ['fork']})
        cloud = make_home(self.root / 'cloud', {A: ['root'], SEG: ['middle', 'newer']})
        for home in (local, cloud):
            fork_history(home, SEG, A, 2)
        fork_history(local, B, SEG, 4)
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [B])
        self.assertEqual(result['reused_history_segments'], 2)
        self.assertEqual(s.catalog(Path(result['source']))[B]['events'], s.catalog(local)[B]['events'])

    def test_shared_history_is_rechecked_before_merge(self):
        local = make_home(self.root / 'local', {A: ['same']})
        cloud = make_home(self.root / 'cloud', {A: ['same'], B: ['fork']})
        fork_history(cloud, B, A, 2)
        result, _ = self.run_sync('update', local, cloud)
        m.WORK_ROOT = Path(result['package_work'])
        manifest = m.verify_package()
        path = local / 'sessions' / ('rollout-' + A + '.jsonl')
        path.write_bytes(path.read_bytes().replace(b'same', b'changed'))
        with self.assertRaisesRegex(RuntimeError, 'shared history changed'):
            m.build_merge(local, self.root / 'must-not-exist', local, manifest)
        self.assertFalse((self.root / 'must-not-exist').exists())

    def test_shared_history_only_blocked_selection_is_noop(self):
        local = make_home(self.root / 'local', {A: ['local parent'], B: ['fork']})
        cloud = make_home(self.root / 'cloud', {A: ['cloud parent']})
        fork_history(local, B, A, 2)
        result, work = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_count'], 0)
        self.assertEqual(set(result['blocked_history']), {B})
        self.assertFalse((work / 'selected-package').exists())

    def test_parent_append_preserves_unselected_fork_history(self):
        local = make_home(self.root / 'local', {A: ['same', 'new']})
        cloud = make_home(self.root / 'cloud', {A: ['same'], B: ['preserved fork']})
        fork_history(cloud, B, A, 2)
        before = s.catalog(cloud)[B]['events']
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [A])
        self.assertEqual(s.catalog(Path(result['source']))[B]['events'], before)

    def test_merge_rejects_equal_size_change_to_preserved_fork_ancestry(self):
        local = make_home(self.root / 'local', {A: ['xxxx']})
        cloud = make_home(self.root / 'cloud', {A: ['same'], B: ['preserved fork']})
        fork_history(cloud, B, A, 2)
        work = self.root / 'package'
        s.prepare_package(local, cloud, work)
        manifest = s.restrict_package(work, [A])
        before = {p.relative_to(cloud): p.read_bytes() for p in m.safe_files(cloud)}
        with self.assertRaisesRegex(RuntimeError, 'shared history changed'):
            m.build_merge(cloud, self.root / 'rejected-stage', cloud, manifest)
        for path, data in before.items():
            self.assertEqual((cloud / path).read_bytes(), data)

    def test_selective_upload_retains_cloud_and_omits_unselected_local(self):
        local = make_home(self.root / 'local', {A: ['old', 'latest'], B: ['private unselected'], C: ['local new']})
        cloud = make_home(self.root / 'cloud', {A: ['old'], B: ['cloud retained']})
        original = m.sha(local / m.DBS[0])
        result, work = self.run_sync('upload', local, cloud, '1')
        merged = Path(result['source'])
        self.assertEqual(result['selected_ids'], [A])
        after = s.catalog(merged)
        self.assertEqual(set(after), {A, B})
        self.assertEqual(after[A]['events'], s.catalog(local)[A]['events'])
        self.assertEqual(after[B]['events'], s.catalog(cloud)[B]['events'])
        self.assertFalse((merged / 'auth.json').exists())
        self.assertFalse((merged / 'config.toml').exists())
        self.assertEqual(m.sha(local / m.DBS[0]), original)
        self.assertFalse(any('private unselected' in p.read_text(encoding='utf-8') for p in (merged / 'sessions').glob('*')))
        # Round-trip the exact artifact that would be uploaded, without a network.
        env = work / 'environment.json'
        m.write_json(env, {'test': True})
        s.snapshot.backup(merged, work / 'snapshot', env, allow_live=True)
        s.snapshot.extract(work / 'snapshot', work / 'roundtrip')
        self.assertEqual(s.catalog(work / 'roundtrip' / 'data'), after)

    def test_update_keeps_local_newer_and_rehearses_only_cloud_additions(self):
        local = make_home(self.root / 'local', {A: ['old', 'local latest'], B: ['same']})
        cloud = make_home(self.root / 'cloud', {A: ['old'], B: ['same'], C: ['new cloud']})
        result, _ = self.run_sync('update', local, cloud)
        self.assertEqual(result['selected_ids'], [C])
        m.WORK_ROOT = Path(result['package_work'])
        run = m.restore(rehearsal=True)
        merged = run / 'merged'
        self.assertEqual(set(s.catalog(merged)), {A, B, C})
        self.assertEqual((merged / 'sessions' / ('rollout-' + A + '.jsonl')).read_bytes(),
                         (local / 'sessions' / ('rollout-' + A + '.jsonl')).read_bytes())
        before = m.sha(local / m.DBS[0])
        with patch.object(m, 'running_codex', return_value=[]):
            m.restore()
            backups = list((local.parent / 'Codex-chat-merge-backups').glob('*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(set(s.catalog(local)), {A, B, C})
            m.rollback(backups[0], idle_check=lambda: None)
        self.assertEqual(m.sha(local / m.DBS[0]), before)
        self.assertEqual(set(s.catalog(local)), {A, B})

    def test_all_same_is_noop_without_package_install(self):
        local = make_home(self.root / 'local', {A: ['same']})
        cloud = make_home(self.root / 'cloud', {A: ['same']})
        for action in ('upload', 'update'):
            result, _ = self.run_sync(action, local, cloud)
            self.assertEqual(result['selected_count'], 0)
            self.assertNotIn('source', result)
            self.assertNotIn('package_work', result)

    def test_changed_target_after_selection_aborts_install(self):
        local = make_home(self.root / 'local', {A: ['old']})
        cloud = make_home(self.root / 'cloud', {A: ['old', 'cloud newer']})
        result, _ = self.run_sync('update', local, cloud)
        with (local / 'sessions' / ('rollout-' + A + '.jsonl')).open('a', encoding='utf-8') as out:
            out.write('{}\n')
        m.WORK_ROOT = Path(result['package_work'])
        with self.assertRaisesRegex(RuntimeError, 'changed since selection'):
            m.restore(rehearsal=True)
        self.assertFalse((local.parent / 'Codex-chat-merge-backups').exists())

    def test_only_selected_attachments_and_registry_excerpts_survive(self):
        local = make_home(self.root / 'local', {A: ['see attachments/one/a.txt'], B: ['see attachments/two/b.txt']})
        for rel in ('one/a.txt', 'two/b.txt'):
            p = local / 'attachments' / rel
            p.parent.mkdir()
            p.write_text('attachment ' + rel, encoding='utf-8')
        paths = [str(local / 'attachments' / rel) for rel in ('one/a.txt', 'two/b.txt')]
        m.write_json(local / m.PASTED_TEXT_REGISTRY, {'attachmentPaths': paths, 'pendingRemovalPaths': [],
                                                   'textExcerptsByPath': dict(zip(paths, ('keep excerpt', 'private excerpt')))})
        work = self.root / 'package'
        s.prepare_package(local, self.root / 'target' / '.codex', work)
        s.restrict_package(work, [A])
        incoming = work / 'incoming'
        self.assertTrue((incoming / 'attachments/one/a.txt').exists())
        self.assertFalse((incoming / 'attachments/two/b.txt').exists())
        registry = m.read_json(incoming / m.PASTED_TEXT_REGISTRY)
        self.assertEqual(list(registry['textExcerptsByPath'].values()), ['keep excerpt'])
        self.assertEqual(m.read_json(incoming / '.codex-global-state.json')['pinned-thread-ids'], [A])
        self.assertNotIn(B, (incoming / 'session_index.jsonl').read_text())

    def test_parent_update_does_not_delete_preserved_child_edge(self):
        local = make_home(self.root / 'local', {A: ['old', 'new']})
        cloud = make_home(self.root / 'cloud', {A: ['old'], B: ['child']})
        db = sqlite3.connect(cloud / m.DBS[0])
        db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('edge', A, B))
        db.commit()
        db.close()
        result, _ = self.run_sync('upload', local, cloud)
        db = m.db_open(Path(result['source']) / m.DBS[0], True)
        try:
            self.assertEqual(db.execute('SELECT * FROM thread_spawn_edges').fetchall(), [('edge', A, B)])
            m.check_db(db)
        finally:
            db.close()

    def test_running_guard_stops_before_copy_or_preferences(self):
        local = make_home(self.root / 'local', {A: ['one']})
        work = self.root / 'work'
        work.mkdir()
        with patch.object(m, 'require_idle', side_effect=RuntimeError('Codex running')):
            with self.assertRaisesRegex(RuntimeError, 'Codex running'):
                s.capture_local(local, work)
        self.assertFalse((work / 'local-copy').exists())

    def test_new_child_can_reference_unchanged_cloud_parent(self):
        local = make_home(self.root / 'local', {A: ['same'], B: ['new child']})
        cloud = make_home(self.root / 'cloud', {A: ['same']})
        db = sqlite3.connect(local / m.DBS[0])
        db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('edge', A, B))
        db.commit()
        db.close()
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [B])
        db = m.db_open(Path(result['source']) / m.DBS[0], True)
        try:
            self.assertEqual(db.execute('SELECT * FROM thread_spawn_edges').fetchall(), [('edge', A, B)])
            m.check_db(db)
        finally:
            db.close()

    def test_new_child_with_unselected_missing_parent_fails_safely(self):
        local = make_home(self.root / 'local', {A: ['parent'], B: ['child']})
        cloud = make_home(self.root / 'cloud', {C: ['unrelated']})
        db = sqlite3.connect(local / m.DBS[0])
        db.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('edge', A, B))
        db.commit()
        db.close()
        before = m.sha(cloud / m.DBS[0])
        with self.assertRaisesRegex(RuntimeError, 'requires its parent'):
            self.run_sync('upload', local, cloud, '2')
        self.assertEqual(m.sha(cloud / m.DBS[0]), before)

    def test_initialized_empty_local_profile_can_update(self):
        local = make_home(self.root / 'local', {})
        cloud = make_home(self.root / 'cloud', {A: ['new']})
        result, _ = self.run_sync('update', local, cloud)
        m.WORK_ROOT = Path(result['package_work'])
        stage = m.restore(rehearsal=True) / 'merged'
        self.assertEqual(set(s.catalog(stage)), {A})

    def test_upload_to_empty_cloud_can_select_only_one_chat(self):
        local = make_home(self.root / 'local', {A: ['one'], B: ['unselected']})
        cloud = make_home(self.root / 'cloud', {})
        result, _ = self.run_sync('upload', local, cloud, '1')
        self.assertEqual(set(s.catalog(Path(result['source']))), {A})

    def test_excluded_chat_stays_local_and_does_not_delete_old_cloud_copy(self):
        local = make_home(self.root / 'local', {A: ['old', 'latest'], B: ['new']})
        cloud = make_home(self.root / 'cloud', {A: ['old']})
        m.write_json(local / s.PREFERENCES, {'format': 1, 'excluded_ids': [A]})
        result, _ = self.run_sync('upload', local, cloud)
        self.assertEqual(result['selected_ids'], [B])
        self.assertEqual(s.catalog(Path(result['source']))[A]['events'], s.catalog(cloud)[A]['events'])
        self.assertFalse((Path(result['source']) / s.PREFERENCES).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
