"""Check tracked public files and an optional portable release, never user data."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = 'vendor/python-3.11.9-embed-amd64.zip'
RUNTIME_SHA = '009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b'
ZSTD = 'vendor/zstd-v1.5.7-win64.zip'
ZSTD_SHA = 'acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9'
ALLOWED = {
    '.gitignore', '.gitattributes', 'README.md', 'KNOWN_ISSUES.md', 'SECURITY.md',
    'THIRD_PARTY_NOTICES.md', '使用说明.txt', 'build.ps1', 'package.ps1', 'codex智入.exe', RUNTIME,
    'src/App.cs', 'src/RichMessage.cs', 'src/offline.py', 'src/sidebar.py',
    'src/win_process.py', 'src/asset_conflicts.py', 'src/app.manifest',
    'lib/chat_merge.py', 'lib/chat_select.py', 'lib/chat_snapshot.py',
    'lib/test_chat_merge.py', 'lib/test_chat_select.py',
    'tests/test_offline.py', 'tests/UiChecks.cs', 'tests/GracefulWindow.cs',
    'tests/test_graceful_native.py', 'tests/benchmark.py', 'tests/run-ui.ps1',
    'tests/run-data.ps1', 'tests/check_publication.py',
    'diagnose.cmd', 'diagnose.ps1', 'src/chat_diagnostics.py',
    'lib/session_io.py', 'tests/test_session_io.py', ZSTD, 'vendor/zstd-LICENSE.txt',
}
PORTABLE = {'codex智入.exe', '使用说明.txt', RUNTIME,
            'THIRD_PARTY_NOTICES.md', 'lib/session_io.py', ZSTD, 'vendor/zstd-LICENSE.txt',
            'README.md', 'KNOWN_ISSUES.md', 'diagnose.cmd', 'diagnose.ps1', 'src/chat_diagnostics.py',
            'src/offline.py', 'src/win_process.py', 'src/sidebar.py', 'src/asset_conflicts.py',
            'lib/chat_merge.py', 'lib/chat_select.py', 'lib/chat_snapshot.py'}
SECRET = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{30,255}|'
                    r'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,255}|'
                    r'AIza[A-Za-z0-9_-]{30,60}|xox[baprs]-[A-Za-z0-9-]{20,255})\b')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path)
    parser.add_argument('--working-tree', action='store_true', help='Validate the reviewable working copy, including new files, without staging')
    args = parser.parse_args()
    command = ['git', '-C', str(ROOT), 'ls-files', '-z']
    if args.working_tree: command += ['--cached', '--others', '--exclude-standard']
    process = subprocess.run(command, capture_output=True, check=True)
    names = [name for name in process.stdout.decode('utf-8').split('\0') if name]
    if not names: raise ValueError('No tracked publication files. Stage explicit approved files first.')
    if set(names) != ALLOWED: raise ValueError('Tracked file list differs from the complete publication allowlist.')
    for name in names:
        path = ROOT / name
        if name not in ALLOWED or path.is_symlink() or ROOT not in path.resolve().parents:
            raise ValueError('Unexpected public path: ' + name)
        staged = path.read_bytes() if args.working_tree else subprocess.run(
            ['git', '-C', str(ROOT), 'show', ':' + name], capture_output=True, check=True).stdout
        if name in ('codex智入.exe', RUNTIME, ZSTD):
            if hashlib.sha256(staged).hexdigest() != digest(path):
                raise ValueError('Staged binary differs: ' + name)
            continue
        text = path.read_text(encoding='utf-8-sig')
        if staged.decode('utf-8-sig').replace('\r\n', '\n') != text.replace('\r\n', '\n'):
            raise ValueError('Staged text differs: ' + name)
        if SECRET.search(text) or re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text):
            raise ValueError('Potential secret in: ' + name)
    if digest(ROOT / RUNTIME) != RUNTIME_SHA: raise ValueError('Official embedded runtime hash differs.')
    if digest(ROOT / ZSTD) != ZSTD_SHA: raise ValueError('Official Zstandard runtime hash differs.')
    if not (ROOT / 'codex智入.exe').read_bytes().startswith(b'MZ'): raise ValueError('Application binary missing.')
    if args.release:
        with zipfile.ZipFile(args.release) as archive:
            expected = {'codex智入/' + name for name in PORTABLE}
            if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
                raise ValueError('Unexpected portable contents.')
            for name in PORTABLE:
                actual = hashlib.sha256(archive.read('codex智入/' + name)).hexdigest()
                if actual != digest(ROOT / name): raise ValueError('Portable content mismatch: ' + name)
    print('Publication checks passed: %d tracked files; no chat data paths; runtime verified.' % len(names))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('ERROR:', error, file=sys.stderr)
        sys.exit(1)
