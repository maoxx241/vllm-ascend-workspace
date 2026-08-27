[CmdletBinding()]
param(
    [switch]$Scheduled
)

. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-NfmWindows

$projectRoot = Get-NfmProjectRoot
$config = Get-NfmConfig
$python = Resolve-NfmPython
Set-NfmProcessEnvironment -Config $config
Set-Location $projectRoot

$pythonArgs = @($python.Arguments) + @((Join-Path $projectRoot 'scripts\supervisor.py'))
if ($Scheduled) {
    $logPath = Get-NfmLogPath
    $logDirectory = Split-Path -Parent $logPath
    New-Item -ItemType Directory -Force $logDirectory | Out-Null
    if ((Test-Path $logPath) -and (Get-Item $logPath).Length -gt 10MB) {
        Move-Item -Force $logPath "$logPath.1"
    }
    "[$(Get-Date -Format o)] Windows scheduled service starting" | Out-File -Append -Encoding utf8 $logPath
    & $python.File @pythonArgs *>> $logPath
} else {
    & $python.File @pythonArgs
}
exit $LASTEXITCODE
