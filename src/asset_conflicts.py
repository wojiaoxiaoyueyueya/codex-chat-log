"""Keep both versions of conflicting local attachments and rewrite incoming references."""
import json
from pathlib import Path
import shutil
import sqlite3
from urllib.parse import quote


def resolve(incoming, current, home, manifest, merge):
    mapping, renamed = {}, []
    for directory in merge.DIRS[2:]:
        for source in list(merge.safe_files(incoming / directory)):
            rel = source.relative_to(incoming)
            if rel == merge.PASTED_TEXT_REGISTRY: continue
            local = current / rel
            if not local.exists() or merge.sha(source) == merge.sha(local): continue
            digest = merge.sha(source)
            candidate = source.with_name(source.stem + '.来自备份-' + digest[:10] + source.suffix)
            n = 1
            while candidate.exists() or (current / candidate.relative_to(incoming)).exists():
                candidate = source.with_name(source.stem + '.来自备份-' + digest[:10] + '-' + str(n) + source.suffix); n += 1
            new_rel = candidate.relative_to(incoming)
            source.rename(candidate)
            for old, new in ((rel.as_posix(), new_rel.as_posix()), (str(rel), str(new_rel)),
                             (str(home / rel), str(home / new_rel)),
                             ((home / rel).as_uri(), (home / new_rel).as_uri())):
                mapping[old] = new
                encoded = quote(old, safe='/:')
                if encoded != old: mapping[encoded] = quote(new, safe='/:')
            renamed.append({'old': rel.as_posix(), 'new': new_rel.as_posix()})
    if not mapping: return []
    # Replace longest full paths first; user prose is changed only where it
    # actually references this precise attachment. Historical commands are never run.
    pairs = sorted(mapping.items(), key=lambda p: -len(p[0]))
    def text(value):
        for old, new in pairs: value = value.replace(old, new)
        return value
    def walk(value):
        if isinstance(value, str): return text(value)
        if isinstance(value, dict): return {text(k): walk(v) for k, v in value.items()}
        if isinstance(value, list): return [walk(v) for v in value]
        return value
    for path in merge.safe_files(incoming):
        if path.suffix.lower() not in ('.jsonl', '.json', '.txt', '.md', '.html', '.svg'): continue
        if path.suffix == '.jsonl':
            temporary = path.with_suffix('.rewriting')
            with path.open('rb') as stream, temporary.open('wb') as out:
                for raw in stream:
                    obj = json.loads(raw); mapped = walk(obj)
                    out.write((json.dumps(mapped, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8') if mapped != obj else raw)
            # Break any staging hardlink instead of modifying the preview source.
            temporary.replace(path)
        else:
            original = path.read_text(encoding='utf-8-sig'); updated = text(original)
            if updated != original:
                temporary = path.with_suffix(path.suffix + '.rewriting')
                temporary.write_text(updated, encoding='utf-8'); temporary.replace(path)
    for name in merge.DBS:
        db = sqlite3.connect(incoming / name)
        try:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_sqlx_%'").fetchall():
                columns = [r[1] for r in db.execute('PRAGMA table_info(' + merge.quote(table) + ')') if r[2].upper() in ('TEXT', 'JSON', 'VARCHAR')]
                if not columns: continue
                for row in db.execute('SELECT rowid,' + ','.join(map(merge.quote, columns)) + ' FROM ' + merge.quote(table)).fetchall():
                    changes = []
                    for column, value in zip(columns, row[1:]):
                        if not isinstance(value, str): continue
                        updated = text(value)
                        try:
                            obj = json.loads(value); mapped = walk(obj)
                            if mapped != obj: updated = json.dumps(mapped, ensure_ascii=False, separators=(',', ':'))
                        except ValueError: pass
                        if updated != value: changes.append((column, updated))
                    if changes: db.execute('UPDATE ' + merge.quote(table) + ' SET ' + ','.join(merge.quote(k) + '=?' for k, _ in changes) + ' WHERE rowid=?', [v for _, v in changes] + [row[0]])
            db.commit()
        finally: db.close()
    manifest['files'] = [{'path': p.relative_to(incoming).as_posix(), 'size': p.stat().st_size, 'sha256': merge.sha(p)} for p in merge.safe_files(incoming)]
    return renamed
