$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.runtime\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskExe = Join-Path $taskRoot 'codex智入.exe'
    if (-not (Test-Path -LiteralPath $taskExe)) { & (Join-Path $taskRoot 'build.ps1') }
    $taskPrepare = Start-Process -FilePath $taskExe -ArgumentList '--verify-runtime' -WindowStyle Hidden -Wait -PassThru
    if ($taskPrepare.ExitCode -ne 0) { throw 'Offline runtime verification failed.' }
}
Push-Location $taskRoot
try {
    & $taskPython -I -X utf8 -c "import sys,unittest; from pathlib import Path; sys.path[:0]=[str(Path('tests').resolve()),str(Path('lib').resolve())]; result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(['test_offline','test_chat_merge','test_chat_select'])); sys.exit(not result.wasSuccessful())"
    if ($LASTEXITCODE -ne 0) { throw 'Data regression tests failed.' }
} finally { Pop-Location }
