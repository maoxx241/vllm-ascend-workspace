Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-NfmWindows {
    if ($env:OS -ne 'Windows_NT') {
        throw 'This entry point only supports native Windows PowerShell or PowerShell.'
    }
}

function Get-NfmProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Get-NfmTaskName {
    return 'NPU Fleet Monitor'
}

function Get-NfmConfigPath {
    return Join-Path (Get-NfmProjectRoot) 'data\windows-service.json'
}

function Get-NfmLogPath {
    return Join-Path (Get-NfmProjectRoot) 'data\logs\windows-service.log'
}

function Resolve-NfmPython {
    $candidates = @(
        [pscustomobject]@{ Name = 'python.exe'; Arguments = @() },
        [pscustomobject]@{ Name = 'python'; Arguments = @() },
        [pscustomobject]@{ Name = 'py.exe'; Arguments = @('-3') },
        [pscustomobject]@{ Name = 'py'; Arguments = @('-3') }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        $probeArgs = @($candidate.Arguments) + @('-c', 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
        $global:LASTEXITCODE = 0
        $versionText = & $command.Source @probeArgs 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $versionText) { continue }
        try { $version = [version]($versionText | Select-Object -Last 1) } catch { continue }
        if ($version -lt [version]'3.11') {
            throw "Python 3.11+ is required; found $version."
        }
        return [pscustomobject]@{ File = $command.Source; Arguments = @($candidate.Arguments); Version = $version }
    }
    throw 'Python 3.11+ was not found. Install 64-bit Python and add it to PATH.'
}

function Resolve-NfmNpm {
    $command = Get-Command npm.cmd -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { $command = Get-Command npm -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if (-not $command) { throw 'npm was not found. Install Node.js 22.13+ and add it to PATH.' }
    $global:LASTEXITCODE = 0
    $versionText = & node.exe -p 'process.versions.node' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $versionText) { throw 'Node.js could not be executed.' }
    $version = [version]($versionText | Select-Object -Last 1)
    if ($version -lt [version]'22.13') { throw "Node.js 22.13+ is required; found $version." }
    return $command.Source
}

function Assert-NfmOpenSsh {
    foreach ($name in @('ssh.exe', 'ssh-keygen.exe')) {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
            throw "$name was not found. Install the Windows OpenSSH Client optional feature."
        }
    }
}

function Get-NfmPowerShellPath {
    $candidate = Join-Path $PSHOME 'pwsh.exe'
    if (Test-Path $candidate) { return $candidate }
    $candidate = Join-Path $PSHOME 'powershell.exe'
    if (Test-Path $candidate) { return $candidate }
    throw 'The current PowerShell executable could not be located.'
}

function Get-NfmConfig {
    $path = Get-NfmConfigPath
    if (-not (Test-Path $path)) {
        throw "The Windows background task is not installed; missing $path"
    }
    return Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json
}

function Set-NfmProcessEnvironment {
    param([Parameter(Mandatory = $true)]$Config)
    $values = @{
        NFM_BIND = '127.0.0.1'
        NFM_WEB_PORT = [string]$Config.web_port
        NFM_PORT = [string]$Config.api_port
        NFM_IDLE_INTERVAL_SECONDS = [string]$Config.idle_interval
        NFM_HISTORY_INTERVAL_SECONDS = [string]$Config.history_interval
        NFM_INFRA_INTERVAL_SECONDS = [string]$Config.infrastructure_interval
        NFM_HBM_BUSY_THRESHOLD_MB = [string]$Config.hbm_busy_threshold_mb
        NFM_STATE_DIR = [string]$Config.state_dir
    }
    if ($Config.source_workspace) {
        $values.NFM_SOURCE_WORKSPACE = [string]$Config.source_workspace
    }
    foreach ($entry in $values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
}

function Test-NfmHealth {
    param([Parameter(Mandatory = $true)][int]$Port)
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(3)
    try {
        $response = $client.GetAsync("http://127.0.0.1:$Port/api/health").GetAwaiter().GetResult()
        return $response.IsSuccessStatusCode
    } catch {
        return $false
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}
