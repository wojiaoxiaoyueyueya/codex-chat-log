param(
    [string]$CodexHome,
    [string]$Bundle,
    [switch]$RepairPreviews
)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskRuntime = Join-Path $taskRoot '.runtime\python.exe'
$taskPrepare = Start-Process -FilePath (Join-Path $taskRoot 'codex智入.exe') -ArgumentList '--verify-runtime' -WindowStyle Hidden -Wait -PassThru
if ($taskPrepare.ExitCode -ne 0) { throw 'Offline runtime verification failed.' }
$taskArgs = @('-I', '-X', 'utf8', (Join-Path $taskRoot 'src\chat_diagnostics.py'))
if ($CodexHome) { $taskArgs += @('--home', $CodexHome) }
if ($Bundle) { $taskArgs += @('--bundle', $Bundle) }
if ($RepairPreviews) { $taskArgs += '--repair-previews' }
& $taskRuntime @taskArgs
if ($LASTEXITCODE -ne 0) { throw 'Chat diagnostic/repair did not complete. Read the error above.' }
