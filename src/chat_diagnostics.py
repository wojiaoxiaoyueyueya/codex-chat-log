"""Local chat visibility checks. Reports contain counts, never chat text or IDs."""
from __future__ import annotations
import argparse
from collections import Counter
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import chat_merge as m


def file_signature(paths):
    return [(p.name, p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else (p.name, None, None)
            for p in paths]


def database_copy(home, name, destination):
    """Copy DB + WAL; open only the disposable copy, never the real database.

    A changed source invalidates the attempt. Even a stable check is a best
    effort while Codex runs; the report explicitly recommends a closed retry.
    """
    sources = [home / (name + suffix) for suffix in ('', '-wal')]
    for _ in range(3):
        before = file_signature(sources)
        for suffix, source in zip(('', '-wal'), sources):
            target = destination / (name + suffix)
            if source.exists():
                shutil.copyfile(source, target)
            elif target.exists():
                target.unlink()
        if file_signature(sources) == before:
            return destination / name
    raise RuntimeError('聊天数据库仍在写入，请正常退出 Codex 后重新诊断。')


def diagnose(home, *, bundle=None, cache=None):
    home = Path(home).resolve()
    if not (home / m.DBS[0]).is_file():
        raise ValueError('所选目录没有 state_5.sqlite；请检查 CODEX_HOME 或 --home 指定的数据目录。')
    cache = Path(cache) if cache else ROOT / '.diagnostics-cache'
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='visibility-', dir=cache) as temp:
        work = Path(temp)
        with contextlib.closing(sqlite3.connect(database_copy(home, m.DBS[0], work))) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            columns = set(m.cols(db, 'threads'))
            rows = [dict(r) for r in db.execute('SELECT * FROM threads')]
        history = {'present': (home / m.DBS[1]).is_file(), 'tables': {}}
        if history['present']:
            with contextlib.closing(sqlite3.connect(database_copy(home, m.DBS[1], work))) as db:
                db.execute('PRAGMA query_only=ON')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in m.HISTORY_TABLES:
                    if table in tables:
                        history['tables'][table] = db.execute('SELECT count(*) FROM ' + m.quote(table)).fetchone()[0]
        counts = Counter()
        reason_by_id = {}
        referenced = set()
        for row in rows:
            reasons = []
            if row.get('archived'):
                counts['archived'] += 1
                reasons.append('archived')
            else:
                counts['active'] += 1
            source = row.get('source')
            helper = bool(isinstance(source, str) and 'subagent' in source or
                          row.get('thread_source') in ('guardian_review', 'subagent') or row.get('agent_nickname'))
            if helper:
                counts['internal_helpers'] += 1
            empty_preview = 'preview' in columns and not row.get('preview')
            if empty_preview:
                counts['empty_preview'] += 1
                if not row.get('archived'):
                    counts['active_empty_preview'] += 1
                    reasons.append('empty_preview')
                    if not helper:
                        counts['ordinary_active_empty_preview'] += 1
                message = row.get('first_user_message')
                if isinstance(message, str) and message.strip():
                    counts['repairable_previews'] += 1
            path = Path(m.normalized(row.get('rollout_path') or ''))
            referenced.add(os.path.normcase(str(m.plain_path(path))))
            if not m.physical_path(path).is_file():
                counts['missing_transcript_paths'] += 1
                reasons.append('missing_transcript_path')
            if not row.get('cwd') or not Path(m.normalized(row['cwd'])).is_dir():
                counts['missing_workspaces'] += 1
            reason_by_id[row['id']] = reasons
        files = [p for directory in m.DIRS[:2] for p in (home / directory).rglob('*')
                 if p.is_file() and m.is_transcript(p) and m.preferred(p)]
        report = {
            'format': 'codex-visibility-diagnostic-v1',
            'read_only': True,
            'snapshot_note': '运行中的诊断是尽力快照；退出 Codex 后重跑可排除写入干扰。',
            'privacy': '本报告不含聊天正文、标题、会话 ID、账号或文件路径。',
            'database_threads': len(rows),
            'counts': {key: counts[key] for key in (
                'active', 'archived', 'internal_helpers', 'empty_preview', 'active_empty_preview',
                'ordinary_active_empty_preview', 'repairable_previews', 'missing_transcript_paths', 'missing_workspaces')},
            'has_preview_column': 'preview' in columns,
            'transcript_files': len(files),
            'compressed_transcript_files': sum(p.name.lower().endswith('.jsonl.zst') for p in files),
            'unreferenced_transcript_files': sum(os.path.normcase(str(m.plain_path(p))) not in referenced for p in files),
            'history_database': history,
            'notes': [],
        }
        if bundle:
            sys.path.insert(0, str(ROOT / 'src'))
            import offline
            manifest = offline.verify_bundle(Path(bundle))
            ids = {row['id'] for row in manifest['chats']}
            existing = ids & reason_by_id.keys()
            report['bundle_comparison'] = {
                'backup_threads': len(ids), 'present_in_target': len(existing), 'absent_from_target': len(ids - existing),
                'present_with_empty_preview': sum('empty_preview' in reason_by_id[sid] for sid in existing),
                'present_with_missing_transcript': sum('missing_transcript_path' in reason_by_id[sid] for sid in existing),
                'present_archived': sum('archived' in reason_by_id[sid] for sid in existing),
            }
            if ids - existing:
                report['notes'].append('部分备份聊天未入库：回到导入核对列表检查是否未勾选、历史冲突或分支依赖受阻。')
        if counts['repairable_previews']:
            report['notes'].append('存在可补齐的空 preview；退出 Codex 后执行 diagnose.cmd -RepairPreviews，可保留回滚副本修复。')
        if counts['missing_transcript_paths']:
            report['notes'].append('数据库引用的会话文件缺失：检查是否只复制了数据库，或仍保留旧电脑的绝对路径。')
        if counts['missing_workspaces']:
            report['notes'].append('有工作区目录不存在；本工具不迁移项目源码，需检查新电脑的项目路径和项目分组。')
        if not history['present']:
            report['notes'].append('缺少分页历史数据库；paginated 聊天需要相应历史，不能只凭 JSONL 文件判断已恢复。')
        report['notes'].append('未被活动聊天直接引用的 JSONL 也可能是分支共享分段，不能据此删除或强行入库。')
        report['notes'].append('诊断只验证已知条件；通过不等于 Codex 界面的完整恢复验收。')
        return report


def main():
    parser = argparse.ArgumentParser(description='Codex 聊天可见性诊断，默认只读，不关闭应用。')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    parser.add_argument('--bundle', type=Path, help='可选：与原 .czr 比较哪些聊天已入库')
    parser.add_argument('--repair-previews', action='store_true', help='明确执行修复：先正常退出 Codex；保留回滚副本')
    parser.add_argument('--output', type=Path, help='可选报告路径；已有文件不会覆盖')
    args = parser.parse_args()
    if args.repair_previews:
        sys.path.insert(0, str(ROOT / 'src'))
        import offline
        result = offline.main({'action': 'repair_previews', 'home': str(args.home)})
        print('已补齐 %d 条聊天的列表摘要。' % result['repaired_previews'])
        if result['backup']:
            print('回滚副本：' + result['backup'])
    report = diagnose(args.home, bundle=args.bundle)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    print(rendered)
    output = args.output or ROOT / ('codex-diagnostic-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.txt')
    with output.open('x', encoding='utf-8') as stream:
        stream.write(rendered)
    print('诊断报告已保存：' + str(output.resolve()))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('诊断／修复未完成：' + str(exc), file=sys.stderr)
        sys.exit(1)
