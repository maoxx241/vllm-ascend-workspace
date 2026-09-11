Status: current

# Windows installation and offline transfer

These are Agent execution recipes for PowerShell in the workspace root. Prepare Windows x64
Python, uv and Git first. The validated combination is Python 3.13.12 and uv
0.10.7; the project supports other Python versions, but a prepared cache must
be validated again for another platform, interpreter or uv version. See the
[installation measurements](installation-feedback-2026-09-11.md).

## Online installation with a cache on the workspace drive

```powershell
$workspaceRoot = (Get-Location).Path
$cachePath = Join-Path $workspaceRoot '.vaws-local\uv-cache'
python .agents/scripts/vaws_deps.py sync --locked --group dev --python 3.13 --no-python-downloads --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& .\.vaws-local\venvs\win32\Scripts\python.exe .agents/scripts/vaws_deps.py doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency inspection failed' }
```

`--cache-dir` applies to this command. Keep using the same cache path on later
syncs. A local cache on the same filesystem as the platform environment permits hardlinks;
cross-filesystem installations fall back to copying. This matters when the
workspace is on a different drive from the default user cache. A junction or
mounted directory can still cross filesystems despite sharing a drive letter.
Use `--link-mode copy` when the destination filesystem cannot hardlink.
No global uv settings need to change. See
[uv cache location](https://docs.astral.sh/uv/concepts/cache/#cache-directory)
and [uv sync options](https://docs.astral.sh/uv/reference/cli/#uv-sync).

`--group dev` includes the local test dependencies. Omitting it does not remove
the three required runtime packages or the default knowledge capability.
`--no-python-downloads` makes a missing interpreter visible immediately; install
Python before continuing.

## Prepare an offline bundle while online

First complete the online sync above for the exact checkout and target Python.
Wait for all uv operations using this cache to finish before copying it.
Create a new bundle directory; copy the entire cache without changing its
internal files. The manifest records the lock and tool combination.

```powershell
$bundlePath = Join-Path $workspaceRoot ('.vaws-local\offline-bundle-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $bundlePath) { throw 'Choose a new bundle directory' }
New-Item -ItemType Directory -Path $bundlePath -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath $cachePath -Destination (Join-Path $bundlePath 'uv-cache') -Recurse -Force -ErrorAction Stop
$pythonIdentity = & .\.vaws-local\venvs\win32\Scripts\python.exe -c 'import platform, sysconfig; print(platform.python_version(), sysconfig.get_platform())'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Python identity' }
$manifest = [ordered]@{
    uv = (uv --version)
    python = $pythonIdentity
    pyproject_sha256 = (Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash
    lock_sha256 = (Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Encoding utf8 -ErrorAction Stop
$bundlePath
```

Transfer that bundle and the matching workspace checkout separately. Prepare
the Python/uv/Git installers while online if the destination lacks them. Do not
copy a prepared virtual environment as the installation: recreate it from the lock and cache. The
bundle does not include model weights, remote containers, shared knowledge
Release downloads or the separate vaws-top service. Those have their own
preparation and storage requirements. Native client configuration belongs to
the destination machine; see [repo-init](../.agents/skills/repo-init/SKILL.md).

## Recreate the environment offline

Run from the matching destination checkout with the bundle path resolved from
the transfer operation. The following checks prevent accidentally
using a bundle prepared for a different lock or interpreter. Use a new local
cache directory instead of merging files into an active uv cache.

```powershell
$workspaceRoot = (Get-Location).Path
# $bundlePath is the actual transferred bundle directory selected by the Agent.
$manifest = Get-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Raw -ErrorAction Stop | ConvertFrom-Json
if ((uv --version) -ne $manifest.uv) { throw 'Install the uv version recorded in manifest.json' }
$pythonIdentity = py -3.13 -c 'import platform, sysconfig; print(platform.python_version(), sysconfig.get_platform())'
if ($LASTEXITCODE -ne 0) { throw 'Install the prepared Python interpreter first' }
if ($pythonIdentity -ne $manifest.python) { throw 'Python version or platform differs from the prepared cache' }
if ((Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash -ne $manifest.pyproject_sha256) { throw 'pyproject.toml differs from the bundle' }
if ((Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash -ne $manifest.lock_sha256) { throw 'uv.lock differs from the bundle' }
$cachePath = Join-Path $workspaceRoot ('.vaws-local\offline-cache-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $cachePath) { throw 'Choose a new cache directory' }
New-Item -ItemType Directory -Path (Split-Path -Parent $cachePath) -Force -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath (Join-Path $bundlePath 'uv-cache') -Destination $cachePath -Recurse -Force -ErrorAction Stop
python .agents/scripts/vaws_deps.py sync --locked --group dev --python 3.13 --offline --no-python-downloads --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Offline sync failed; retain the output and prepare the missing cache entries online' }
& .\.vaws-local\venvs\win32\Scripts\python.exe .agents/scripts/vaws_deps.py doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency inspection failed' }
```

The `py` command assumes the Windows Python launcher is installed; an explicit
path to the same interpreter can replace it. `--offline` limits uv to local and
cached data. A failure means the bundle, interpreter or selected dependencies
are incomplete; prepare those on a connected machine with the same lock and
retry. A changed lock needs a new prepared cache. Preserve the failed output.

Inspect the doctor's JSON `outcome` and individual capabilities. Successful
package installation does not prove remote access or NPU execution. A missing
optional fleet monitor may appear separately from the installed packages.
Use `uv cache clean --cache-dir $cachePath` only when intentionally discarding
that cache; do not manually alter uv's internal cache layout.
