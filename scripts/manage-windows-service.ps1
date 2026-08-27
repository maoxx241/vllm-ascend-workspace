[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'uninstall')]
    [string]$Action = 'status'
)

. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-NfmWindows
Import-Module ScheduledTasks -ErrorAction Stop

$taskName = Get-NfmTaskName
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task -and $Action -ne 'logs') {
    throw 'The Windows background task is not installed. Run install-windows-service.ps1 first.'
}

switch ($Action) {
    'start' {
        Start-ScheduledTask -TaskName $taskName
        Write-Host 'Start requested.'
    }
    'stop' {
        Stop-ScheduledTask -TaskName $taskName
        Write-Host 'Service stopped.'
    }
    'restart' {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        Start-ScheduledTask -TaskName $taskName
        Write-Host 'Service restarted.'
    }
    'status' {
        $config = Get-NfmConfig
        $task = Get-ScheduledTask -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        $healthy = Test-NfmHealth -Port ([int]$config.api_port)
        Write-Host "Task state: $($task.State)"
        Write-Host "Health: $(if ($healthy) { 'healthy' } else { 'unavailable' })"
        Write-Host "Dashboard: http://127.0.0.1:$($config.web_port)"
        Write-Host "Last run: $($info.LastRunTime)"
        Write-Host "Last result: $($info.LastTaskResult)"
    }
    'logs' {
        $logPath = Get-NfmLogPath
        if (-not (Test-Path $logPath)) { throw "No log file exists yet: $logPath" }
        Get-Content -Tail 100 -Wait $logPath
    }
    'uninstall' {
        if ($task) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        Write-Host 'The Windows task was removed. History, SSH keys, and configuration were preserved.'
    }
}
