[CmdletBinding()]
param(
    [string]$SourceWorkspace = '',
    [string]$StateDir = '',
    [ValidateRange(1, 65535)][int]$WebPort = 8788,
    [ValidateRange(1, 65535)][int]$ApiPort = 8789,
    [ValidateRange(10, 86400)][int]$IdleInterval = 120,
    [ValidateRange(5, 86400)][int]$HistoryInterval = 30,
    [ValidateRange(15, 86400)][int]$InfrastructureInterval = 60,
    [ValidateRange(1, 1048576)][int]$HbmBusyThresholdMb = 8192,
    [switch]$SkipDependencies,
    [switch]$SkipBuild,
    [switch]$SkipTests
)

. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-NfmWindows
Import-Module ScheduledTasks -ErrorAction Stop

$projectRoot = Get-NfmProjectRoot
$python = Resolve-NfmPython
$npm = Resolve-NfmNpm
Assert-NfmOpenSsh

if ($WebPort -eq $ApiPort) { throw 'The web and API ports must be different.' }
if ($SourceWorkspace) {
    $SourceWorkspace = (Resolve-Path $SourceWorkspace).Path
}
if (-not $StateDir) {
    $StateDir = Join-Path $projectRoot 'data'
} elseif (-not [IO.Path]::IsPathRooted($StateDir)) {
    $StateDir = [IO.Path]::GetFullPath((Join-Path $projectRoot $StateDir))
}
New-Item -ItemType Directory -Force $StateDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $projectRoot 'data') | Out-Null

Push-Location $projectRoot
try {
    if (-not $SkipDependencies) {
        & $npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
    }
    if (-not $SkipBuild) {
        & $npm run build
        if ($LASTEXITCODE -ne 0) { throw 'The frontend production build failed.' }
    }
    if (-not $SkipTests) {
        $testArgs = @($python.Arguments) + @((Join-Path $projectRoot 'scripts\run-backend-tests.py'))
        & $python.File @testArgs
        if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
    }
} finally {
    Pop-Location
}

$config = [ordered]@{
    source_workspace = if ($SourceWorkspace) { $SourceWorkspace } else { $null }
    state_dir = $StateDir
    web_port = $WebPort
    api_port = $ApiPort
    idle_interval = $IdleInterval
    history_interval = $HistoryInterval
    infrastructure_interval = $InfrastructureInterval
    hbm_busy_threshold_mb = $HbmBusyThresholdMb
    installed_at = (Get-Date).ToString('o')
}
$config | ConvertTo-Json -Depth 3 | Set-Content -Encoding UTF8 (Get-NfmConfigPath)

$taskName = Get-NfmTaskName
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }

$startScript = Join-Path $projectRoot 'scripts\start-windows.ps1'
$powershell = Get-NfmPowerShellPath
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`" -Scheduled"
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $projectRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local-only Ascend NPU fleet monitor'
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    if (Test-NfmHealth -Port $ApiPort) { $healthy = $true; break }
}
if (-not $healthy) {
    $logPath = Get-NfmLogPath
    if (Test-Path $logPath) { Get-Content -Tail 40 $logPath }
    throw "The task was registered but failed its health check. Inspect $logPath"
}

Write-Host 'NPU Fleet Monitor is installed and running.'
Write-Host "Dashboard: http://127.0.0.1:$WebPort"
Write-Host 'Manage: .\scripts\manage-windows-service.ps1 status'
