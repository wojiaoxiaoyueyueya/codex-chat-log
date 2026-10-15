"""Windows Codex chat snapshot and offline restore. Standard library only."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
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

ROOT = Path(__file__).resolve().parent
DIRECTORIES = ('sessions', 'archived_sessions', 'attachments', 'generated_images', 'visualizations')
GLOBAL_KEYS = {
    'electron-saved-workspace-roots', 'projectless-thread-ids',
    'thread-workspace-root-hints', 'project-order', 'active-workspace-roots',
    'electron-workspace-root-labels', 'pinned-thread-ids',
    'thread-projectless-output-directories', 'thread-writable-roots',
    'thread-project-assignments', 'local-projects', 'selected-project',
    'sidebar-project-thread-orders', 'electron-thread-read-state-v1',
}
DB_PATTERN = re.compile(r'^(state_\d+|thread_history_\d+)\.sqlite$')
SENSITIVE_TABLES = ('remote_control_enrollments', 'external_agent_config_imports')
SECRET_PATTERNS = [
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', re.S),
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{30,255})\b'),
    re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,255}\b'),
    re.compile(r'\bAIza[A-Za-z0-9_-]{30,60}\b'),
    re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,255}\b'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b'),
]

def log(s):
    print(s, flush=True)

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest()

def inside(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or parent not in path.parents:
        raise ValueError(f'Unsafe path outside expected directory: {path}')
    return path

def safe_remove(path, parent):
    path = inside(path, parent)
    if path.is_symlink():
        raise ValueError(f'Refusing symlink: {path}')
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()

def q(name):
    return '"'+name.replace('"', '""')+'"'

def normal_windows_path(value):
    # Codex stores Windows extended-length paths; pathlib treats their anchor
    # differently from the equivalent normal drive path during relative_to.
    if value.startswith('\\\\?\\UNC\\'):
        value='\\\\'+value[8:]
    elif value.startswith('\\\\?\\'):
        value=value[4:]
    return Path(value)

@contextmanager
def connection(*args, **kwargs):
    db = sqlite3.connect(*args, **kwargs)
    try:
        with db:
            yield db
    finally:
        db.close()

def sqlite_snapshot(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with connection(source.resolve().as_uri()+'?mode=ro', uri=True, timeout=30) as src:
        with connection(dest) as dst:
            started = time.monotonic()
            def progress(status, remaining, total):
                if time.monotonic()-started > 180:
                    raise TimeoutError('Database remained busy for 180 seconds')
            src.backup(dst, pages=512, progress=progress, sleep=0.05)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Invalid SQLite snapshot: '+source.name)

class Scrubber:
    def __init__(self, home):
        self.count = 0
        self.known = set()
        # Read credentials only to remove exact occurrences from transcripts.
        # Credential source files are never added to the backup.
        for filename in ('auth.json', 'config.toml'):
            p = home/filename
            if not p.exists():
                continue
            try:
                if p.suffix == '.json':
                    def walk(x, key=''):
                        if isinstance(x, dict):
                            for k, v in x.items(): walk(v, k)
                        elif isinstance(x, str) and len(x) >= 20 and re.search('token|secret|api.?key|password', key, re.I):
                            self.known.add(x)
                    walk(read_json(p))
                else:
                    for m in re.finditer(r'(?im)^\s*[\w.-]*(?:token|secret|api_key|password)[\w.-]*\s*=\s*[\"\']([^\"\'\r\n]{20,})[\"\']', p.read_text(encoding='utf-8')):
                        self.known.add(m.group(1))
            except (ValueError, UnicodeError):
                pass
        for k, v in os.environ.items():
            if re.search('TOKEN|SECRET|API_KEY|PASSWORD', k) and len(v) >= 20:
                self.known.add(v)

    def __call__(self, value):
        for secret in self.known:
            n = value.count(secret)
            if n:
                value = value.replace(secret, '[REDACTED_CREDENTIAL]')
                self.count += n
        for pattern in SECRET_PATTERNS:
            value, n = pattern.subn('[REDACTED_CREDENTIAL]', value)
            self.count += n
        return value

def walk_strings(obj, transform):
    if isinstance(obj, str):
        return transform(obj)
    if isinstance(obj, list):
        return [walk_strings(x, transform) for x in obj]
    if isinstance(obj, dict):
        return {transform(k): walk_strings(v, transform) for k, v in obj.items()}
    return obj

def transform_db(path, transform, sanitize=False):
    with connection(path) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        tables = [x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if sanitize and table in SENSITIVE_TABLES:
                db.execute('DELETE FROM '+q(table))
                continue
            cols = [x[1] for x in db.execute('PRAGMA table_info('+q(table)+')') if x[2].upper() in ('TEXT', 'JSON', 'VARCHAR')]
            if not cols:
                continue
            cur = db.execute('SELECT rowid,'+','.join(map(q, cols))+' FROM '+q(table))
            while True:
                batch = cur.fetchmany(64)
                if not batch:
                    break
                for row in batch:
                    changed = [(c, transform(v)) for c, v in zip(cols, row[1:]) if isinstance(v, str)]
                    changed = [(c,v) for c,v in changed if v != row[1+cols.index(c)]]
                    if changed:
                        db.execute('UPDATE '+q(table)+' SET '+','.join(q(c)+'=?' for c,v in changed)+' WHERE rowid=?', [v for c,v in changed]+[row[0]])
        db.commit()
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('VACUUM')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed: '+path.name)

def jsonl_copy(source, dest, transform, limit=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    seen = 0
    lines = 0
    with source.open('rb') as src, dest.open('wb') as dst:
        while True:
            b = src.readline()
            if not b:
                break
            seen += len(b)
            if limit is not None and seen > limit:
                break
            if not b.endswith(b'\n'):
                if limit is not None:
                    break  # A currently-writing tail is not a complete record.
            if not b.strip():
                continue
            obj = json.loads(b)
            obj = walk_strings(obj, transform)
            dst.write((json.dumps(obj, ensure_ascii=False, separators=(',',':'))+'\n').encode('utf-8'))
            lines += 1
    return lines

def get_stats(data):
    result = {'session_files':0, 'archived_session_files':0, 'unique_sessions':0, 'database_threads':0}
    ids = set()
    for directory in ('sessions','archived_sessions'):
        for p in (data/directory).rglob('*.jsonl'):
            with p.open(encoding='utf-8') as f:
                first = json.loads(f.readline())
            sid = first.get('payload',{}).get('id')
            if sid:
                ids.add(sid)
            result['session_files'] += 1
            if directory == 'archived_sessions': result['archived_session_files'] += 1
    result['unique_sessions'] = len(ids)
    dbs = sorted(data.glob('state_*.sqlite'))
    if dbs:
        with connection(dbs[-1]) as db:
            result['database_threads'] = db.execute('SELECT COUNT(*) FROM threads').fetchone()[0]
            result['archived_database_threads'] = db.execute('SELECT COUNT(*) FROM threads WHERE archived=1').fetchone()[0]
    return result

def backup(source, destination, environment, allow_live=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not allow_live:
        require_closed()
    destination.mkdir(parents=True, exist_ok=True)
    if (destination/'manifest.json').exists():
        raise ValueError('Snapshot output must be empty; preserve the previous snapshot first')
    start = now()
    scrub = Scrubber(source)
    with tempfile.TemporaryDirectory(prefix='codex-backup-') as temp:
        data = Path(temp)/'data'
        data.mkdir()
        # Capture finite file sizes before copying; do not follow a growing file.
        files = [(p,p.stat().st_size) for d in DIRECTORIES for p in (source/d).rglob('*') if p.is_file() and not p.is_symlink()]
        for p in sorted(source.glob('*.sqlite')):
            if DB_PATTERN.fullmatch(p.name):
                log('Snapshot database: '+p.name)
                sqlite_snapshot(p,data/p.name)
                transform_db(data/p.name, scrub, sanitize=True)
        for p, size in files:
            rel = p.relative_to(source)
            dest = data/rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix == '.jsonl':
                log('Capture session: '+p.name)
                jsonl_copy(p,dest,scrub,limit=size)
            elif p.suffix.lower() in ('.json','.html','.md','.txt','.svg'):
                dest.write_text(scrub(p.read_text(encoding='utf-8')),encoding='utf-8')
            else:
                shutil.copy2(p,dest)
        for name in ('session_index.jsonl','history.jsonl'):
            if (source/name).exists():
                jsonl_copy(source/name,data/name,scrub,limit=(source/name).stat().st_size)
        if (source/'.codex-global-state.json').exists():
            state = read_json(source/'.codex-global-state.json')
            state = {k:v for k,v in state.items() if k in GLOBAL_KEYS}
            write_json(data/'.codex-global-state.json',walk_strings(state,scrub))
        stats = get_stats(data)
        inventory = []
        archive = Path(temp)/'snapshot.zip'
        log('Compressing snapshot...')
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
            for p in sorted(data.rglob('*')):
                if p.is_file():
                    rel = p.relative_to(data).as_posix()
                    inventory.append({'path':rel,'size':p.stat().st_size,'sha256':sha(p)})
                    z.write(p,rel)
        parts=[]
        with archive.open('rb') as f:
            i=1
            while block := f.read(45*1024*1024):
                p=destination/f'snapshot.zip.{i:03d}.part'
                p.write_bytes(block)
                parts.append({'file':p.name,'size':len(block),'sha256':sha(p)})
                i+=1
        manifest={'format':1,'started_at_utc':start,'completed_at_utc':now(),
            'source_profile':str(Path.home()),'source_codex_home':str(source),
            'environment':read_json(environment),'live_snapshot':allow_live,
            'redacted_occurrences':scrub.count,'stats':stats,
            'uncompressed_bytes':sum(x['size'] for x in inventory),
            'archive_bytes':archive.stat().st_size,'archive_sha256':sha(archive),
            'parts':parts,'files':inventory,
            'excluded':['credentials and auth.json','config.toml','remote enrollments','browser profiles','plugins and skills','automation/queue/goal state','project working directories outside Codex chat storage','cloud-only or inaccessible remote chats'],
            'note':'Snapshot contains complete saved local records captured during the time window. A live snapshot is per-file/per-database, not a global atomic point; later messages are not included. Known credential patterns are redacted; private storage remains required.'}
        write_json(destination/'manifest.json',manifest)
        log(json.dumps({'stats':stats,'archive_bytes':manifest['archive_bytes'],'redacted_occurrences':scrub.count}))
        return manifest

def approved_member(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        return False
    if p.parts[0] in DIRECTORIES:
        return len(p.parts)>1
    return len(p.parts)==1 and (DB_PATTERN.fullmatch(name) or name in ('session_index.jsonl','history.jsonl','.codex-global-state.json'))

def unpack(snapshot, destination):
    snapshot, destination = Path(snapshot).resolve(), Path(destination).resolve()
    manifest=read_json(snapshot/'manifest.json')
    if manifest.get('format')!=1:
        raise ValueError('Unsupported snapshot format')
    expected={x['path']:x for x in manifest['files']}
    if len(expected)!=len(manifest['files']) or not all(approved_member(x) for x in expected):
        raise ValueError('Invalid or duplicate snapshot paths')
    destination.mkdir(parents=True,exist_ok=True)
    archive=destination/'_snapshot.zip'
    with archive.open('wb') as out:
        for item in manifest['parts']:
            if not re.fullmatch(r'snapshot\.zip\.\d{3}\.part',item['file']):
                raise ValueError('Invalid part name')
            part=snapshot/item['file']
            if part.stat().st_size != item['size'] or sha(part)!=item['sha256']:
                raise ValueError('Corrupt/missing snapshot part: '+part.name)
            with part.open('rb') as src: shutil.copyfileobj(src,out)
    if sha(archive)!=manifest['archive_sha256']:
        raise ValueError('Archive hash mismatch')
    data=destination/'data'
    data.mkdir()
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        if len(set(names))!=len(names) or set(names)!=set(expected):
            raise ValueError('Archive inventory mismatch')
        for item in z.infolist():
            if not approved_member(item.filename) or (item.external_attr>>16)&0o170000 == 0o120000:
                raise ValueError('Unsafe archive member')
            if item.file_size!=expected[item.filename]['size']:
                raise ValueError('Member size mismatch')
            target=inside(data/Path(item.filename),data)
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(item) as src,target.open('wb') as out:
                shutil.copyfileobj(src,out)
            if sha(target)!=expected[item.filename]['sha256']:
                raise ValueError('Member hash mismatch: '+item.filename)
    archive.unlink()
    return manifest,data

def extract(snapshot, destination):
    """Verify and expand a snapshot into an otherwise empty working directory."""
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Extraction output must be empty')
    return unpack(snapshot, destination)

class PathMapper:
    def __init__(self, old_profile, new_profile, old_home, new_home):
        self.pairs=[]
        self.need_colon=all(':' in str(p) for p in (old_profile,old_home))
        for old,new in ((old_home,new_home),(old_profile,new_profile)):
            # JSON-encoded strings in SQLite may have doubled backslashes.
            for a,b in ((str(old).replace('\\','/'),str(new).replace('\\','/')),
                        (str(old).replace('/','\\'),str(new).replace('/','\\')),
                        (str(old).replace('/','\\').replace('\\','\\\\'),str(new).replace('/','\\').replace('\\','\\\\'))):
                self.pairs.append((re.escape(a)+r'(?=$|[\\/"\s])',b))
        self.pairs.sort(key=lambda x:len(x[0]),reverse=True)
        self.replacements={'p'+str(i):b for i,(a,b) in enumerate(self.pairs)}
        self.pattern=re.compile('|'.join('(?P<p'+str(i)+'>'+a+')' for i,(a,b) in enumerate(self.pairs)),re.I)
    def __call__(self,s):
        if self.need_colon and ':' not in s:
            return s
        if s.startswith('data:image/') and ';base64,' in s[:100]:
            return s
        # One pass: a destination nested under the old profile must not be
        # remapped repeatedly by subsequent prefix substitutions.
        return self.pattern.sub(lambda match:self.replacements[match.lastgroup],s)

def running_codex():
    if os.name!='nt':
        return []
    script="@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(Codex|codex-app-server|ChatGPT)$' } | Select-Object -ExpandProperty Id) | ConvertTo-Json -Compress"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError('Cannot verify whether Codex is closed')
    value=json.loads(result.stdout.strip() or '[]')
    return value if isinstance(value,list) else [value]

def require_closed():
    if running_codex():
        raise RuntimeError('Close Codex desktop, Codex CLI and IDE Codex sessions first. No chat files were replaced.')

def managed_targets(target):
    names=set(DIRECTORIES)|{'session_index.jsonl','history.jsonl','.codex-global-state.json'}
    for p in target.glob('*'):
        if re.fullmatch(r'(state_\d+|thread_history_\d+)\.sqlite(?:-wal|-shm)?',p.name):
            names.add(p.name)
    return names

def prepare_paths(data,manifest,target,profile):
    mapper=PathMapper(manifest['source_profile'],profile,manifest['source_codex_home'],target)
    for directory in ('sessions','archived_sessions'):
        for p in (data/directory).rglob('*.jsonl'):
            tmp=p.with_suffix('.tmp')
            jsonl_copy(p,tmp,mapper)
            os.replace(tmp,p)
    for name in ('session_index.jsonl','history.jsonl'):
        p=data/name
        if p.exists():
            tmp=p.with_suffix('.tmp')
            jsonl_copy(p,tmp,mapper)
            os.replace(tmp,p)
    for p in data.glob('*.sqlite'):
        transform_db(p,mapper)
    state=data/'.codex-global-state.json'
    incoming=walk_strings(read_json(state),mapper) if state.exists() else {}
    current=read_json(target/'.codex-global-state.json') if (target/'.codex-global-state.json').exists() else {}
    # Preserve target machine identity, remote pairing and unrelated settings.
    for key in GLOBAL_KEYS:
        current.pop(key,None)
    current.update(incoming)
    write_json(state,current)
    missing=set()
    for p in data.glob('state_*.sqlite'):
        with connection(p) as db:
            for rollout,cwd in db.execute('SELECT rollout_path,cwd FROM threads'):
                rp=normal_windows_path(rollout)
                try:
                    rel=rp.relative_to(target)
                except ValueError:
                    raise ValueError('Unmapped session path in restored database')
                if not (data/rel).is_file():
                    raise ValueError('Database references a missing transcript: '+str(rel))
                if not Path(cwd).exists():
                    missing.add(cwd)
    return sorted(missing)

def repair_history_offsets(data,target):
    states=sorted(data.glob('state_*.sqlite'))
    histories=sorted(data.glob('thread_history_*.sqlite'))
    if not states or not histories:
        return
    paths={}
    with connection(states[-1]) as state:
        for sid,rollout in state.execute('SELECT id,rollout_path FROM threads'):
            paths[sid]=data/normal_windows_path(rollout).relative_to(target)
    for hist in histories:
        with connection(hist) as db:
            cols={r[1] for r in db.execute('PRAGMA table_info(thread_turns)')}
            if not {'thread_id','rollout_ordinal','rollout_byte_offset','rollout_end_ordinal','rollout_end_byte_offset'} <= cols:
                continue
            for sid,path in paths.items():
                offsets=[];pos=0
                with path.open('rb') as f:
                    for line in f:
                        offsets.append(pos);pos+=len(line)
                offsets.append(pos)
                rows=db.execute('SELECT rowid,rollout_ordinal,rollout_end_ordinal FROM thread_turns WHERE thread_id=?',(sid,)).fetchall()
                for rowid,start,end in rows:
                    a=offsets[start] if start is not None and 0<=start<len(offsets)-1 else None
                    b=offsets[end+1] if end is not None and 0<=end+1<len(offsets) else None
                    db.execute('UPDATE thread_turns SET rollout_byte_offset=?,rollout_end_byte_offset=? WHERE rowid=?',(a,b,rowid))
                projection=db.execute('SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(sid,)).fetchone()
                if projection:
                    ordinal=min(max(0,projection[0]),len(offsets)-1)
                    db.execute('UPDATE thread_history_projection_state SET next_rollout_ordinal=?,next_rollout_byte_offset=? WHERE thread_id=?',(ordinal,offsets[ordinal],sid))
            db.commit()
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('Repaired history database failed validation')

def prepare_legacy_caches(data, target):
    legacy = target/'sqlite'
    if not legacy.is_dir():
        return
    # Keep unrelated destination settings/automations, but prevent its old
    # cached chat catalog or legacy state database from resurfacing old chats.
    inside(legacy,target)
    staged = data/'sqlite'
    staged.mkdir()
    for p in legacy.iterdir():
        if p.is_symlink() or p.is_dir():
            raise ValueError('Unexpected linked/directory legacy cache entry')
        if re.fullmatch(r'(state_\d+|thread_history_\d+)\.sqlite(?:-wal|-shm)?',p.name):
            continue
        if p.name.endswith(('-wal','-shm')):
            continue
        if p.suffix in ('.sqlite','.db'):
            sqlite_snapshot(p,staged/p.name)
            if p.name in ('codex-dev.db','codex-thread-summaries-dev.db'):
                with connection(staged/p.name) as db:
                    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    for t in tables:
                        if t.startswith('local_thread_catalog') or t in ('thread_timeline_ledger','thread_turn_summaries','live_visualization_suggestions'):
                            db.execute('DELETE FROM '+q(t))
                    db.commit()
                    db.execute('VACUUM')
        else:
            shutil.copy2(p,staged/p.name)

def restore(snapshot,target,profile,require_idle=True):
    target,profile=Path(target).resolve(),Path(profile).resolve()
    if target==profile or target==Path(target.anchor) or target==ROOT or ROOT in target.parents:
        raise ValueError('Unsafe target directory')
    if target.is_symlink():
        raise ValueError('Target must not be a symlink')
    if require_idle: require_closed()
    target.mkdir(parents=True,exist_ok=True)
    stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    workspace=target.parent/('.codex-restore-stage-'+stamp)
    workspace.mkdir()
    rollback=target.parent/'Codex-chat-restore-backups'/stamp
    moved=[]
    installed=[]
    try:
        log('Verify all hashes and extract snapshot...')
        manifest,data=unpack(snapshot,workspace)
        log('Repair user paths and preserve message history...')
        missing=prepare_paths(data,manifest,target,profile)
        repair_history_offsets(data,target)
        prepare_legacy_caches(data,target)
        if require_idle: require_closed()
        names=managed_targets(target)|{p.name for p in data.iterdir()}
        # Validate every final move target before touching existing state.
        for name in names:
            inside(target/name,target)
            if (target/name).is_symlink(): raise ValueError('Refusing linked target '+name)
        rollback.mkdir(parents=True)
        rollback_info={'format':1,'status':'in_progress','target':str(target),'managed_names':sorted(names),'original_names':[n for n in sorted(names) if (target/n).exists()],'created_at':now()}
        write_json(rollback/'rollback.json',rollback_info)
        log('Preserve current chat state at: '+str(rollback))
        for name in sorted(names):
            p=target/name
            if p.exists():
                os.replace(p,rollback/name)
                moved.append(name)
        for p in list(data.iterdir()):
            os.replace(p,target/p.name)
            installed.append(p.name)
        report={'restored_at':now(),'target':str(target),'snapshot_time':manifest['completed_at_utc'],'stats':manifest['stats'],'rollback':str(rollback),'missing_project_directories':missing,'credentials_preserved':True,'history_preserved_and_offsets_repaired':True}
        write_json(rollback/'restore-report.json',report)
        rollback_info['status']='complete'
        write_json(rollback/'rollback.json',rollback_info)
        log('RESTORE OK. Original chats can be recovered with ROLLBACK.cmd.')
        log('Snapshot chats: '+str(manifest['stats']['unique_sessions']))
        log('Project folders not included/missing: '+str(len(missing)))
        return report
    except Exception:
        # Roll back only paths touched in this operation; never delete .codex.
        for name in installed:
            safe_remove(target/name,target)
        for name in moved:
            os.replace(rollback/name,target/name)
        if (rollback/'rollback.json').exists():
            rollback_info['status']='failed_and_reverted'
            write_json(rollback/'rollback.json',rollback_info)
        raise
    finally:
        safe_remove(workspace,target.parent)

def rollback_restore(backup_dir,require_idle=True):
    backup_dir=Path(backup_dir).resolve()
    info=read_json(backup_dir/'rollback.json')
    if info.get('status')!='complete':
        raise ValueError('This restore did not complete; it is not a rollback candidate')
    target=Path(info['target']).resolve()
    if backup_dir.parent != target.parent/'Codex-chat-restore-backups':
        raise ValueError('Invalid rollback directory')
    if require_idle: require_closed()
    for name in info['managed_names']:
        inside(target/name,target)
        if Path(name).name!=name or (target/name).is_symlink():
            raise ValueError('Unsafe rollback entry')
    # Keep the currently restored state too; rollback itself is reversible.
    undo=backup_dir/'state-before-rollback'
    undo.mkdir(exist_ok=False)
    moved=[]; restored=[]
    try:
        for name in info['managed_names']:
            if (target/name).exists():
                os.replace(target/name,undo/name); moved.append(name)
        for name in info['original_names']:
            if not (backup_dir/name).exists(): raise ValueError('Missing rollback data: '+name)
            os.replace(backup_dir/name,target/name); restored.append(name)
        write_json(backup_dir/'rollback-completed.json',{'completed_at':now()})
        log('ROLLBACK OK. Previous local chats restored.')
    except Exception:
        for name in restored: os.replace(target/name,backup_dir/name)
        for name in moved: os.replace(undo/name,target/name)
        raise

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('backup'); p.add_argument('--source',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))); p.add_argument('--output',required=True); p.add_argument('--environment',required=True); p.add_argument('--allow-live',action='store_true')
    p=sub.add_parser('restore'); p.add_argument('--snapshot',default=str(ROOT/'snapshot')); p.add_argument('--target',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    p=sub.add_parser('verify'); p.add_argument('--snapshot',default=str(ROOT/'snapshot'))
    p=sub.add_parser('extract'); p.add_argument('--snapshot',required=True); p.add_argument('--output',required=True)
    p=sub.add_parser('rollback'); p.add_argument('--backup',required=True)
    args=parser.parse_args()
    if args.command=='backup': backup(args.source,args.output,args.environment,args.allow_live)
    elif args.command=='restore': restore(args.snapshot,args.target,Path.home())
    elif args.command=='rollback': rollback_restore(args.backup)
    elif args.command=='extract':
        manifest, data = extract(args.snapshot, args.output)
        log('EXTRACT OK: '+str(manifest['stats']['unique_sessions'])+' chats to '+str(data))
    else:
        with tempfile.TemporaryDirectory(prefix='codex-verify-') as temp:
            manifest,data=unpack(args.snapshot,temp)
            stats=get_stats(data)
            for p in data.glob('*.sqlite'):
                with connection(p) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Invalid SQLite database')
            if stats!=manifest['stats']: raise ValueError('Chat count mismatch')
            log('VERIFY OK: '+json.dumps(stats))

if __name__=='__main__':
    try: main()
    except Exception as e:
        print('ERROR: '+str(e),file=sys.stderr)
        sys.exit(1)


