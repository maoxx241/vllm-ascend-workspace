[CmdletBinding()]
param(
  [switch]$AddToPath = $true
)

$ErrorActionPreference = "Stop"

function Get-ArchToken {
  $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
  switch ($arch) {
    "x64"   { return "amd64" }
    "arm64" { return "arm64" }
    default { throw "Unsupported Windows architecture: $arch" }
  }
}

$archToken = Get-ArchToken
$apiUrl = "https://api.github.com/repos/cli/cli/releases/latest"
$headers = @{
  "Accept" = "application/vnd.github+json"
  "User-Agent" = "repo-init-fallback"
}

Write-Host "Querying latest GitHub CLI release ..."
$release = Invoke-RestMethod -Uri $apiUrl -Headers $headers
$asset = $release.assets | Where-Object { $_.name -match ("^gh_.*_windows_{0}\.zip$" -f $archToken) } | Select-Object -First 1

if (-not $asset) {
  throw "Could not find a matching Windows asset for architecture $archToken"
}

$installRoot = Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI\$($release.tag_name)"
$binDir = Join-Path $installRoot "bin"
$currentDir = Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI\current"
$tmpZip = Join-Path $env:TEMP $asset.name
$tmpExtract = Join-Path $env:TEMP ("repo-init-gh-" + [System.Guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Force -Path $binDir | Out-Null
New-Item -ItemType Directory -Force -Path $currentDir | Out-Null
New-Item -ItemType Directory -Force -Path $tmpExtract | Out-Null

Write-Host "Downloading $($asset.name) ..."
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmpZip
Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force

$ghExe = Get-ChildItem -Path $tmpExtract -Recurse -Filter gh.exe | Where-Object {
  $_.FullName -match "\\bin\\gh\.exe$"
} | Select-Object -First 1

if (-not $ghExe) {
  throw "Downloaded archive does not contain bin\gh.exe"
}

Copy-Item -Force $ghExe.FullName (Join-Path $binDir "gh.exe")
Copy-Item -Force (Join-Path $binDir "gh.exe") (Join-Path $currentDir "gh.exe")

if ($AddToPath) {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $pathEntries = @()
  if ($userPath) {
    $pathEntries = $userPath -split ";"
  }
  if ($pathEntries -notcontains $currentDir) {
    $newPath = if ($userPath) { "$userPath;$currentDir" } else { $currentDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Updated the user PATH with: $currentDir"
    Write-Host "Restart your terminal so the new PATH is visible."
  }
}

Remove-Item -Force $tmpZip
Remove-Item -Recurse -Force $tmpExtract

Write-Host ""
Write-Host "Installed gh to $binDir"
Write-Host "Convenience path: $currentDir\gh.exe"
Write-Host ""
Write-Host "Verify with:"
Write-Host "  gh --version"
Write-Host "  gh auth status --hostname github.com"
