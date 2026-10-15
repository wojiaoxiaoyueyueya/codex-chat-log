$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskFramework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$taskReferences = @('System.dll','System.Core.dll','System.Xaml.dll','WPF\WindowsBase.dll','System.Web.Extensions.dll','System.Windows.Forms.dll','System.Drawing.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll')
$taskArgs = @('/nologo','/target:winexe','/platform:x64','/langversion:5','/optimize+',('/win32manifest:' + (Join-Path $taskRoot 'src\app.manifest')),('/out:' + (Join-Path $taskRoot 'codex智入.exe')))
foreach ($taskReference in $taskReferences) { $taskArgs += ('/reference:' + (Join-Path $taskFramework $taskReference)) }
$taskArgs += ('/reference:' + (Join-Path $taskFramework 'WPF\PresentationCore.dll'))
$taskArgs += ('/reference:' + (Join-Path $taskFramework 'WPF\PresentationFramework.dll'))
$taskArgs += (Join-Path $taskRoot 'src\App.cs')
$taskArgs += (Join-Path $taskRoot 'src\RichMessage.cs')
& (Join-Path $taskFramework 'csc.exe') @taskArgs
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Write-Host 'Build complete.'
