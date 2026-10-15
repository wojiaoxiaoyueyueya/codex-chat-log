"""Offline fixtures only. Never close an application or write a real Codex home."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'lib')]
import offline as o
from test_chat_select import make_home, fork_history, compact_transcript, A, B, C, SEG
m, s = o.m, o.selection


def named(home, titles):
    with contextlib.closing(sqlite3.connect(home / m.DBS[0])) as db, db:
        db.execute('ALTER TABLE threads ADD COLUMN name TEXT')
        db.execute('ALTER TABLE threads ADD COLUMN project_id TEXT')
        db.execute('INSERT INTO projects VALUES(?,?)', ('project-one', '机器人项目'))
        for sid, title in titles.items():
            db.execute('UPDATE threads SET name=?, project_id=? WHERE id=?', (title, 'project-one', sid))
    return home


def contents(home):
    return {p.relative_to(home).as_posix(): p.read_bytes() for p in m.safe_files(home)}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zhiru-test-')
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(o, 'CACHE', self.root / 'cache'))
        self.stack.enter_context(patch.object(o, 'owners', return_value=[]))
        self.stack.enter_context(patch.object(m, 'running_codex', return_value=[]))
        self.stack.enter_context(patch.object(m, 'log', o.progress))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def home(self, name, chats):
        return named(make_home(self.root / name, chats), {sid: 'hello' if sid == A else '中文分支' for sid in chats})

    def bundle(self, home, name='backup'):
        path = self.root / (name + '.czr')
        o.export_bundle(home, path)
        return path

    def test_unicode_catalog_project_branch_archive_and_conversation(self):
        home = self.home('source', {A: ['你好'], B: ['分支回答']})
        fork_history(home, B, A, 2)
        with contextlib.closing(sqlite3.connect(home / m.DBS[0])) as db, db:
            db.execute('UPDATE threads SET archived=1 WHERE id=?', (B,))
        rows = {r['id']: r for r in o.list_chats(home)['chats']}
        self.assertEqual(rows[A]['title'], 'hello')
        self.assertEqual(rows[B]['title'], '中文分支')
        self.assertEqual(rows[B]['project'], '机器人项目')
        self.assertEqual(rows[B]['parent_id'], A)
        self.assertTrue(rows[B]['archived'])
        result = o.conversation(home, B)
        self.assertEqual([r['text'] for r in result['messages']], ['你好', '分支回答'])

    def test_export_complete_integrity_without_account_files_or_text_redaction(self):
        text = 'offline secret text ghp_' + 'a' * 36
        home = self.home('source', {A: [text], B: ['分支']})
        fork_history(home, B, A, 2)
        before = contents(home)
        path = self.bundle(home)
        meta = o.verify_bundle(path)
        self.assertEqual(len(meta['chats']), 2)
        with zipfile.ZipFile(path) as z:
            self.assertFalse(any('auth.json' in n or 'config.toml' in n for n in z.namelist()))
            self.assertTrue(any(text.encode() in z.read(n) for n in z.namelist() if n.endswith('.jsonl')))
        self.assertEqual(contents(home), before)

    def test_export_closed_wal_profile_never_opens_source_databases(self):
        home = self.home('closed-wal', {A: ['你好'], B: ['分支']})
        fork_history(home, B, A, 2)
        for name in m.DBS:
            with contextlib.closing(sqlite3.connect(home / name)) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            self.assertFalse((home / (name + '-wal')).exists())
            self.assertFalse((home / (name + '-shm')).exists())
        before, files = o.signature(home), contents(home)
        original_open = m.db_open
        def isolated_only(path, *args, **kwargs):
            self.assertNotEqual(Path(path).resolve().parent, home.resolve(),
                                'Export must not open the original SQLite databases')
            return original_open(path, *args, **kwargs)
        with patch.object(m, 'db_open', side_effect=isolated_only):
            archive = self.bundle(home)
        manifest = o.verify_bundle(archive)
        rows = {row['id']: row for row in manifest['chats']}
        self.assertEqual(rows[B]['parent_id'], A)
        self.assertEqual(rows[A]['title'], 'hello')
        self.assertTrue(all(row['available'] for row in rows.values()))
        self.assertEqual(o.signature(home), before)
        self.assertEqual(contents(home), files)

    def test_export_closed_profile_with_committed_wal_preserves_source(self):
        writer = self.home('writer', {A: ['committed message']})
        home = self.root / 'closed-copy' / '.codex'
        with contextlib.ExitStack() as connections:
            for name in m.DBS:
                db = connections.enter_context(contextlib.closing(sqlite3.connect(writer / name)))
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA wal_autocheckpoint=0')
                if name == m.DBS[0]:
                    db.execute('UPDATE threads SET name=? WHERE id=?', ('WAL 中的最新标题', A))
                else:
                    db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=?',
                               (json.dumps({'committed': 'WAL 中的历史记录'}), A))
                db.commit()
            # Simulate a closed backup left with uncheckpointed, committed WAL.
            # Only these isolated test databases have open writers, never a real profile.
            shutil.copytree(writer, home)
        before, files = o.signature(home), contents(home)
        self.assertTrue(all((home / (name + '-wal')).stat().st_size > 0 for name in m.DBS))
        archive = self.bundle(home)
        extracted = self.root / 'verified'
        manifest = o.verify_bundle(archive, extracted)
        self.assertEqual(manifest['chats'][0]['title'], 'WAL 中的最新标题')
        with contextlib.closing(sqlite3.connect(extracted / m.DBS[1])) as db:
            item = json.loads(db.execute('SELECT item_json FROM thread_items WHERE thread_id=?', (A,)).fetchone()[0])
        self.assertEqual(item, {'committed': 'WAL 中的历史记录'})
        self.assertEqual(o.signature(home), before)
        self.assertEqual(contents(home), files)

    def test_export_rejects_actual_source_changes_with_file_details(self):
        for changed in ('transcript', 'database', 'wal', 'state'):
            with self.subTest(changed=changed):
                home = self.home('changed-' + changed, {A: ['before']})
                output = self.root / (changed + '.czr')
                relative = {'transcript': 'sessions/rollout-' + A + '.jsonl',
                            'database': m.DBS[0], 'wal': m.DBS[0] + '-wal',
                            'state': '.codex-global-state.json'}[changed]
                def change_after_packing(message, percent=None):
                    if percent == 100:
                        with (home / relative).open('ab') as stream:
                            stream.write(b'changed by isolated test')
                with patch.object(o, 'progress', side_effect=change_after_packing):
                    with self.assertRaisesRegex(RuntimeError, re.escape(relative)):
                        o.export_bundle(home, output)
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name(output.name + '.partial').exists())
                self.assertEqual(list(o.CACHE.iterdir()), [])

    def test_capture_rejects_changes_during_copy_with_file_details(self):
        home = self.home('copy-changed', {A: ['before']})
        relative = 'sessions/rollout-' + A + '.jsonl'
        original_copy = m.take_current
        def copy_and_mutate(*args, **kwargs):
            original_copy(*args, **kwargs)
            with (home / relative).open('ab') as stream:
                stream.write(b'changed by isolated test')
        with patch.object(m, 'take_current', side_effect=copy_and_mutate):
            with self.assertRaisesRegex(RuntimeError, re.escape(relative)):
                self.bundle(home)
        self.assertFalse((self.root / 'backup.czr').exists())

    def test_export_has_no_sibling_partial_and_never_replaces_existing_backup(self):
        home = self.home('source', {A: ['hello']})
        output = self.root / 'only-finished.czr'
        observed = []
        def inspect(message, percent=None):
            if percent == 100:
                observed.append(list(self.root.glob('only-finished*')))
        with patch.object(o, 'progress', side_effect=inspect): o.export_bundle(home, output)
        self.assertEqual(observed, [[]])
        self.assertEqual(list(self.root.glob('only-finished*')), [output])
        original = output.read_bytes()
        with self.assertRaises(ValueError): o.export_bundle(home, output)
        self.assertEqual(output.read_bytes(), original)
        racing = self.root / 'racing.czr'
        def create_racing(message, percent=None):
            if percent == 100: racing.write_bytes(b'existing backup must survive')
        with patch.object(o, 'progress', side_effect=create_racing):
            with self.assertRaisesRegex(RuntimeError, '同名文件'): o.export_bundle(home, racing)
        self.assertEqual(racing.read_bytes(), b'existing backup must survive')
        self.assertEqual(list(o.CACHE.iterdir()), [])

    def test_browsing_closed_wal_during_export_does_not_modify_source(self):
        home = self.home('source', {A: ['hello'], B: ['branch']})
        fork_history(home, B, A, 2)
        for name in m.DBS:
            with contextlib.closing(sqlite3.connect(home / name)) as db: db.execute('PRAGMA journal_mode=WAL')
        before, files = o.signature(home), contents(home)
        readers = o.prepare_readers(home)
        view = Path(readers['home'])
        self.assertFalse((view / 'auth.json').exists())
        self.assertFalse((view / 'config.toml').exists())
        seen = []
        def read_during_export(message, percent=None):
            if percent == 100: seen.extend(x['text'] for x in o.conversation(view, B)['messages'])
        with patch.object(o, 'progress', side_effect=read_during_export): self.bundle(home)
        self.assertEqual(seen, ['hello', 'branch'])
        self.assertEqual(o.signature(home), before)
        self.assertEqual(contents(home), files)
        o.clean_job(o.job_path(readers['token']))

    def test_browsing_snapshots_survive_import_preview_cleanup_and_rollback(self):
        source = self.home('source', {A: ['one', 'two'], B: ['new']})
        local = self.home('local', {A: ['one']})
        before = contents(local)
        preview = o.preview_import(self.bundle(source), local)
        readers = o.prepare_readers(local, preview['archive_home'])
        o.apply_import(preview['token'], [A, B])
        self.assertFalse(o.job_path(preview['token']).exists())
        self.assertEqual([x['text'] for x in o.conversation(readers['home'], A)['messages']], ['one'])
        self.assertEqual([x['text'] for x in o.conversation(readers['archive_home'], A)['messages']], ['one', 'two'])
        o.rollback_latest(local)
        self.assertEqual(contents(local), before)
        self.assertEqual(o.conversation(readers['archive_home'], B)['messages'][0]['text'], 'new')
        o.clean_job(o.job_path(readers['token']))

    def test_browsing_falls_back_to_independent_copy_when_hardlinks_unavailable(self):
        home = self.home('source', {A: ['hello']})
        with patch.object(o.os, 'link', side_effect=OSError('isolated cross-volume fixture')):
            readers = o.prepare_readers(home)
        self.assertEqual(o.conversation(readers['home'], A)['messages'][0]['text'], 'hello')
        original = home / 'sessions' / ('rollout-' + A + '.jsonl')
        copied = Path(readers['home']) / original.relative_to(home)
        self.assertNotEqual(original.stat().st_ino, copied.stat().st_ino)
        o.clean_job(o.job_path(readers['token']))

    def test_cross_volume_partial_is_in_a_hidden_unique_staging_directory(self):
        work = type('OtherVolume', (), {'drive':'Z:'})()
        partial, folder = o.export_temporary(work, self.root / 'finished.czr')
        self.assertEqual(folder.parent, self.root)
        self.assertTrue(folder.name.startswith('.codex-zhiru-export-'))
        self.assertEqual(partial.parent, folder)
        self.assertTrue(folder.stat().st_file_attributes & 0x2)
        folder.rmdir()

    def test_optional_history_metadata_downgrade_roundtrip_and_rollback(self):
        source = self.home('source', {A: ['shared'], B: ['child']})
        fork_history(source, B, A, 2)
        with contextlib.closing(sqlite3.connect(source / m.DBS[1])) as db, db:
            db.execute('ALTER TABLE thread_turns ADD COLUMN root_turn_id TEXT')
            db.execute('UPDATE thread_turns SET root_turn_id=?', ('causal-root-preserved',))
            db.execute('ALTER TABLE thread_items ADD COLUMN started_at_ms INTEGER')
            db.execute('ALTER TABLE thread_items ADD COLUMN completed_at_ms INTEGER')
            db.execute('UPDATE thread_items SET started_at_ms=123,completed_at_ms=456')
        local = self.home('legacy', {SEG: ['local only']})
        before = contents(local)
        preview = o.preview_import(self.bundle(source), local)
        self.assertTrue(preview['schema']['supported'])
        self.assertTrue(preview['schema']['differences'])
        result = o.apply_import(preview['token'], [A, B])
        self.assertGreater(result['compatibility_rows_preserved'], 0)
        with contextlib.closing(sqlite3.connect(local / m.DBS[1])) as db:
            self.assertNotIn('root_turn_id', m.cols(db, 'thread_turns'))
            self.assertNotIn('started_at_ms', m.cols(db, 'thread_items'))
        self.assertTrue(list((local / m.HISTORY_COMPAT_DIR).glob('*.json')))
        self.assertEqual(o.conversation(local, B)['messages'][-1]['text'], 'child')
        fresh = self.home('future', {})
        with contextlib.closing(sqlite3.connect(fresh / m.DBS[1])) as db, db:
            db.execute('ALTER TABLE thread_turns ADD COLUMN root_turn_id TEXT')
            db.execute('ALTER TABLE thread_items ADD COLUMN started_at_ms INTEGER')
            db.execute('ALTER TABLE thread_items ADD COLUMN completed_at_ms INTEGER')
        again = o.preview_import(self.bundle(local, 'legacy-export'), fresh)
        restored = o.apply_import(again['token'], [A, B])
        self.assertGreater(restored['compatibility_values_restored'], 0)
        with contextlib.closing(sqlite3.connect(fresh / m.DBS[1])) as db:
            self.assertEqual(db.execute('SELECT DISTINCT root_turn_id FROM thread_turns').fetchall(), [('causal-root-preserved',)])
            self.assertEqual(db.execute('SELECT DISTINCT started_at_ms,completed_at_ms FROM thread_items').fetchall(), [(123, 456)])
        self.assertEqual(contents(source)['auth.json'], before['auth.json'])
        o.rollback_latest(local)
        self.assertEqual(contents(local), before)

    def test_unknown_history_schema_fails_without_modification_or_losing_preview(self):
        source = self.home('source', {A: ['hello']})
        with contextlib.closing(sqlite3.connect(source / m.DBS[1])) as db, db:
            db.execute('ALTER TABLE thread_turns ADD COLUMN unknown_history_semantics TEXT')
        local = self.home('local', {})
        before = contents(local)
        preview = o.preview_import(self.bundle(source), local)
        self.assertFalse(preview['schema']['supported'])
        self.assertIn('unknown_history_semantics', preview['schema']['reason'])
        self.assertEqual(preview['safe'], 0)
        with self.assertRaisesRegex(ValueError, '不能导入'):
            o.apply_import(preview['token'], [A])
        self.assertEqual(contents(local), before)
        self.assertFalse(m.read_json(o.job_path(preview['token']) / 'job.json')['used'])

    def test_compatibility_payload_cannot_modify_unselected_rows_or_overwrite_values(self):
        home = self.root / 'compat-test'; home.mkdir()
        records = [{'table':'thread_turns', 'thread_id':A, 'key':{'id':'local-row'}, 'values':{'root_turn_id':'wrong'}},
                   {'table':'thread_turns', 'thread_id':A, 'key':{'id':'selected-row'}, 'values':{'root_turn_id':'stale'}}]
        m.save_history_compat(home, records)
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE thread_turns(id TEXT PRIMARY KEY,thread_id TEXT,root_turn_id TEXT)')
            db.executemany('INSERT INTO thread_turns VALUES(?,?,?)', [('local-row',B,None),('selected-row',A,'current')])
            self.assertEqual(m.restore_history_compat(db, home, {A}), 0)
            self.assertEqual(db.execute('SELECT root_turn_id FROM thread_turns ORDER BY id').fetchall(), [(None,), ('current',)])

    def test_conflicting_compatibility_values_fail_closed_in_isolated_stage(self):
        home = self.root / 'compat-conflict'; home.mkdir()
        for value in ('one', 'two'):
            m.save_history_compat(home, [{'table':'thread_turns', 'thread_id':A,
                                         'key':{'id':'selected'}, 'values':{'root_turn_id':value}}])
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE thread_turns(id TEXT PRIMARY KEY,thread_id TEXT,root_turn_id TEXT)')
            db.execute('INSERT INTO thread_turns VALUES(?,?,NULL)', ('selected',A)); db.commit()
            with self.assertRaisesRegex(RuntimeError, '兼容元数据有冲突'):
                with db: m.restore_history_compat(db, home, {A})
            self.assertIsNone(db.execute('SELECT root_turn_id FROM thread_turns').fetchone()[0])

    def test_safe_merge_skip_local_newer_and_preserve_unselected_then_rollback(self):
        source = self.home('source', {A: ['hello'], B: ['old', 'new'], C: ['新增']})
        local = self.home('local', {A: ['hello', '本机更新'], B: ['old'], SEG: ['本机独有']})
        before = contents(local)
        preview = o.preview_import(self.bundle(source), local)
        self.assertEqual(preview['local_newer'], 1)
        self.assertEqual({r['id'] for r in preview['changes']}, {B, C})
        self.assertEqual(contents(local), before, 'Preview must not mutate target')
        result = o.apply_import(preview['token'], [B, C])
        catalog = s.catalog(local)
        self.assertEqual(set(catalog), {A, B, C, SEG})
        self.assertEqual(catalog[A]['events'], s.catalog(self.home('check', {A: ['hello', '本机更新']}))[A]['events'])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['updated'], 1)
        self.assertEqual((local / 'auth.json').read_bytes(), before['auth.json'])
        self.assertEqual((local / 'config.toml').read_bytes(), before['config.toml'])
        self.assertTrue((Path(result['backup']) / '恢复到导入前.cmd').is_file())
        self.assertFalse(o.job_path(preview['token']).exists())
        o.rollback_latest(local)
        self.assertEqual(contents(local), before)

    def test_fresh_target_preserves_project_and_shared_history(self):
        source = self.home('source', {A: ['共享开头'], B: ['分支内容']})
        fork_history(source, B, A, 2)
        local = self.root / 'new-computer' / '.codex'
        preview = o.preview_import(self.bundle(source), local)
        self.assertTrue(preview['new_profile'])
        o.apply_import(preview['token'], [A, B])
        self.assertEqual(s.catalog(source), s.catalog(local))
        self.assertEqual(o.conversation(local, B)['messages'][-1]['text'], '分支内容')
        self.assertFalse((local / 'auth.json').exists())
        o.rollback_latest(local)
        self.assertFalse((local / m.DBS[0]).exists())

    def test_reuse_newer_parent_for_branch_without_overwriting_parent(self):
        source = self.home('source', {A: ['shared'], B: ['child']})
        local = self.home('local', {A: ['shared', 'new local']})
        fork_history(source, B, A, 2)
        path = local / 'sessions' / ('rollout-' + A + '.jsonl')
        compact_transcript(path)
        before = path.read_bytes()
        preview = o.preview_import(self.bundle(source), local)
        self.assertEqual(preview['safe'], 1)
        o.apply_import(preview['token'], [B])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([x['text'] for x in o.conversation(local, B)['messages']], ['shared', 'child'])

    def test_true_conflict_and_incompatible_shared_prefix_are_not_selectable(self):
        source = self.home('source', {A: ['one'], B: ['fork'], C: ['safe']})
        local = self.home('local', {A: ['different']})
        fork_history(source, B, A, 2)
        preview = o.preview_import(self.bundle(source), local)
        rows = {r['id']: r for r in preview['changes']}
        self.assertFalse(rows[A]['selectable'])
        self.assertFalse(rows[B]['selectable'])
        self.assertIn('hello', rows[B]['reason'])
        self.assertTrue(rows[C]['selectable'])
        before = contents(local)
        with self.assertRaises(ValueError):
            o.apply_import(preview['token'], [A])
        self.assertEqual(contents(local), before)

    def test_unchecked_required_parent_stops_before_mutation(self):
        source = self.home('source', {A: ['parent'], B: ['child']})
        local = self.home('local', {})
        fork_history(source, B, A, 2)
        preview = o.preview_import(self.bundle(source), local)
        before = contents(local)
        with self.assertRaisesRegex(RuntimeError, '父聊天'):
            o.apply_import(preview['token'], [B])
        self.assertEqual(contents(local), before)

    def test_changed_target_invalidates_preview(self):
        source = self.home('source', {A: ['one', 'two']})
        local = self.home('local', {A: ['one']})
        preview = o.preview_import(self.bundle(source), local)
        (local / 'config.toml').write_text('model="changed"', encoding='utf-8')
        before = contents(local)
        with self.assertRaisesRegex(RuntimeError, '变化'):
            o.apply_import(preview['token'], [A])
        self.assertEqual(contents(local), before)

    def test_install_failure_rolls_back_and_has_standalone_helper(self):
        source = self.home('source', {A: ['one', 'two']})
        local = self.home('local', {A: ['one']})
        preview = o.preview_import(self.bundle(source), local)
        before = contents(local)
        install = m.install_stage
        def fail(*args, **kwargs):
            return install(*args, **kwargs, fail_after=7)
        with patch.object(m, 'install_stage', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, '恢复记录'):
                o.apply_import(preview['token'], [A])
        self.assertEqual(contents(local), before)
        backup = next((local.parent / 'Codex-chat-merge-backups').glob('zhiru-*'))
        self.assertTrue((backup / '恢复到导入前.cmd').exists())
        self.assertEqual(m.read_json(backup / 'transaction.json')['status'], 'failed_and_reverted')

    def test_missing_helper_does_not_touch_chat(self):
        source = self.home('source', {A: ['new']})
        local = self.home('local', {})
        preview = o.preview_import(self.bundle(source), local)
        before = contents(local)
        with patch.object(o, 'rollback_helper', side_effect=OSError('runtime missing')):
            with self.assertRaises(OSError):
                o.apply_import(preview['token'], [A])
        self.assertEqual(contents(local), before)

    def test_custom_codex_home_path_mapping(self):
        source = self.home('source', {A: ['中文']})
        custom = source.parent / 'custom-data'
        source.rename(custom)
        with contextlib.closing(sqlite3.connect(custom / m.DBS[0])) as db, db:
            db.execute('UPDATE threads SET rollout_path=replace(rollout_path,?,?)', (str(source), str(custom)))
        destination = self.root / 'machine-two' / 'my-codex-data'
        preview = o.preview_import(self.bundle(custom), destination)
        o.apply_import(preview['token'], [A])
        with contextlib.closing(sqlite3.connect(destination / m.DBS[0])) as db, db:
            rollout = db.execute('SELECT rollout_path FROM threads').fetchone()[0]
        self.assertTrue(Path(rollout).is_relative_to(destination))
        self.assertEqual(o.conversation(destination, A)['messages'][0]['text'], '中文')

    def test_all_same_has_no_updates(self):
        source = self.home('source', {A: ['same']})
        local = self.home('local', {A: ['same']})
        preview = o.preview_import(self.bundle(source), local)
        self.assertEqual(preview['changes'], [])
        self.assertEqual(preview['same'], 1)
        self.assertEqual(preview['safe'], 0)

    def test_archive_corruption_and_unsafe_path_rejected(self):
        good = self.bundle(self.home('source', {A: ['test']}))
        with zipfile.ZipFile(good) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        meta = json.loads(entries['codex-zhiru.json'])
        first = 'data/' + meta['files'][0]['path']
        entries[first] = b'x' * len(entries[first])
        corrupt = self.root / 'corrupt.czr'
        with zipfile.ZipFile(corrupt, 'w') as z:
            for n, value in entries.items(): z.writestr(n, value)
        with self.assertRaises(ValueError): o.verify_bundle(corrupt)
        for path in ('../auth.json', 'auth.json', '/sessions/file', 'sessions/../../auth.json',
                     'sessions/CON.jsonl', 'sessions/file:stream', 'sessions/a.', 'sessions\\x', '.'):
            self.assertFalse(o.approved_path(path), path)

    def test_running_process_prevents_export_without_forcing(self):
        source = self.home('source', {A: ['test']})
        output = self.root / 'never.czr'
        with patch.object(o, 'owners', return_value=[{'pid': 123, 'name': 'Codex.exe'}]), \
             patch.object(o.time, 'monotonic', side_effect=[0, 100]), \
             patch.object(o.win_process, 'close_windows') as close, \
             patch.object(o.win_process, 'graceful_shutdown') as shutdown:
            with self.assertRaisesRegex(RuntimeError, '仍在运行'):
                o.export_bundle(source, output, auto_close=True)
        close.assert_called_once()
        shutdown.assert_called_once()
        self.assertFalse(output.exists())

    def test_cleanup_cannot_escape_owned_cache(self):
        with self.assertRaises(ValueError): o.clean_job(self.root)
        with self.assertRaises(ValueError): o.job_path('../outside')
        job = o.new_job()
        o.clean_job(job)
        self.assertFalse(job.exists())

    def test_nameless_agent_uses_nickname_and_parent_project(self):
        home = self.home('source', {A: ['parent'], B: ['branch']})
        path = home / 'sessions' / ('rollout-' + B + '.jsonl')
        lines = path.read_bytes().splitlines(keepends=True)
        meta = json.loads(lines[0])
        meta['payload'].update(parent_thread_id=A, agent_nickname='审阅助手')
        path.write_bytes((json.dumps(meta, ensure_ascii=False) + '\n').encode('utf-8') + b''.join(lines[1:]))
        with contextlib.closing(sqlite3.connect(home / m.DBS[0])) as db, db:
            db.execute('UPDATE threads SET name=NULL,title=NULL,project_id=NULL WHERE id=?', (B,))
        row = next(r for r in o.list_chats(home)['chats'] if r['id'] == B)
        self.assertEqual(row['title'], '审阅助手')
        self.assertEqual(row['project'], '机器人项目')

    def test_operation_lock_rejects_parallel_writer(self):
        with o.operation_lock():
            with self.assertRaisesRegex(RuntimeError, '另一个'):
                with o.operation_lock(): self.fail('Duplicate writer acquired lock')
        with o.operation_lock(): pass

    def test_interrupted_install_recoverable_even_without_live_state_database(self):
        local = self.home('local', {A: ['before']})
        before = contents(local)
        backup = local.parent / 'Codex-chat-merge-backups' / 'zhiru-test-interrupted'
        (backup / 'original').mkdir(parents=True)
        shutil.move(str(local / m.DBS[0]), str(backup / 'original' / m.DBS[0]))
        m.write_json(backup / 'transaction.json', {'status': 'installing', 'target': str(local),
                     'steps': [{'name': m.DBS[0], 'had_original': True, 'has_new': True}]})
        with self.assertRaisesRegex(RuntimeError, '中断'): o.assert_no_pending(local)
        o.rollback_latest(local)
        self.assertEqual(contents(local), before)

    def test_duplicate_casefold_zip_entry_rejected(self):
        source = self.home('source', {A: ['one']})
        archive = self.bundle(source)
        with zipfile.ZipFile(archive, 'a') as z:
            name = next(n for n in z.namelist() if n.startswith('data/'))
            z.writestr(name.upper(), z.read(name))
        with self.assertRaisesRegex(ValueError, '重复'): o.verify_bundle(archive)

    def test_worker_protocol_unicode_readonly(self):
        home = self.home('source', {A: ['你好']})
        request = json.dumps({'action': 'list', 'home': str(home)}, ensure_ascii=False).encode('utf-8')
        result = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(ROOT / 'src' / 'offline.py')],
                                input=request, capture_output=True, timeout=30, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        reply = json.loads(result.stdout.decode('utf-8').strip())
        self.assertEqual(reply['data']['chats'][0]['project'], '机器人项目')

    def test_visualization_collision_keeps_both_and_rewrites_incoming_chat(self):
        relative = Path('visualizations') / '2026' / '07' / '10' / B / 'gripper.html'
        source = self.home('source', {A: ['新聊天引用：visualizations/' + relative.relative_to('visualizations').as_posix()], B: ['same']})
        local = self.home('local', {B: ['same']})
        for home, text in ((source, '<html>source</html>'), (local, '<html>local</html>')):
            path = home / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding='utf-8')
        preview = o.preview_import(self.bundle(source), local)
        original_pack = contents(o.job_path(preview['token']) / 'pack' / 'incoming')
        result = o.apply_import(preview['token'], [A])
        self.assertEqual(len(result['renamed_assets']), 1)
        self.assertEqual((local / relative).read_text(), '<html>local</html>')
        new_path = Path(result['renamed_assets'][0]['new'])
        self.assertEqual((local / new_path).read_text(), '<html>source</html>')
        self.assertIn(new_path.name, o.conversation(local, A)['messages'][0]['text'])

    def test_failed_attempt_keeps_preview_and_can_retry(self):
        source = self.home('source', {A: ['one', 'two'], B: ['new']})
        local = self.home('local', {A: ['one']})
        preview = o.preview_import(self.bundle(source), local)
        work = o.job_path(preview['token']); original = contents(work / 'pack' / 'incoming')
        before = contents(local)
        with patch.object(m, 'build_merge', side_effect=RuntimeError('fixture conflict')):
            with self.assertRaisesRegex(RuntimeError, 'fixture conflict'):
                o.apply_import(preview['token'], [A])
        self.assertFalse(m.read_json(work / 'job.json')['used'])
        self.assertEqual(contents(work / 'pack' / 'incoming'), original)
        self.assertEqual(contents(local), before)
        result = o.apply_import(preview['token'], [A, B])
        self.assertEqual(result['selected'], 2)

    def test_cancel_all_is_noop_and_does_not_consume_preview(self):
        source = self.home('source', {A: ['new']}); local = self.home('local', {})
        preview = o.preview_import(self.bundle(source), local)
        before = contents(local)
        self.assertTrue(o.apply_import(preview['token'], [])['noop'])
        self.assertFalse(m.read_json(o.job_path(preview['token']) / 'job.json')['used'])
        self.assertEqual(contents(local), before)

    def test_saved_sidebar_assignments_and_pinned_order(self):
        home = self.home('source', {A: ['first'], B: ['second'], C: ['third']})
        with contextlib.closing(sqlite3.connect(home / m.DBS[0])) as db, db:
            db.execute('UPDATE threads SET project_id=NULL')
        m.write_json(home / '.codex-global-state.json', {
            'local-projects': {'p': {'name': '真实项目', 'rootPaths': []}},
            'thread-project-assignments': {C: {'projectKind': 'local', 'projectId': 'p'}},
            'projectless-thread-ids': [A, B], 'pinned-thread-ids': [B, A]})
        rows = o.list_chats(home)['chats']
        self.assertEqual([r['id'] for r in rows[:2]], [B, A])
        self.assertEqual(rows[2]['group_name'], '真实项目')

    def test_guardian_named_and_marked_as_internal(self):
        home = self.home('source', {A: ['hello'], B: ['Review']})
        with contextlib.closing(sqlite3.connect(home / m.DBS[0])) as db, db:
            db.execute('ALTER TABLE threads ADD COLUMN thread_source TEXT')
            db.execute('UPDATE threads SET name=?,thread_source=? WHERE id=?', ('Guardian review', 'guardian_review', B))
        rows = {r['id']: r for r in o.list_chats(home)['chats']}
        self.assertTrue(rows[B]['helper']); self.assertTrue(rows[B]['guardian'])
        self.assertTrue(rows[B]['title'].startswith('安全审查'))
        self.assertEqual(rows[B]['original_title'], 'Guardian review')

    def test_cached_page_reused_and_invalidated_on_new_message(self):
        home = self.home('source', {A: ['one']})
        self.assertEqual(o.conversation(home, A)['total'], 1)
        with patch.object(o, 'iter_history', side_effect=AssertionError('Should reuse parsed view')):
            self.assertEqual(o.conversation(home, A)['total'], 1)
        path = home / 'sessions' / ('rollout-' + A + '.jsonl')
        with path.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'新的回答'}}) + '\n')
        self.assertEqual(o.conversation(home, A)['total'], 2)

    def test_chatgpt_named_codex_detected_without_closing_other_chatgpt(self):
        rows = {1:{'pid':1,'parent':0,'name':'ChatGPT.exe','path':r'C:\Program Files\WindowsApps\OpenAI.Codex_123\ChatGPT.exe'},
                2:{'pid':2,'parent':1,'name':'ChatGPT.exe','path':r'C:\Program Files\WindowsApps\OpenAI.Codex_123\ChatGPT.exe'},
                3:{'pid':3,'parent':1,'name':'codex.exe','path':r'C:\Users\test\OpenAI\Codex\codex.exe'},
                4:{'pid':4,'parent':0,'name':'ChatGPT.exe','path':r'C:\ChatGPT\ChatGPT.exe'}}
        found = o.win_process.identify(rows)
        self.assertEqual({r['pid'] for r in found}, {1, 3})

    def test_export_only_keeps_sidebar_atoms_not_drafts_or_account_state(self):
        home = self.home('source', {A: ['one']})
        m.write_json(home / '.codex-global-state.json', {'electron-persisted-atom-state': {
            'flat-project-sidebar-preferences-v1':{'mode':'project'}, 'composer-prompt-drafts-v2':'private-draft', 'sensitive-token':'secret'}})
        with zipfile.ZipFile(self.bundle(home)) as z:
            value = json.loads(z.read('data/.codex-global-state.json'))
        self.assertEqual(set(value['electron-persisted-atom-state']), {'flat-project-sidebar-preferences-v1'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
