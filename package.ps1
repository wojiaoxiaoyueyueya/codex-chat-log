$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'build.ps1')
$taskRelease = Join-Path $PSScriptRoot '发布包'
$taskZip = Join-Path $taskRelease 'codex-zhiru-1.3.0-Windows-x64.zip'
if (Test-Path -LiteralPath $taskZip) {
    throw 'Release destination exists. Preserve or move the previous release before packaging again.'
}
$taskPython = Join-Path $PSScriptRoot '.runtime\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPrepare = Start-Process -FilePath (Join-Path $PSScriptRoot 'codex智入.exe') -ArgumentList '--verify-runtime' -WindowStyle Hidden -Wait -PassThru
    if ($taskPrepare.ExitCode -ne 0) { throw 'Offline runtime verification failed.' }
}
$taskPackageCode = @'
from pathlib import Path
import sys
import zipfile

root, target = map(Path, sys.argv[1:])
files = ('codex智入.exe', '使用说明.txt', 'src/offline.py', 'src/win_process.py',
         'src/sidebar.py', 'src/asset_conflicts.py', 'lib/chat_merge.py',
         'lib/chat_select.py', 'lib/chat_snapshot.py', 'vendor/python-3.11.9-embed-amd64.zip')
target.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for name in files:
        archive.write(root / name, 'codex智入/' + name)
print('Portable package created: %d files.' % len(files))
'@
& $taskPython -I -X utf8 -c $taskPackageCode $PSScriptRoot $taskZip
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed. Do not publish an incomplete ZIP.' }
Get-Item -LiteralPath $taskZip | Select-Object FullName,Length
Get-FileHash -LiteralPath $taskZip -Algorithm SHA256
