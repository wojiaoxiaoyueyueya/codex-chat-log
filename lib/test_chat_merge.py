import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('chat_merge', Path(__file__).with_name('chat_merge.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MergeTests(unittest.TestCase):
    def registry(self, paths=(), pending=(), excerpts=None):
        return {'attachmentPaths': list(paths), 'pendingRemovalPaths': list(pending),
                'textExcerptsByPath': excerpts or {}}

    def test_registry_mapping_changes_paths_but_preserves_excerpt_text(self):
        old = r'C:\Users\Old\.codex\attachments\id\pasted-text.txt'
        new = r'C:\Users\New\.codex\attachments\id\pasted-text.txt'
        data = self.registry([old], [old], {old: old + '\n历史文本'})
        result = m.map_attachment_registry(data, m.Mapper(r'C:\Users\Old', Path(r'C:\Users\New\.codex')))
        self.assertEqual(result, self.registry([new], [], {new: old + '\n历史文本'}))
        self.assertEqual(data['pendingRemovalPaths'], [old])

    def test_registry_union_is_repeatable_and_protects_restored_files(self):
        local, shared, cloud = [r'C:\Users\New\.codex\attachments' + '\\' + n for n in ('local.txt', 'shared.txt', 'cloud.txt')]
        alias = shared.lower().replace('\\', '/')
        current = self.registry([local, shared], [local, shared], {local: 'local text', shared: 'original excerpt'})
        incoming = self.registry([alias, cloud], [cloud], {alias: 'sanitized excerpt', cloud: 'cloud text'})
        expected = self.registry([local, shared, cloud], [local], {local: 'local text', shared: 'original excerpt', cloud: 'cloud text'})
        result = m.merge_attachment_registries(current, incoming)
        self.assertEqual(result, expected)
        self.assertEqual(m.merge_attachment_registries(result, incoming), expected)
        m.assert_attachment_registry_preserved(current, result)
        # A cloud excerpt must use the local path's spelling if it exists locally.
        self.assertEqual(m.merge_attachment_registries(self.registry([shared]), incoming)['textExcerptsByPath'][shared], 'sanitized excerpt')

    def test_registry_missing_and_older_optional_excerpt_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'registry.json'
            self.assertEqual(m.read_attachment_registry(p), self.registry())
            m.write_json(p, {'attachmentPaths': ['one'], 'pendingRemovalPaths': []})
            self.assertEqual(m.read_attachment_registry(p), self.registry(['one']))
            incoming = self.registry(['cloud'], ['cloud'], {'cloud': 'text'})
            self.assertEqual(m.merge_attachment_registries(self.registry(), incoming), self.registry(['cloud'], [], {'cloud': 'text'}))

    def test_invalid_registry_is_rejected_without_modifying_it(self):
        bad_values = ['{broken', '[]', '{}',
            json.dumps(self.registry([42])),
            json.dumps({**self.registry(), 'pendingRemovalPaths': 'not-a-list'}),
            json.dumps(self.registry(excerpts={'one': None})),
            json.dumps({**self.registry(), 'futureFormat': 2})]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'registry.json'
            for value in bad_values:
                with self.subTest(value=value):
                    p.write_text(value, encoding='utf-8')
                    with self.assertRaisesRegex(RuntimeError, 'pasted-text attachment registry'):
                        m.read_attachment_registry(p)
                    self.assertEqual(p.read_text(encoding='utf-8'), value)

    def test_structural_mapping_preserves_historical_text(self):
        mapper = m.Mapper(r'C:\Users\Old', Path(r'C:\Users\New\.codex'))
        obj = {'cwd': r'C:\Users\Old\project', 'payload': {
            'text': r'C:\Users\Old\project', 'base_instructions': {'text': r'C:\Users\Old\secret'},
            'content': [{'type': 'input_image', 'image_url': r'C:\Users\Old\.codex\attachments\one.png'}]}}
        mapped = mapper.obj(obj)
        self.assertEqual(mapped['cwd'], r'C:\Users\New\project')
        self.assertEqual(mapped['payload']['text'], obj['payload']['text'])
        self.assertEqual(mapped['payload']['base_instructions'], obj['payload']['base_instructions'])
        self.assertEqual(mapped['payload']['content'][0]['image_url'], r'C:\Users\New\.codex\attachments\one.png')
        self.assertEqual(mapper.path(r'\\?\C:\Users\Old\.codex\sessions\a'), r'C:\Users\New\.codex\sessions\a')
        self.assertEqual(mapper.path(r'C:\Users\Older\project'), r'C:\Users\Older\project')

    def test_merge_global_lists_and_current_preferences(self):
        current = {'threads': ['new'], 'setting': 'local', 'nested': {'x': 1}}
        old = {'threads': ['old', 'new'], 'setting': 'old', 'nested': {'x': 2, 'y': 3}}
        self.assertEqual(m.merge_value(current, old), {'threads': ['new', 'old'], 'setting': 'local', 'nested': {'x': 1, 'y': 3}})

    def test_segmented_history_offsets_use_segment_ordinal_base(self):
        c = sqlite3.connect(':memory:')
        try:
            c.execute('CREATE TABLE thread_turns(thread_id TEXT,rollout_ordinal INTEGER,rollout_end_ordinal INTEGER,rollout_byte_offset INTEGER,rollout_end_byte_offset INTEGER)')
            c.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_ordinal INTEGER,next_rollout_byte_offset INTEGER)')
            c.execute('INSERT INTO thread_turns VALUES("segment",13001,13002,999,999)')
            c.execute('INSERT INTO thread_history_projection_state VALUES("segment",13003,999)')
            m.repair_segment_offsets(c, 'segment', 13000, [0, 100, 250, 420])
            self.assertEqual(c.execute('SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns').fetchone(), (100, 420))
            self.assertEqual(c.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state').fetchone()[0], 420)
        finally:
            c.close()

    def make_fixture(self, root):
        target, stage = root / '.codex', root / 'stage'
        target.mkdir()
        stage.mkdir()
        (target / 'sessions').mkdir()
        (target / 'sessions' / 'local.jsonl').write_bytes(b'latest current chat\n')
        (target / 'auth.json').write_bytes(b'keep-current-login')
        (target / 'config.toml').write_bytes(b'keep-current-config')
        (target / 'state_5.sqlite').write_bytes(b'old db')
        (target / 'state_5.sqlite-wal').write_bytes(b'latest wal')
        (stage / 'sessions').mkdir()
        (stage / 'sessions' / 'local.jsonl').write_bytes(b'latest current chat\n')
        (stage / 'sessions' / 'incoming.jsonl').write_bytes(b'old chat\n')
        (stage / 'state_5.sqlite').write_bytes(b'merged db')
        (stage / 'attachments').mkdir()
        (stage / 'attachments' / 'a.png').write_bytes(b'image')
        backup = root / 'Codex-chat-merge-backups' / 'test'
        return target, stage, backup

    def assert_original(self, target):
        self.assertEqual((target / 'sessions' / 'local.jsonl').read_bytes(), b'latest current chat\n')
        self.assertFalse((target / 'sessions' / 'incoming.jsonl').exists())
        self.assertEqual((target / 'state_5.sqlite').read_bytes(), b'old db')
        self.assertEqual((target / 'state_5.sqlite-wal').read_bytes(), b'latest wal')
        self.assertEqual((target / 'auth.json').read_bytes(), b'keep-current-login')
        self.assertEqual((target / 'config.toml').read_bytes(), b'keep-current-config')
        self.assertFalse((target / 'attachments').exists())

    def test_install_and_rollback_preserve_every_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, stage, backup = self.make_fixture(Path(tmp))
            m.install_stage(stage, target, backup, m.managed_names(target), {}, 'test', idle_check=lambda: None)
            self.assertEqual(m.read_json(backup / 'transaction.json')['status'], 'complete')
            self.assertTrue((target / 'sessions' / 'incoming.jsonl').exists())
            self.assertFalse((target / 'state_5.sqlite-wal').exists())
            m.rollback(backup, idle_check=lambda: None)
            self.assert_original(target)
            self.assertEqual((backup / 'state-before-rollback' / 'state_5.sqlite').read_bytes(), b'merged db')

    def test_failure_at_every_install_step_reverts(self):
        for failed_step in range(1, 18):
            with self.subTest(step=failed_step), tempfile.TemporaryDirectory() as tmp:
                target, stage, backup = self.make_fixture(Path(tmp))
                names = m.managed_names(target)
                if failed_step > len(names):
                    continue
                with self.assertRaisesRegex(RuntimeError, 'Injected'):
                    m.install_stage(stage, target, backup, names, {}, 'test', idle_check=lambda: None, fail_after=failed_step)
                self.assert_original(target)
                self.assertEqual(m.read_json(backup / 'transaction.json')['status'], 'failed_and_reverted')

    def test_running_codex_guard_prevents_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, stage, backup = self.make_fixture(Path(tmp))
            def blocked():
                raise RuntimeError('Codex is running')
            with self.assertRaisesRegex(RuntimeError, 'Codex is running'):
                m.install_stage(stage, target, backup, m.managed_names(target), {}, 'test', idle_check=blocked)
            self.assertFalse(backup.exists())
            self.assert_original(target)

    def test_sql_merge_keeps_target_schema_and_rows(self):
        a, b = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
        try:
            a.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)')
            b.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, future_column TEXT DEFAULT "new")')
            a.executemany('INSERT INTO threads VALUES (?,?)', [('old', 'old title'), ('local', 'stale title')])
            b.execute('INSERT INTO threads VALUES (?,?,?)', ('local', 'latest title', 'preserve'))
            self.assertEqual(m.insert_table(a, b, 'threads', {'local'}), 1)
            self.assertEqual(b.execute('SELECT * FROM threads WHERE id="local"').fetchone(), ('local', 'latest title', 'preserve'))
            self.assertEqual(b.execute('SELECT * FROM threads WHERE id="old"').fetchone(), ('old', 'old title', 'new'))
        finally:
            a.close()
            b.close()

    def test_wal_snapshot_captures_uncheckpointed_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / 'live.sqlite'
            c = sqlite3.connect(p)
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA wal_autocheckpoint=0')
            c.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)')
            c.commit()
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            c.execute('INSERT INTO messages VALUES (1,"latest message only in WAL")')
            c.commit()
            raw = root / 'raw'
            raw.mkdir()
            for suffix in ('', '-wal', '-shm'):
                src = root / ('live.sqlite' + suffix)
                if src.exists():
                    m.shutil.copy2(src, raw / src.name)
            m.snapshot_db(raw / 'live.sqlite', root / 'snapshot.sqlite')
            d = sqlite3.connect(root / 'snapshot.sqlite')
            self.assertEqual(d.execute('SELECT text FROM messages').fetchone()[0], 'latest message only in WAL')
            d.close()
            c.close()

    def test_rehearsal_reads_schema_and_messages_from_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, current = root / '.codex', root / 'current'
            target.mkdir()
            writers = []
            try:
                for name in m.DBS:
                    db = sqlite3.connect(target / name)
                    writers.append(db)
                    db.execute('PRAGMA journal_mode=WAL')
                    db.execute('PRAGMA wal_autocheckpoint=0')
                    db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)')
                    db.commit()
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute('ALTER TABLE messages ADD COLUMN root_turn_id TEXT')
                    db.execute('INSERT INTO messages VALUES (1, "in WAL", "root")')
                    db.commit()
                original = {p: p.read_bytes() for p in target.iterdir() if not p.name.endswith('-shm')}
                m.take_current(target, current, rehearsal=True)
                for name in m.DBS:
                    db = m.db_open(current / name, True)
                    try:
                        self.assertEqual(db.execute('SELECT * FROM messages').fetchall(), [(1, 'in WAL', 'root')])
                    finally:
                        db.close()
                for path, content in original.items():
                    self.assertEqual(path.read_bytes(), content)
            finally:
                for db in writers:
                    db.close()

    def test_cloud_rows_replace_overlap_but_local_rows_remain(self):
        db = sqlite3.connect(':memory:')
        try:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)')
            db.execute('CREATE TABLE thread_items (id TEXT PRIMARY KEY, thread_id TEXT, body TEXT)')
            db.executemany('INSERT INTO threads VALUES (?, ?)', [('cloud', 'old local copy'), ('local', 'local only')])
            db.executemany('INSERT INTO thread_items VALUES (?, ?, ?)', [('a', 'cloud', 'old'), ('b', 'local', 'keep')])
            self.assertEqual(m.remove_cloud_rows(db, 'thread_items', {'cloud'}), 1)
            self.assertEqual(m.remove_cloud_rows(db, 'threads', {'cloud'}), 1)
            self.assertEqual(db.execute('SELECT * FROM threads').fetchall(), [('local', 'local only')])
            self.assertEqual(db.execute('SELECT * FROM thread_items').fetchall(), [('b', 'local', 'keep')])
        finally:
            db.close()

    def test_rehearsal_updates_cloud_overlap_and_keeps_local_only_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target, work = root / 'old-codex', root / '.codex', root / 'work'
            for home in (source, target):
                for name in ('sessions', 'archived_sessions', 'attachments', 'generated_images', 'visualizations'):
                    (home / name).mkdir(parents=True, exist_ok=True)
            cloud = '11111111-1111-1111-1111-111111111111'
            local = '22222222-2222-2222-2222-222222222222'
            segment = '33333333-3333-3333-3333-333333333333'
            cloud_file = 'rollout-' + cloud + '.jsonl'
            local_file = 'rollout-' + local + '.jsonl'
            segment_file = 'rollout-' + segment + '.jsonl'

            def transcript(path, thread_id, text, history_base=None):
                payload = {'id': thread_id, 'history_mode': 'legacy', 'model_provider': 'openai'}
                if history_base:
                    payload['history_base'] = history_base
                path.write_text(
                    json.dumps({'type': 'session_meta', 'payload': payload})
                    + '\n' + json.dumps({'type': 'event', 'payload': {'text': text}}) + '\n',
                    encoding='utf-8',
                )

            transcript(source / 'sessions' / segment_file, cloud, 'cloud base history')
            transcript(target / 'sessions' / segment_file, cloud, 'old base history')
            transcript(source / 'sessions' / cloud_file, cloud, 'new cloud content',
                       {'thread_id': segment, 'end_ordinal_exclusive': 2, 'end_byte_offset': 0})
            transcript(target / 'sessions' / cloud_file, cloud, 'old local cloud content')
            transcript(target / 'sessions' / local_file, local, 'local-only content')
            (source / '.codex-global-state.json').write_text('{}', encoding='utf-8')
            old_attachment = r'C:\Users\Old\.codex\attachments\cloud\pasted-text.txt'
            new_attachment = str(target / 'attachments' / 'cloud' / 'pasted-text.txt')
            local_attachment = str(target / 'attachments' / 'local' / 'pasted-text.txt')
            for home, folder, text in ((source, 'cloud', 'cloud attachment'), (target, 'local', 'local attachment')):
                p = home / 'attachments' / folder / 'pasted-text.txt'
                p.parent.mkdir()
                p.write_text(text, encoding='utf-8')
            m.write_json(source / m.PASTED_TEXT_REGISTRY, self.registry([old_attachment], [old_attachment], {old_attachment: old_attachment}))
            m.write_json(target / m.PASTED_TEXT_REGISTRY, self.registry([local_attachment], [], {local_attachment: 'local attachment'}))
            (target / 'auth.json').write_bytes(b'local login')
            (target / 'config.toml').write_bytes(b'model_provider="openai"')
            cloud_assets = [Path('visualizations/2026/10/11') / cloud / 'chart.html',
                            Path('generated_images') / cloud / 'image.png']
            local_asset = Path('visualizations/2026/10/11') / local / 'chart.html'
            for relative in cloud_assets:
                for home, contents in ((source, b'new cloud asset'), (target, b'old local cloud asset')):
                    (home / relative).parent.mkdir(parents=True, exist_ok=True)
                    (home / relative).write_bytes(contents)
            (target / local_asset).parent.mkdir(parents=True, exist_ok=True)
            (target / local_asset).write_bytes(b'local-only asset')

            def create_state(path, rows):
                db = sqlite3.connect(path)
                try:
                    db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)')
                    db.execute('CREATE TABLE project_roots (id TEXT PRIMARY KEY, project_id TEXT)')
                    db.execute('CREATE TABLE thread_sections (id TEXT PRIMARY KEY, thread_id TEXT)')
                    db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, history_mode TEXT, model_provider TEXT, model TEXT)')
                    db.execute('CREATE TABLE thread_dynamic_tools (id TEXT PRIMARY KEY, thread_id TEXT)')
                    db.execute('CREATE TABLE thread_attachments (id TEXT PRIMARY KEY, thread_id TEXT)')
                    db.execute('CREATE TABLE thread_spawn_edges (id TEXT PRIMARY KEY, parent_thread_id TEXT, child_thread_id TEXT)')
                    db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?)', rows)
                    db.commit()
                finally:
                    db.close()

            old_path = 'C:\\Users\\Old\\.codex\\sessions\\' + cloud_file
            target_cloud_path = str(target / 'sessions' / cloud_file)
            target_local_path = str(target / 'sessions' / local_file)
            create_state(source / 'state_5.sqlite', [(cloud, old_path, str(root / 'missing'), 'legacy', 'openai', 'gpt-test')])
            create_state(target / 'state_5.sqlite', [
                (cloud, target_cloud_path, str(root / 'missing'), 'legacy', 'openai', 'gpt-test'),
                (local, target_local_path, str(root / 'local'), 'legacy', 'openai', 'gpt-test'),
            ])
            for home, ids in ((source, [cloud, segment]), (target, [cloud, local, segment])):
                db = sqlite3.connect(home / 'thread_history_1.sqlite')
                try:
                    db.execute('CREATE TABLE thread_turns (id TEXT PRIMARY KEY, thread_id TEXT, rollout_ordinal INTEGER, rollout_end_ordinal INTEGER, rollout_byte_offset INTEGER, rollout_end_byte_offset INTEGER)')
                    db.execute('CREATE TABLE thread_items (id TEXT PRIMARY KEY, thread_id TEXT, item_json TEXT)')
                    db.execute('CREATE TABLE thread_realtime_items (id TEXT PRIMARY KEY, thread_id TEXT, item_json TEXT)')
                    db.execute('CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY, next_rollout_ordinal INTEGER, next_rollout_byte_offset INTEGER)')
                    for index, thread_id in enumerate(ids):
                        base_ordinal = 2 if thread_id == cloud else 0
                        db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?)', ('turn-' + thread_id, thread_id, base_ordinal, base_ordinal + 1, 0, 0))
                        db.execute('INSERT INTO thread_items VALUES (?,?,?)', ('item-' + thread_id, thread_id, '{}'))
                        db.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)', (thread_id, base_ordinal + 2, 0))
                    db.commit()
                finally:
                    db.close()

            original_files = {p.relative_to(target): p.read_bytes() for p in m.safe_files(target)}
            old_work = m.WORK_ROOT
            try:
                work.mkdir()
                m.WORK_ROOT = work
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    m.prepare(source, target)
                self.assertIn('Prepared transcript 2/2', output.getvalue())
                result = m.restore(rehearsal=True)
                merged = result / 'merged'
                self.assertIn('new cloud content', (merged / 'sessions' / cloud_file).read_text(encoding='utf-8'))
                self.assertIn('local-only content', (merged / 'sessions' / local_file).read_text(encoding='utf-8'))
                state = sqlite3.connect(merged / 'state_5.sqlite')
                self.assertEqual(state.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 2)
                state.close()
                history = m.db_open(merged / m.DBS[1], True)
                try:
                    self.assertEqual(history.execute('SELECT rollout_end_byte_offset FROM thread_turns WHERE thread_id=?', (segment,)).fetchone()[0],
                                     (merged / 'sessions' / segment_file).stat().st_size)
                    self.assertEqual(history.execute('SELECT COUNT(*) FROM thread_turns').fetchone()[0], 3)
                finally:
                    history.close()
                expected_registry = self.registry([local_attachment, new_attachment], [],
                    {local_attachment: 'local attachment', new_attachment: old_attachment})
                self.assertEqual(m.read_json(merged / m.PASTED_TEXT_REGISTRY), expected_registry)
                self.assertEqual((merged / 'attachments' / 'cloud' / 'pasted-text.txt').read_text(), 'cloud attachment')
                self.assertEqual((merged / 'attachments' / 'local' / 'pasted-text.txt').read_text(), 'local attachment')
                for relative in cloud_assets:
                    self.assertEqual((merged / relative).read_bytes(), b'new cloud asset')
                self.assertEqual((merged / local_asset).read_bytes(), b'local-only asset')
                self.assertEqual(m.read_json(result / 'report.json')['cloud_asset_files_updated'], 2)
                # Rehearsal never installs into the target.
                for path, contents in original_files.items():
                    self.assertEqual((target / path).read_bytes(), contents)
                manifest = m.verify_package()
                m.build_merge(merged, root / 'repeated', target, manifest)
                self.assertEqual(m.read_json(root / 'repeated' / m.PASTED_TEXT_REGISTRY), expected_registry)

                # A different ordinary attachment with the same path remains a
                # real conflict; the special case applies only to the registry.
                conflict = work / 'incoming' / 'attachments' / 'local' / 'pasted-text.txt'
                conflict.parent.mkdir()
                conflict.write_bytes(b'different attachment')
                with self.assertRaisesRegex(RuntimeError, 'File conflict:'):
                    m.build_merge(result / 'current', root / 'conflicted', target, manifest)
                conflict.unlink()
                # A visualization that belongs only to a local chat cannot be
                # overwritten by a file merely present in the cloud archive.
                conflict = work / 'incoming' / local_asset
                conflict.parent.mkdir(parents=True, exist_ok=True)
                conflict.write_bytes(b'wrong cloud asset')
                with self.assertRaisesRegex(RuntimeError, 'File conflict:'):
                    m.build_merge(result / 'current', root / 'local-asset-conflict', target, manifest)
                conflict.unlink()
                for path, contents in original_files.items():
                    self.assertEqual((target / path).read_bytes(), contents)

                # Install only into this synthetic profile, then restore exact
                # original bytes, including the unmerged attachment registry.
                backup = root / 'Codex-chat-merge-backups' / 'attachment-test'
                m.install_stage(merged, target, backup, m.managed_names(target), {}, manifest['package_id'], idle_check=lambda: None)
                self.assertEqual(m.read_json(target / m.PASTED_TEXT_REGISTRY), expected_registry)
                self.assertEqual((target / 'auth.json').read_bytes(), b'local login')
                m.rollback(backup, idle_check=lambda: None)
                for path, contents in original_files.items():
                    self.assertEqual((target / path).read_bytes(), contents)
                self.assertFalse((target / 'attachments' / 'cloud').exists())
            finally:
                m.WORK_ROOT = old_work


if __name__ == '__main__':
    unittest.main(verbosity=2)


