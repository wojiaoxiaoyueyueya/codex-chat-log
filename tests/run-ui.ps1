$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
& (Join-Path $taskRoot 'build.ps1')
$taskFramework=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$taskArgs=@('/nologo','/target:exe','/platform:x64','/langversion:5',('/out:'+(Join-Path $taskRoot 'UiChecks.exe')),('/reference:'+(Join-Path $taskRoot 'codex智入.exe')))
foreach($taskRef in @('System.dll','System.Core.dll','System.Xaml.dll','WPF\WindowsBase.dll','WPF\PresentationCore.dll','WPF\PresentationFramework.dll')){$taskArgs+=('/reference:'+(Join-Path $taskFramework $taskRef))}
$taskArgs+=(Join-Path $PSScriptRoot 'UiChecks.cs')
& (Join-Path $taskFramework 'csc.exe') @taskArgs
if($LASTEXITCODE -ne 0){throw 'UI test build failed'}
& (Join-Path $taskRoot 'UiChecks.exe')
if($LASTEXITCODE -ne 0){throw 'UI checks failed'}
